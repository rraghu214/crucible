"""Getting an approved change onto the target box, and proving it arrived.

``DESIGN.md`` §19. Crucible's sandbox is not where performance is measured: the
agent edits configuration in its own workspace, the target runs on a separate
pre-prod box, and the change reaches that box by being pushed to a dedicated
branch and deployed from it. Without this there is no autonomous loop at all.

Four rules from §19 are load-bearing and are implemented here rather than
documented and hoped for:

**§19.2 — the refspec is configuration, never model output.** The model decides
*whether* to deploy; it never decides *where*. Remote and branch come from
``profile.yaml``, which is a protected path. This is why ``git push`` is not in
``crucible.coding.exec``'s allowlist: that list governs argv the model composes,
and the risk was never that push exists — pushing to a sandbox branch on pre-prod
is reversible — but that a model could compose ``HEAD:main`` or ``--force``.

**§19.3 — force-push is refused, always.** Reverting means deploying an earlier
commit, never rewriting history. A force-push would destroy the experiment
history that the journal and every manifest depend on.

**§19.5 — automation is declared, never guessed.** An agent that guessed wrong
would either stall a working pipeline or silently skip a deploy that never
happened.

**§19.6 — no measurement begins until the target proves it is running the new
commit.** Measuring early attributes the old configuration's numbers to the new
change: a plausible-looking number that is simply wrong, and nothing downstream
would catch it. This was not hypothetical during the K1 cloud run — a full set of
measurements was taken against a target that had never been restarted, and was
caught only because a pool of 20 cannot cap active connections at 2.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

#: Argv fragments that rewrite history or retarget the push. Refused outright:
#: none of them can appear in a deploy this module builds, and their presence in
#: a configured command means the configuration itself is wrong.
FORBIDDEN_PUSH_ARGS = (
    "--force", "-f", "--force-with-lease", "--mirror", "--delete",
    "--prune", "--all", "--tags", "--receive-pack", "--exec",
)

#: Branches Crucible will never deploy to, whatever a profile says. Operator
#: decision, 20 September 2026: the agent writes to a dedicated sandbox branch
#: and to nothing else, and the branch the operator works from is never written
#: at all.
#:
#: DESIGN.md 19.8 puts the real control on the remote ("branch protection on the
#: remote keeps it off main"), and that still holds -- but that is a control on a
#: server somebody else configures. This costs nothing and catches a profile typo
#: before it reaches the remote, which is the cheaper place to catch it.
PROTECTED_BRANCH_PATTERNS = ("main", "master", "develop", "trunk", "release/*", "hotfix/*")


class BranchPolicyViolation(RuntimeError):
    """A deploy target names a branch Crucible must never write to."""


def branch_policy_violation(branch: str, base_branch: str) -> str | None:
    """Why this branch pairing is refused, or ``None`` when it is allowed.

    Kept as a free function and called from :class:`DeployTarget`'s constructor
    so the rule attaches to the CONFIGURATION rather than to any one adapter. A
    future Jenkins or Argo deployer gets it without being asked, which is the
    point: a guardrail that lives in `GitPushDeployer` alone is one somebody
    reimplements without it, reading the interface and not the history.
    """
    if not branch:
        return None  # an unconfigured target is manual, not a policy breach
    clean = branch.strip()
    base = (base_branch or "").strip()
    if base and clean == base:
        return (
            f"deploy.branch and deploy.base_branch are both {clean!r}. Crucible "
            "applies changes on a dedicated sandbox branch and never writes to "
            "the branch the operator works from."
        )
    for pattern in PROTECTED_BRANCH_PATTERNS:
        if fnmatch.fnmatch(clean, pattern):
            return (
                f"deploy.branch {clean!r} matches the protected pattern {pattern!r}. "
                "Experiments are applied to a dedicated sandbox branch; a shared "
                "branch would take uncommitted experimental configuration and "
                "would make every experiment's history unreproducible."
            )
    return None


class DeployError(RuntimeError):
    """The deploy could not be performed, or was refused before it started."""


class DeployBlocked(DeployError):
    """A manual step is required. The campaign blocks; it does not fail."""


@dataclass(frozen=True)
class DeployTarget:
    """Where a deploy goes, as declared by the profile. Never model-authored."""

    remote: str = ""
    #: The dedicated sandbox branch. The ONLY branch Crucible ever writes to.
    branch: str = ""
    #: The branch the operator works from. Crucible never writes to it; it is
    #: read once, to create the sandbox branch when that does not yet exist.
    base_branch: str = "main"
    #: "pipeline" (push triggers CI) or "manual" (block with instructions).
    mode: str = "manual"
    instructions: str = ""
    #: Endpoint that reports the commit the target is actually running.
    version_url: str = ""
    #: Key in that endpoint's JSON holding the commit sha.
    version_field: str = "commit"
    verify_timeout_s: float = 300.0
    poll_interval_s: float = 5.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> DeployTarget:
        data = data or {}
        return cls(
            remote=str(data.get("remote", "")),
            branch=str(data.get("branch", "")),
            base_branch=str(data.get("base_branch", "main")),
            mode=str(data.get("mode", "manual")),
            instructions=str(data.get("instructions", "")),
            version_url=str(data.get("version_url", "")),
            version_field=str(data.get("version_field", "commit")),
            verify_timeout_s=float(data.get("verify_timeout_s", 300.0)),
            poll_interval_s=float(data.get("poll_interval_s", 5.0)),
        )

    def __post_init__(self) -> None:
        """Refuse an unsafe branch pairing at construction.

        Validating here rather than at push time means a bad profile fails when
        it is LOADED -- during `crucible plan`, before anything has been measured
        or deployed -- instead of eight minutes into a campaign. It also means
        every adapter inherits the rule, because every adapter takes one of
        these.
        """
        violation = branch_policy_violation(self.branch, self.base_branch)
        if violation:
            raise BranchPolicyViolation(violation)

    @property
    def automated(self) -> bool:
        return self.mode == "pipeline"


@dataclass
class DeployResult:
    """What a deploy did, in the form the manifest records.

    ``manual`` matters on its own: ``DESIGN.md`` §11 says a run with human
    intervention is not comparable to a fully autonomous one, so the manifest has
    to be able to tell them apart rather than recording only that it worked.
    """

    commit: str
    deployed: bool = False
    verified: bool = False
    manual: bool = False
    remote: str = ""
    branch: str = ""
    waited_s: float = 0.0
    observed_commit: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Deployer(Protocol):
    """Git-push-to-CI, SSH, Kubernetes and manual all satisfy this."""

    name: str

    def deploy(self, commit: str) -> DeployResult: ...


def _check_push_command(argv: list[str]) -> None:
    """Refuse a configured command that rewrites history or retargets the push.

    This guards against a bad *configuration*, not a bad model — the model never
    reaches this code path. A profile that had been edited to force-push would
    otherwise destroy the experiment history silently.
    """
    for token in argv:
        for bad in FORBIDDEN_PUSH_ARGS:
            if token == bad or token.startswith(f"{bad}="):
                raise DeployError(
                    f"{bad!r} is refused in a deploy: reverting means deploying an "
                    "earlier commit, never rewriting history (DESIGN.md 19.3)."
                )
        if token.startswith("+"):
            raise DeployError(
                f"a forced refspec ({token!r}) is refused; it rewrites the remote branch"
            )


def read_running_commit(url: str, field_name: str, timeout_s: float = 5.0) -> str | None:
    """Ask the target which commit it is running. ``None`` when it will not say.

    ``None`` is not a failure to be papered over -- it means the target cannot
    prove its version, and §19.6 says a measurement may not begin on that basis.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    value = payload.get(field_name)
    return str(value) if value is not None else None


def same_commit(observed: str | None, wanted: str) -> bool:
    """Whether the target is running the commit we deployed.

    Either side may be abbreviated -- a version endpoint commonly reports a short
    sha while the deploy knows the full one -- so a prefix match in either
    direction counts. Below seven characters an abbreviation is not distinctive
    enough to trust, and an exact match is required instead: matching loosely
    here would mean measuring the wrong build, which is the precise failure
    DESIGN.md 19.6 exists to prevent.
    """
    if not observed or not wanted:
        return False
    a, b = observed.strip(), wanted.strip()
    if len(a) < 7 or len(b) < 7:
        return a == b
    return a.startswith(b) or b.startswith(a)


def await_commit(
    target: DeployTarget,
    commit: str,
    *,
    now: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> tuple[bool, float, str | None]:
    """Block until the target reports ``commit``, or the deadline passes.

    Returns ``(verified, waited_s, last_observed)``. The last observation is
    returned even on failure, because "it is running the previous commit" and
    "it will not tell us anything" need different responses from a human.
    """
    if not target.version_url:
        return False, 0.0, None
    started = now()
    last: str | None = None
    while True:
        last = read_running_commit(url=target.version_url, field_name=target.version_field)
        if same_commit(last, commit):
            return True, now() - started, last
        waited = now() - started
        if waited >= target.verify_timeout_s:
            return False, waited, last
        sleep(target.poll_interval_s)


@dataclass
class ManualDeployer:
    """No automation configured: block with instructions rather than fail.

    ``DESIGN.md`` §11. The campaign stops and waits for a human, and the manifest
    records that it did — a measurement taken after a hand-deploy is still valid,
    but it is not the same claim as one taken by an unattended run.
    """

    target: DeployTarget
    name: str = "manual"

    def deploy(self, commit: str) -> DeployResult:
        raise DeployBlocked(
            f"deploy of {commit} requires a manual step.\n"
            f"{self.target.instructions or 'No instructions recorded in profile.yaml.'}\n"
            "The campaign is paused, not failed. It resumes once the target reports "
            "the new commit."
        )


@dataclass
class GitPushDeployer:
    """Push to a pinned branch and let the configured pipeline deploy it.

    Every argument of the command is built here from the profile. Nothing the
    model produced reaches argv, which is the whole of §19.2: the model chooses
    whether to deploy, and this chooses where.
    """

    target: DeployTarget
    workspace: str = "."
    git_binary: str = "git"
    timeout_s: float = 180.0
    name: str = "git-push"
    _runner: Any = None  # injected in tests; defaults to subprocess

    def _run(self, argv: list[str]) -> tuple[int, str]:
        if self._runner is not None:
            return self._runner(argv)
        completed = subprocess.run(  # noqa: S603 - argv list, no shell, config-built
            argv, cwd=self.workspace, capture_output=True, text=True,
            timeout=self.timeout_s, check=False,
        )
        return completed.returncode, (completed.stderr or completed.stdout or "").strip()

    def push_command(self, commit: str) -> list[str]:
        """The exact argv. Separated so a test can assert on it without pushing."""
        if not self.target.remote or not self.target.branch:
            raise DeployError(
                "deploy.remote and deploy.branch must both be set in profile.yaml; "
                "an unpinned refspec is how a push reaches the wrong branch"
            )
        argv = [
            self.git_binary, "push", self.target.remote,
            f"{commit}:refs/heads/{self.target.branch}",
        ]
        _check_push_command(argv)
        return argv

    def remote_branch_exists(self) -> bool:
        """Whether the sandbox branch is already on the remote."""
        code, output = self._run([
            self.git_binary, "ls-remote", "--heads",
            self.target.remote, f"refs/heads/{self.target.branch}",
        ])
        if code != 0:
            raise DeployError(f"could not query {self.target.remote}: {output[:300]}")
        return bool(output.strip())

    def ensure_branch(self) -> str:
        """Create the sandbox branch from the base branch if it does not exist.

        Returns a one-line note for the manifest. The base branch is READ and
        never written: the sandbox branch is created pointing at whatever the
        base currently is, and every experiment after that lands on the sandbox.

        This is why `base_branch` exists at all. Without it a first campaign
        against a fresh remote would fail on a missing ref, and the obvious fix
        -- "just push to the branch that does exist" -- is precisely the thing
        that must never happen.
        """
        if self.remote_branch_exists():
            return f"{self.target.branch} already exists on {self.target.remote}"

        code, base_sha = self._run([self.git_binary, "rev-parse", self.target.base_branch])
        if code != 0:
            raise DeployError(
                f"cannot create {self.target.branch!r}: base branch "
                f"{self.target.base_branch!r} does not resolve ({base_sha[:200]})"
            )
        argv = [
            self.git_binary, "push", self.target.remote,
            f"{base_sha.strip()}:refs/heads/{self.target.branch}",
        ]
        # Same refusal set as any other push. Creating a branch is still a push,
        # and a --force smuggled into this path would be just as destructive.
        _check_push_command(argv)
        code, output = self._run(argv)
        if code != 0:
            raise DeployError(f"could not create {self.target.branch}: {output[:300]}")
        return (
            f"created {self.target.branch} on {self.target.remote} from "
            f"{self.target.base_branch} at {base_sha.strip()[:12]}"
        )

    def deploy(self, commit: str) -> DeployResult:
        result = DeployResult(
            commit=commit, remote=self.target.remote, branch=self.target.branch
        )
        if not self.target.automated:
            raise DeployBlocked(
                f"profile declares deploy mode {self.target.mode!r}, not 'pipeline'. "
                "Automation is declared, never guessed (DESIGN.md 19.5)."
            )

        # Create the sandbox branch on first use. Never the base branch.
        result.reason = self.ensure_branch()

        code, output = self._run(self.push_command(commit))
        if code != 0:
            result.reason = f"git push failed ({code}): {output[:400]}"
            return result
        result.deployed = True

        verified, waited, observed = await_commit(self.target, commit)
        result.verified, result.waited_s, result.observed_commit = verified, waited, observed
        if not verified:
            result.reason = (
                f"target did not report commit {commit} within "
                f"{self.target.verify_timeout_s:.0f}s (last seen: {observed or 'no answer'}). "
                "No measurement may begin: it would attribute the previous "
                "configuration's numbers to this change (DESIGN.md 19.6)."
            )
        else:
            result.reason = f"target confirmed running {commit} after {waited:.1f}s"
        return result


@dataclass
class DeployLog:
    """Every deploy in a campaign, for the manifest and for revert.

    §19.7: the last good commit must be a recorded fact rather than something
    reconstructed by inference, because §19.9's abort has to redeploy it.
    """

    entries: list[DeployResult] = field(default_factory=list)

    def record(self, result: DeployResult) -> DeployResult:
        self.entries.append(result)
        return result

    @property
    def last_good_commit(self) -> str | None:
        """The most recent commit the target actually confirmed running."""
        for entry in reversed(self.entries):
            if entry.verified:
                return entry.commit
        return None

    @property
    def had_manual_step(self) -> bool:
        return any(e.manual for e in self.entries)

    def as_manifest_entries(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.entries]
