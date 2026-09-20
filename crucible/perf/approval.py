"""The human gate: nothing irreversible happens without an operator saying so.

``DESIGN.md`` principle 4 -- a human holds every irreversible action -- and
section 19.4, which settles *where* the gate goes: **on the change, not on the
transport**. The operator approves the proposed configuration change. Deploying
that approved change is a mechanical consequence, not a second decision, and a
second gate would cost autonomy while adding no safety.

The approval is bound to the exact parameters it was shown. This is the S12
invariant the coding loop already relies on
(:func:`crucible.ui.hitl.decide_resume`), reused here rather than reimplemented:
an approval of "raise the pool to 20" must not be redeemable for "raise the pool
to 100". Without the binding, the gate would authorise a *category* of action
rather than the action itself, which is not what the person clicking it believes
they are doing.

Requests and decisions are files, not in-memory state, because ``crucible run``
and ``crucible approve`` are different processes -- often different terminals,
sometimes different days. A campaign that could only be approved by the process
that proposed it would rule out every unattended overnight run.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..ui.hitl import PendingAction, decide_resume
from .applicator import Proposal

#: Where pending approvals live when nothing else is configured. Under the state
#: directory rather than the repo: an approval is run state, not source, and a
#: stray approval file committed by accident would be a genuine safety problem.
DEFAULT_STATE_DIR = Path(os.getenv("CRUCIBLE_STATE_DIR", Path.home() / ".crucible"))


class ApprovalTimeout(RuntimeError):
    """Nobody answered inside the deadline. The campaign pauses; it does not apply."""


@dataclass
class ApprovalRequest:
    """What the operator is being asked to approve, in full.

    ``params`` is the binding surface: it is what the decision is checked
    against. It holds the concrete property/value pairs rather than the prose,
    because the prose is what persuades and the parameters are what execute, and
    only the second one can be compared mechanically.
    """

    run_id: str
    experiment: int
    summary: str
    params: dict[str, Any]
    reasoning: str = ""
    cause_family: str = ""
    confidence: float | None = None
    predicted_p99_ms: float | None = None
    evidence_cited: list[str] = field(default_factory=list)
    created_at_epoch_s: float = field(default_factory=time.time)

    @classmethod
    def from_proposal(cls, proposal: Proposal, run_id: str, experiment: int) -> ApprovalRequest:
        return cls(
            run_id=run_id,
            experiment=experiment,
            summary=proposal.summary(),
            # Sorted so two runs proposing the same change produce byte-identical
            # params. The binding check is order-independent anyway, but a stable
            # form makes the audit trail diffable.
            params={c.prop: c.value for c in sorted(proposal.changes, key=lambda c: c.prop)},
            reasoning=proposal.reasoning,
            cause_family=proposal.cause_family,
            confidence=proposal.confidence,
            predicted_p99_ms=proposal.predicted_p99_ms,
            evidence_cited=list(proposal.evidence_cited),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """The approval card, as the CLI prints it.

        Shows the prediction and the confidence because an operator deciding
        whether to spend fifteen minutes of wall clock deserves to see how sure
        the agent is -- and shows them as *claims*, not as results, because
        section 4.5 says the verdict comes from a fresh measurement and never
        from this number.
        """
        lines = [
            f"run {self.run_id} · experiment {self.experiment}",
            f"cause family : {self.cause_family or 'unnamed'}",
            "change       : " + ", ".join(f"{k} -> {v!r}" for k, v in self.params.items()),
        ]
        if self.confidence is not None:
            lines.append(f"confidence   : {self.confidence:.2f} (agent's own estimate, not a measurement)")
        if self.predicted_p99_ms is not None:
            lines.append(
                f"prediction   : p99 {self.predicted_p99_ms:.0f} ms "
                "(tracked and scored; the verdict re-measures instead)"
            )
        if self.evidence_cited:
            lines.append("evidence     : " + ", ".join(self.evidence_cited))
        if self.reasoning:
            lines.append("")
            lines.append(self.reasoning.strip())
        return "\n".join(lines)


@dataclass
class ApprovalDecision:
    """An operator's answer, and what it was an answer to."""

    state: str  # "approved" | "rejected" | "pending"
    responder: str = ""
    reason: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    decided_at_epoch_s: float | None = None

    @property
    def approved(self) -> bool:
        return self.state == "approved"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalGate(Protocol):
    """Anything that can ask a human and wait for the answer."""

    name: str

    def request(self, request: ApprovalRequest) -> ApprovalDecision: ...


@dataclass
class DenyingGate:
    """Refuses everything. The default when no gate is configured.

    Failing closed is the point. A missing gate must not mean "apply freely" --
    that is precisely the configuration mistake that would turn an unattended
    campaign into an unsupervised one.
    """

    name: str = "deny"

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            state="rejected",
            responder="none",
            reason=(
                "no approval gate is configured, so nothing can be approved. "
                "Run with --approve-file, or use `crucible approve` from another terminal."
            ),
            params=dict(request.params),
            decided_at_epoch_s=time.time(),
        )


@dataclass
class PreapprovedGate:
    """Approve without asking. Preflight and tests only, and always recorded.

    ``DESIGN.md`` section 9's preflight is one tiny end-to-end experiment run
    against a value the operator already chose, so there is no second decision to
    make. It carries ``responder="preapproved"`` into the manifest so a reader can
    never mistake such a run for one a human actually looked at -- a run with
    intervention and a run without are different claims (section 11).
    """

    responder: str = "preapproved"
    reason: str = "preflight: operator chose this value when starting the run"
    name: str = "preapproved"

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            state="approved",
            responder=self.responder,
            reason=self.reason,
            params=dict(request.params),
            decided_at_epoch_s=time.time(),
        )


@dataclass
class FileApprovalGate:
    """Write the request to disk, wait for a decision file to appear.

    The transport is deliberately dull. A campaign parks its proposal and blocks;
    an operator in another terminal runs ``crucible approve <run-id>``, which
    writes the decision; this picks it up. No daemon, no socket, nothing that
    needs to survive a reboot to keep an overnight run honest.

    A timeout **pauses**, it does not apply. Section 7's pause holds without
    discarding: the scenario's clock freezes and nothing already measured is
    lost, so an operator who was asleep has not destroyed the run.
    """

    run_id: str
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    timeout_s: float = 3600.0
    poll_interval_s: float = 2.0
    name: str = "file"
    _sleep: Any = None
    _now: Any = None

    # -- paths ------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return Path(self.state_dir) / "approvals" / self.run_id

    def request_path(self, experiment: int) -> Path:
        return self.run_dir / f"{experiment:03d}.request.json"

    def decision_path(self, experiment: int) -> Path:
        return self.run_dir / f"{experiment:03d}.decision.json"

    # -- the gate ---------------------------------------------------------

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        now = self._now or time.monotonic
        sleep = self._sleep or time.sleep

        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.request_path(request.experiment)
        path.write_text(json.dumps(request.as_dict(), indent=2), encoding="utf-8")

        decision_file = self.decision_path(request.experiment)
        started = now()
        while True:
            if decision_file.exists():
                return self._read_decision(decision_file, request)
            if now() - started >= self.timeout_s:
                raise ApprovalTimeout(
                    f"no decision on experiment {request.experiment} of run "
                    f"{self.run_id} within {self.timeout_s:.0f}s. The campaign is "
                    "paused, not failed: nothing was applied and nothing measured "
                    "was discarded. Approve it and resume."
                )
            sleep(self.poll_interval_s)

    def _read_decision(self, path: Path, request: ApprovalRequest) -> ApprovalDecision:
        """Load the decision and check it is an answer to *this* question.

        The binding check is the whole reason this is not simply "does a file
        exist". A decision file whose params differ from the parked ones is
        refused rather than honoured, so a stale file left over from an earlier
        experiment -- or an edited one -- cannot authorise a change nobody saw.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return ApprovalDecision(
                state="rejected",
                responder="unreadable",
                reason=f"decision file could not be read, so it is not an approval: {exc}",
                decided_at_epoch_s=time.time(),
            )

        action = str(payload.get("action", "")).strip().lower()
        args = payload.get("params") or {}
        responder = str(payload.get("responder", "")) or "unknown"
        note = str(payload.get("reason", ""))

        verdict = decide_resume(
            PendingAction(
                run_id=self.run_id,
                node_id=f"experiment-{request.experiment}",
                summary=request.summary,
                params=dict(request.params),
            ),
            action,
            dict(args),
        )
        if not verdict.allowed:
            return ApprovalDecision(
                state="rejected",
                responder=responder,
                reason=verdict.reason,
                params=dict(args),
                decided_at_epoch_s=time.time(),
            )
        if action == "reject":
            return ApprovalDecision(
                state="rejected",
                responder=responder,
                reason=note or "rejected by operator",
                params=dict(args),
                decided_at_epoch_s=time.time(),
            )
        return ApprovalDecision(
            state="approved",
            responder=responder,
            reason=note or verdict.reason,
            params=dict(args),
            decided_at_epoch_s=time.time(),
        )


def write_decision(
    state_dir: Path,
    run_id: str,
    experiment: int,
    *,
    action: str,
    responder: str,
    reason: str = "",
) -> Path:
    """Answer a parked approval. This is what ``crucible approve`` calls.

    The params are copied from the request rather than supplied by the caller.
    That is not a shortcut -- it is what makes the CLI safe: an operator typing
    ``crucible approve`` is approving *what was shown to them*, and giving them a
    way to type different values would reintroduce the unbound approval the
    binding check exists to prevent. Changing the value means a new proposal.
    """
    run_dir = Path(state_dir) / "approvals" / run_id
    request_file = run_dir / f"{experiment:03d}.request.json"
    if not request_file.exists():
        raise FileNotFoundError(
            f"no pending approval for experiment {experiment} of run {run_id} "
            f"(looked in {run_dir})"
        )
    request = json.loads(request_file.read_text(encoding="utf-8"))
    decision = {
        "action": action,
        "responder": responder,
        "reason": reason,
        "params": request.get("params", {}),
        "decided_at_epoch_s": time.time(),
    }
    decision_file = run_dir / f"{experiment:03d}.decision.json"
    decision_file.write_text(json.dumps(decision, indent=2), encoding="utf-8")
    return decision_file


def pending_approvals(state_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Every parked approval for a run that has no decision yet."""
    run_dir = Path(state_dir) / "approvals" / run_id
    if not run_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for request_file in sorted(run_dir.glob("*.request.json")):
        # `*.request.json` also matches `NNN.manual.request.json`. A manual step
        # is not an approval -- it carries no params and authorises nothing -- so
        # listing it here made `crucible status` show it as a pending approval
        # and then fail reading a field it does not have. Found by running the
        # command; the two file types share a directory on purpose, so anything
        # globbing that directory has to be explicit about which it wants.
        if request_file.name.endswith(".manual.request.json"):
            continue
        stem = request_file.name.split(".")[0]
        if (run_dir / f"{stem}.decision.json").exists():
            continue
        try:
            out.append(json.loads(request_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Manual steps (W2-Q8)
# ---------------------------------------------------------------------------
#
# Section 11 says the campaign "blocks with instructions rather than failing",
# and section 7's pause "holds without discarding". A manual restart therefore
# parks and waits rather than aborting, so a long campaign is not restarted from
# its baseline because somebody had to bounce a JVM by hand.
#
# This reuses the approvals DIRECTORY and the experiment numbering -- that is
# what makes a pause and its resume correlate with nothing to remember -- but
# NOT the approval decision type. An approval carries a binding check: the values
# in the answer are compared against the values the campaign parked, which is
# what stops an approval of "pool 20" being redeemed for "pool 100". "I finished
# the restart" carries no values. Binding it would misrepresent it as a second
# approval; exempting it would create a file that bypasses the guarantee. So it
# is a different file with a different action, exempt by construction rather
# than by exception, and recorded on the manifest as a manual step and never as
# an approval.


class ManualStepTimeout(RuntimeError):
    """Nobody confirmed the manual step. The campaign stays paused."""


@dataclass
class ManualStep:
    """One thing a human has to do before the campaign can continue."""

    run_id: str
    experiment: int
    instructions: str
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    timeout_s: float = 3600.0
    poll_interval_s: float = 2.0
    _sleep: Any = None
    _now: Any = None

    @property
    def run_dir(self) -> Path:
        return Path(self.state_dir) / "approvals" / self.run_id

    def request_path(self) -> Path:
        # Distinct from `NNN.request.json`. One experiment can wait TWICE -- once
        # for authorisation, later for a manual step -- and a shared filename
        # would let the answer to one satisfy the other.
        return self.run_dir / f"{self.experiment:03d}.manual.request.json"

    def confirmation_path(self) -> Path:
        return self.run_dir / f"{self.experiment:03d}.manual.json"

    def park(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.request_path()
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "experiment": self.experiment,
                    "kind": "manual_step",
                    "instructions": self.instructions,
                    "parked_at_epoch_s": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def wait(self) -> dict[str, Any]:
        """Block until a human confirms, or the deadline passes.

        A timeout raises rather than continuing. The campaign is paused, and
        proceeding on a deadline would measure a target whose change was never
        put in force -- which is the whole failure this pause exists to prevent.
        """
        now = self._now or time.monotonic
        sleep = self._sleep or time.sleep
        self.park()
        confirmation = self.confirmation_path()
        started = now()
        while True:
            if confirmation.exists():
                try:
                    return json.loads(confirmation.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ManualStepTimeout(
                        f"the manual-step confirmation for experiment {self.experiment} "
                        f"could not be read, so it is not a confirmation: {exc}"
                    ) from exc
            if now() - started >= self.timeout_s:
                raise ManualStepTimeout(
                    f"no confirmation of the manual step for experiment "
                    f"{self.experiment} of run {self.run_id} within "
                    f"{self.timeout_s:.0f}s. The campaign is paused, not failed: "
                    "nothing was applied on your behalf and nothing measured was "
                    "discarded."
                )
            sleep(self.poll_interval_s)


def confirm_manual_step(
    state_dir: Path, run_id: str, experiment: int, *, responder: str, note: str = ""
) -> Path:
    """Record that a human performed the manual step. This is what the CLI calls.

    Carries no parameters and is never compared against a parked change, because
    it authorises nothing -- it reports. The campaign re-verifies the target
    afterwards regardless (W2-Q5's ladder): "done" is a claim about intent, and
    the gate exists because intent and reality diverge.
    """
    run_dir = Path(state_dir) / "approvals" / run_id
    request = run_dir / f"{experiment:03d}.manual.request.json"
    if not request.exists():
        raise FileNotFoundError(
            f"no manual step is pending for experiment {experiment} of run "
            f"{run_id} (looked in {run_dir})"
        )
    path = run_dir / f"{experiment:03d}.manual.json"
    path.write_text(
        json.dumps(
            {
                "action": "manual_step_done",
                "responder": responder,
                "note": note,
                "confirmed_at_epoch_s": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def pending_manual_steps(state_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Manual steps parked for a run that nobody has confirmed yet."""
    run_dir = Path(state_dir) / "approvals" / run_id
    if not run_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for request in sorted(run_dir.glob("*.manual.request.json")):
        stem = request.name.split(".")[0]
        if (run_dir / f"{stem}.manual.json").exists():
            continue
        try:
            out.append(json.loads(request.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out
