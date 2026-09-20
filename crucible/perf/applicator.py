"""Apply one bounded configuration change, restart, and revert if it goes wrong.

This is the only module in Crucible that changes the target's behaviour, so it is
written to be boring: no model output reaches it, every value is checked against
the profile before anything is written, and a change that does not come back
healthy is undone without asking.

Three boundaries, in the order they are enforced:

**The profile decides what may change, not the model.** ``TargetProfile`` holds
``allowed_properties`` with bounds and ``protected_paths``. A proposal naming an
unlisted property is refused even when the value looks harmless -- the allowlist
is the authority, not the plausibility of the number (``DESIGN.md`` section 5).

**The SLA and the load profile cannot be written at all.** ``config/slo.yaml`` and
``locust/**`` are protected paths, and the guard below refuses them by path as
well as by property name. That is the file-guard half of ``DESIGN.md`` section 4.4;
the other half is the ``Policy`` memory kind, which the agent has no write
permission for. Neither is to be weakened without the other, because a file guard
can be bypassed if config moves and a memory permission cannot.

**A change that does not come back healthy is reverted, not reported.** A restart
that fails leaves the target down, and a campaign that pressed on would attribute
the next measurement to a service that never started. Auto-revert restores the
previous file and restarts again; if *that* fails the campaign stops and asks for
a human, because two failed restarts is no longer a configuration problem.

Note what this module does **not** do. It does not decide *whether* to apply --
that is the operator's, through the approval gate in
:mod:`crucible.perf.approval`. It does not measure. And it does not push: getting
the commit onto the target box is :mod:`crucible.perf.deploy`, kept separate
because the refspec is configuration and must never be adjacent to anything the
model influenced (section 19.2).
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .profile import RestartContract, TargetProfile


class ApplyError(RuntimeError):
    """The change could not be applied, or was refused before it was written."""


class ApplyRefused(ApplyError):
    """The guard refused the proposal. Recorded in Audit memory, never retried as-is."""


class RestartBlocked(ApplyError):
    """A manual restart is required. The campaign blocks; it does not fail."""


# ---------------------------------------------------------------------------
# What a change is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One property moving from one value to another.

    ``previous`` is filled in by the applicator from the file it actually read,
    never by the proposer. A model that reported the old value from memory would
    make the revert write back a value that was never in force, and the manifest
    would then describe an experiment that did not happen.
    """

    prop: str
    value: Any
    previous: Any = None
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Proposal:
    """One bounded change, with the reasoning that argues for it.

    Deliberately a *set* of changes rather than a single one, because some fixes
    are genuinely paired -- raising ``maximum-pool-size`` below ``minimum-idle``
    is not a valid configuration -- but the campaign keeps the set small on
    purpose. Two unrelated changes in one experiment produce a result that cannot
    be attributed to either (``DESIGN.md`` section 4.7 in miniature: a fix that
    worked for a reason you cannot name teaches nothing).

    ``predicted_p99_ms`` is recorded and scored, never acted on. The K3 spike's
    agent predicted 140 ms and measured 93 ms; had the prediction been used as
    the "after" figure the verdict would have looked accurate by coincidence
    (section 4.5).
    """

    cause_family: str
    changes: tuple[Change, ...]
    reasoning: str = ""
    confidence: float | None = None
    predicted_p99_ms: float | None = None
    evidence_cited: tuple[str, ...] = ()
    #: Set when the model declined to propose. A refusal is a valid outcome and
    #: must not be coerced into a low-confidence guess.
    abstained: bool = False
    abstain_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cause_family": self.cause_family,
            "changes": [c.as_dict() for c in self.changes],
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "predicted_p99_ms": self.predicted_p99_ms,
            "evidence_cited": list(self.evidence_cited),
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
        }

    def summary(self) -> str:
        """One line for the approval card and the CLI."""
        if self.abstained:
            return f"abstained ({self.abstain_reason or 'no reason given'})"
        parts = [f"{c.prop} {c.previous!r} -> {c.value!r}" for c in self.changes]
        return f"{self.cause_family}: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def path_is_protected(candidate: str | Path, protected_paths: tuple[str, ...] | list[str]) -> str | None:
    """Which protected pattern ``candidate`` matches, or ``None``.

    Patterns are matched against the forward-slash relative path so a profile
    written on one platform behaves the same on the other -- the repo is
    developed on Windows and runs on Linux, and a guard that silently stopped
    matching when the separator changed would be the worst possible bug here.

    ``fnmatch`` is used rather than :meth:`pathlib.PurePath.match` on purpose:
    ``*`` in ``fnmatch`` crosses separators, so ``tests/**`` matches
    ``tests/perf/test_x.py``, whereas ``PurePath.match`` would not. A guard that
    under-matches is worse than one that over-matches.
    """
    text = str(candidate).replace("\\", "/").lstrip("./")
    for pattern in protected_paths:
        clean = pattern.replace("\\", "/")
        if fnmatch.fnmatch(text, clean):
            return pattern
        # `config/profiles/**` should also protect `config/profiles` itself.
        prefix = clean.rstrip("*").rstrip("/")
        if prefix and (text == prefix or text.startswith(f"{prefix}/")):
            return pattern
    return None


def guard_proposal(profile: TargetProfile, proposal: Proposal) -> str | None:
    """Why this proposal is refused, or ``None`` when it may be applied.

    Checks in a fixed order, cheapest and most categorical first. The order
    matters for the audit record: "you may not touch that property at all" is a
    different refusal from "that value is out of range", and an operator reading
    the journal should see the first reason, not the last.
    """
    if proposal.abstained:
        return "proposal abstained; there is nothing to apply"
    if not proposal.changes:
        return "proposal contains no changes"

    if proposal.cause_family not in profile.cause_families:
        return (
            f"{proposal.cause_family!r} is not a cause family declared by profile "
            f"{profile.name!r}. Declared: {', '.join(profile.cause_families)}"
        )

    seen: set[str] = set()
    for change in proposal.changes:
        if change.prop in seen:
            return f"{change.prop} appears twice in one proposal"
        seen.add(change.prop)

        # A property whose name happens to look like a path gets the path check
        # too. Cheap, and it closes the gap where a profile grows a
        # file-valued property and the allowlist alone stops being sufficient.
        hit = path_is_protected(change.prop, profile.protected_paths)
        if hit:
            return f"{change.prop} matches protected path {hit!r} and can never be written by the agent"

        refusal = profile.check_change(change.prop, change.value)
        if refusal:
            return refusal

    hit = path_is_protected(profile.config_file, profile.protected_paths)
    if hit:
        return (
            f"the profile's own config_file {profile.config_file!r} matches protected "
            f"path {hit!r}; the profile is inconsistent and no change can be applied"
        )
    return None


# ---------------------------------------------------------------------------
# Editing a Java properties file
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Render a Python value the way a Java properties file expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def read_property(text: str, prop: str) -> str | None:
    """The current value of ``prop``, or ``None`` when the file does not set it.

    Reads the *last* assignment, because that is what Spring resolves when a key
    appears twice. Returning the first would make the revert write back a value
    the application was never running.
    """
    found: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        key, sep, value = stripped.partition("=")
        if sep and key.strip() == prop:
            found = value.strip()
    return found


def set_property(text: str, prop: str, value: Any) -> str:
    """Return ``text`` with ``prop`` set to ``value``, preserving everything else.

    Comments, blank lines and ordering survive. That is not cosmetic: this file is
    committed once per experiment and read by a human deciding whether a campaign
    did something sensible, and a diff that rewrote the whole file would hide the
    one line that matters.

    A property the file does not yet set is appended under a marked heading rather
    than inserted near similar keys -- an appended line is unambiguous in a diff,
    and guessing where it "belongs" risks landing it inside a profile-specific
    block where it would not apply.
    """
    rendered = _format_value(value)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    hit = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        key, sep, _ = stripped.partition("=")
        if sep and key.strip() == prop:
            lines[index] = f"{prop}={rendered}"
            hit = True
    if not hit:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# added by crucible")
        lines.append(f"{prop}={rendered}")
    trailing = newline if text.endswith(("\n", "\r")) else ""
    return newline.join(lines) + trailing


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


class Restarter(Protocol):
    """Local process, systemd, container or manual all satisfy this."""

    name: str

    def restart(self) -> tuple[bool, str]: ...


def poll_health(
    url: str,
    timeout_s: float,
    *,
    interval_s: float = 2.0,
    now: Any = time.monotonic,
    sleep: Any = time.sleep,
    probe: Any = None,
) -> tuple[bool, float]:
    """Block until the target reports healthy, or the deadline passes.

    Returns ``(healthy, waited_s)``. The wait is charged to wall clock, which is
    the real budget (``DESIGN.md`` section 7), and never to the measured window --
    a restart inside a measurement would put cold-JVM requests into the
    percentiles, and the cold-versus-warm gap on the Oracle box was ~50% against
    a 2.08% noise threshold.
    """
    if not url:
        return True, 0.0
    check = probe or _http_health_ok
    started = now()
    while True:
        if check(url):
            return True, now() - started
        waited = now() - started
        if waited >= timeout_s:
            return False, waited
        sleep(interval_s)


def _http_health_ok(url: str) -> bool:
    """One health probe. Any non-2xx, any exception, is 'not yet'."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5.0) as response:
            if response.status >= 300:
                return False
            body = response.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    # Actuator answers 200 with {"status":"DOWN"} in some configurations, so the
    # status code alone is not enough. An empty body is treated as healthy: not
    # every runtime's health endpoint returns JSON, and a 200 is the contract.
    return '"status":"DOWN"' not in body.replace(" ", "")


@dataclass
class ManualRestarter:
    """No restart automation: block with instructions rather than fail.

    ``DESIGN.md`` section 11. The manifest records that a human intervened -- a run
    with manual steps is not comparable to a fully autonomous one, and a report
    that averaged the two would be quietly wrong.
    """

    contract: RestartContract
    name: str = "manual"

    def restart(self) -> tuple[bool, str]:
        raise RestartBlocked(
            "a manual restart is required.\n"
            f"{self.contract.instructions or 'No instructions recorded in profile.yaml.'}\n"
            "The campaign is paused, not failed."
        )


@dataclass
class CommandRestarter:
    """Run the profile's restart command, then wait for health.

    Every element of argv comes from ``profile.yaml``. Nothing the model produced
    reaches this list, for the same reason the deploy refspec does not: an
    argv-parsing allowlist does not reliably catch a cleverly composed argument,
    so the safe design is that the model never contributes one.
    """

    contract: RestartContract
    workspace: str | Path = "."
    timeout_s: float = 300.0
    name: str = "command"
    _runner: Any = None  # injected in tests
    _probe: Any = None   # injected in tests

    def restart(self) -> tuple[bool, str]:
        if self.contract.manual:
            raise RestartBlocked(
                "profile declares restart.manual: true. Automation is declared, "
                "never guessed (DESIGN.md section 11).\n" + (self.contract.instructions or "")
            )
        if not self.contract.command:
            raise ApplyError(
                "profile declares restart.manual: false but names no restart command; "
                "there is nothing to run and nothing to tell an operator"
            )

        code, output = self._run(list(self.contract.command))
        if code != 0:
            return False, f"restart command exited {code}: {output[:400]}"

        if self.contract.settle_s:
            time.sleep(self.contract.settle_s)

        healthy, waited = poll_health(
            self.contract.health_url,
            self.contract.health_timeout_s,
            probe=self._probe,
        )
        if not healthy:
            return False, (
                f"target did not become healthy within {self.contract.health_timeout_s:.0f}s "
                f"at {self.contract.health_url}"
            )
        return True, f"healthy after {waited:.1f}s"

    def _run(self, argv: list[str]) -> tuple[int, str]:
        if self._runner is not None:
            return self._runner(argv)
        if shutil.which(argv[0]) is None and not Path(self.workspace, argv[0]).exists():
            raise ApplyError(f"restart command {argv[0]!r} is not on PATH and not in the workspace")
        completed = subprocess.run(  # noqa: S603 - argv list, no shell, profile-built
            argv, cwd=str(self.workspace), capture_output=True, text=True,
            timeout=self.timeout_s, check=False,
        )
        return completed.returncode, (completed.stderr or completed.stdout or "").strip()


# ---------------------------------------------------------------------------
# The applicator
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """What one apply attempt did. This is what the manifest records.

    ``reverted`` and ``applied`` are separate fields rather than one status,
    because "applied and kept", "applied then reverted" and "refused before
    writing" are three different experiment outcomes and a scorer must not have to
    infer which happened from a string.
    """

    proposal: Proposal | None = None
    applied: bool = False
    restarted: bool = False
    healthy: bool = False
    reverted: bool = False
    revert_healthy: bool | None = None
    refused: bool = False
    refusal_reason: str = ""
    manual_step: bool = False
    commit: str = ""
    changes: tuple[Change, ...] = ()
    reason: str = ""
    needs_human: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "restarted": self.restarted,
            "healthy": self.healthy,
            "reverted": self.reverted,
            "revert_healthy": self.revert_healthy,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "manual_step": self.manual_step,
            "commit": self.commit,
            "changes": [c.as_dict() for c in self.changes],
            "reason": self.reason,
            "needs_human": self.needs_human,
            "proposal": self.proposal.as_dict() if self.proposal else None,
        }


@dataclass
class Applicator:
    """Guard, write, restart, and revert if the target does not come back.

    ``workspace`` is Crucible's own checkout, not the target box. On the cloud
    setup the edit made here reaches the target by being committed and pushed
    (``DESIGN.md`` section 19); on a local run the restarter below is what makes it
    take effect. Both paths write the same file and record the same manifest, so a
    local rehearsal and a cloud campaign are the same experiment shape.
    """

    profile: TargetProfile
    workspace: Path = field(default_factory=lambda: Path("."))
    restarter: Restarter | None = None
    #: Injected so tests can drive git without a repository.
    _git: Any = None

    # -- file level -------------------------------------------------------

    @property
    def config_path(self) -> Path:
        if not self.profile.config_file:
            raise ApplyError(f"profile {self.profile.name!r} declares no config_file")
        return Path(self.workspace) / self.profile.config_file

    def current_values(self, props: list[str] | tuple[str, ...]) -> dict[str, str | None]:
        """What the config file says right now, for each property named."""
        text = self.config_path.read_text(encoding="utf-8")
        return {prop: read_property(text, prop) for prop in props}

    def _write(self, changes: tuple[Change, ...]) -> None:
        path = self.config_path
        text = path.read_text(encoding="utf-8")
        for change in changes:
            text = set_property(text, change.prop, change.value)
        path.write_text(text, encoding="utf-8")

    # -- the operation ----------------------------------------------------

    def apply(self, proposal: Proposal, *, commit_message: str = "") -> ApplyResult:
        """Guard, write, restart. Revert automatically if it does not come back.

        The order is deliberate and not rearrangeable: the previous values are
        read from disk *before* the write, so the revert restores what was
        actually in force rather than what the proposal claimed was.
        """
        result = ApplyResult(proposal=proposal)

        refusal = guard_proposal(self.profile, proposal)
        if refusal:
            result.refused = True
            result.refusal_reason = refusal
            result.reason = f"refused: {refusal}"
            return result

        path = self.config_path
        if not path.exists():
            raise ApplyError(f"config file not found: {path}")
        original_text = path.read_text(encoding="utf-8")

        # Previous values come from the file, never from the proposal.
        resolved = tuple(
            Change(
                prop=c.prop,
                value=c.value,
                previous=read_property(original_text, c.prop),
                unit=c.unit,
            )
            for c in proposal.changes
        )
        result.changes = resolved

        self._write(resolved)
        result.applied = True

        if commit_message:
            # The resolved changes are appended here rather than formatted by the
            # caller, because only this method knows what was actually on disk.
            # A caller building the message from the proposal would write
            # "None -> 20": the proposal's `previous` is empty until now, and the
            # commit log is part of the audit trail, not a convenience.
            detail = "; ".join(f"{c.prop} {c.previous!r} -> {c.value!r}" for c in resolved)
            result.commit = self._commit(f"{commit_message}\n\n{detail}")

        if self.restarter is None:
            # No restarter: the change is on disk and something else (the deploy
            # hook on the target box) will restart. Not an error, but the result
            # must not claim a health it never observed.
            result.reason = "applied to the workspace; no local restarter configured"
            return result

        try:
            healthy, detail = self.restarter.restart()
        except RestartBlocked as blocked:
            result.manual_step = True
            result.reason = str(blocked)
            return result

        result.restarted = True
        result.healthy = healthy
        result.reason = detail
        if healthy:
            return result

        # Auto-revert. The target did not come back, so the change is undone
        # before anything else happens -- leaving it in place would let the next
        # experiment measure a service that never started, and attribute the
        # result to whatever it tried next.
        path.write_text(original_text, encoding="utf-8")
        result.reverted = True
        if commit_message:
            result.commit = self._commit(f"revert: {commit_message}")
        try:
            revert_healthy, revert_detail = self.restarter.restart()
        except RestartBlocked as blocked:
            result.manual_step = True
            result.needs_human = True
            result.reason = f"{detail}; revert written but restart needs a human: {blocked}"
            return result

        result.revert_healthy = revert_healthy
        if revert_healthy:
            result.reason = f"{detail}; reverted to previous configuration and target is healthy"
        else:
            # Two failed restarts in a row is not a configuration problem any
            # more. Guessing again would be a third arbitrary action against a
            # target nobody understands the state of.
            result.needs_human = True
            result.reason = (
                f"{detail}; revert also failed to come back healthy ({revert_detail}). "
                "The campaign stops: the target is in a state no manifest describes."
            )
        return result

    # -- git --------------------------------------------------------------

    def _commit(self, message: str) -> str:
        """Commit the workspace edit and return the sha.

        Each experiment commits, so aborting experiment 11 leaves HEAD at
        experiment 10 and verified improvements are not thrown away
        (``DESIGN.md`` section 7). The sha is also what the deployer pushes and
        what ``/api/version`` is polled against, so it has to be recorded here
        rather than reconstructed later (section 19.7).
        """
        run = self._git or self._git_default
        rel = str(Path(self.profile.config_file).as_posix())
        code, output = run(["git", "add", "--", rel])
        if code != 0:
            raise ApplyError(f"git add failed: {output[:300]}")
        code, output = run(["git", "commit", "-m", message, "--", rel])
        if code != 0 and "nothing to commit" not in output.lower():
            raise ApplyError(f"git commit failed: {output[:300]}")
        code, output = run(["git", "rev-parse", "HEAD"])
        if code != 0:
            raise ApplyError(f"git rev-parse failed: {output[:300]}")
        return output.strip()

    def _git_default(self, argv: list[str]) -> tuple[int, str]:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell, no model input
            argv, cwd=str(self.workspace), capture_output=True, text=True,
            timeout=60, check=False,
        )
        return completed.returncode, (completed.stdout or completed.stderr or "").strip()
