"""The loop: measure, diagnose, propose, approve, apply, re-measure, decide.

Everything else in ``crucible.perf`` is a component; this is the thing that runs.
It is written as an explicit state machine rather than a chain of calls because
the *order* is where the integrity rules live, and an order that is visible can
be reviewed.

The order, and why each step sits where it does:

1. **Refuse a production environment before anything else.** Section 19.1: every
   later guardrail assumes the target can be broken and restored. Checking this
   first means no campaign can get far enough to matter.
2. **Take the deploy-branch lock.** Two campaigns pushing to the same branch
   interleave commits and invalidate both (section 19.10).
3. **Measure a baseline.** Nothing is claimed that was not measured.
4. **Diagnose from the snapshot**, with previously-disproved causes fed back so
   the loop cannot re-propose them (section 14's journal RAG lands in week 3;
   within a single campaign the ruled-out list does the same job).
5. **Guard, then ask a human.** The guard refuses categorically; the human
   decides among what remains. Doing it in the other order would ask an operator
   to approve things that were never permitted.
6. **Apply, commit, deploy, and prove the target is running the new commit**
   before a stopwatch starts (section 19.6).
7. **Re-measure the actual proposed value.** Never an adjacent one, and never the
   prediction (section 4.5). The K3 agent predicted 140 ms and measured 93 ms;
   an "after" figure borrowed from a nearby earlier run would have looked
   near-perfect by coincidence.
8. **Decide against the measured noise floor**, and revert anything that did not
   clear it.

What this module deliberately does not do: score. The scorer is a separate
process that calls no model and reads manifests from disk (section 4.6), so
changing how results are weighed never means re-running an experiment.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .applicator import (
    Applicator,
    ApplyError,
    ApplyResult,
    Change,
    Proposal,
    guard_proposal,
    novel_cause,
)
from .approval import (
    ApprovalGate,
    ApprovalRequest,
    ApprovalTimeout,
    DenyingGate,
    ManualStep,
    ManualStepTimeout,
)
from .collector import COLLECTOR_VERSION
from .deploy import DeployBlocked, Deployer, DeployLog, DeployResult
from .diagnosis import Diagnoser, Diagnosis
from .hooks import HookFailed, HookSet
from .journal import JournalIndex, render_for_prompt
from .profile import TargetProfile
from .runner import LoadResult, Scenario

# Verdicts lived in this module until 26 September 2026, when the journal needed
# them to interpret a manifest and importing the campaign to get them created a
# cycle. Re-exported here so every existing `from .campaign import IMPROVED`
# keeps working. ALL_VERDICTS and DISPROVING_VERDICTS are deliberately not
# re-exported: they are vocabulary ABOUT the verdicts, and a caller iterating
# them should read crucible.perf.verdicts for why ABORTED is not 'disproving'.
from .verdicts import ABORTED, IMPROVED, INCONCLUSIVE, NOT_MEASURED, WORSE

#: Environment kinds a campaign will run against. Anything else is refused, and
#: the list is a *whitelist* on purpose: a new environment kind nobody has
#: thought about should stop a campaign, not be waved through.
RUNNABLE_ENVIRONMENT_KINDS = ("pre-prod", "preprod", "staging", "dev", "test", "lab")


#: How many guard refusals in a row end the campaign (W2-Q4). Refusals do not
#: consume the experiment budget, so something has to stop a model that keeps
#: proposing forbidden properties -- and stopping with the real reason is more
#: useful than letting it exhaust a budget it never spent.
MAX_CONSECUTIVE_REFUSALS = 3


class CampaignRefused(RuntimeError):
    """The campaign will not start. Refusals are configuration errors, not failures."""


class CampaignAborted(RuntimeError):
    """The campaign stopped early. Everything already verified stands."""


class CampaignPaused(RuntimeError):
    """The campaign is paused at an experiment boundary.

    Everything already verified stands. Call ``Campaign.resume()`` to continue
    from where the loop stopped. Only the measurement window that was in-flight
    at the moment of pause is discarded and re-run on resume (section 7).
    """

    def __init__(self, msg: str, state: "dict[str, Any]") -> None:
        super().__init__(msg)
        self.state = state


# ---------------------------------------------------------------------------
# The SLA, read-only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sla:
    """The objective, the environment, and the noise floor. Never agent-writable.

    Loaded from ``config/slo.yaml``, which is a protected path in every profile
    *and* a ``Policy`` memory kind. This class has no setters and no ``save``: the
    absence is the point. An agent that can move its own goalpost passes every
    time (``DESIGN.md`` section 4.4).
    """

    name: str
    endpoint: str
    p99_ms: float
    error_rate_pct: float
    environment_name: str
    environment_kind: str
    target_base_url: str
    noise_p99_spread_pct: float
    noise_measured_on: str = ""
    noise_source: str = ""
    cpu_steal_abort_pct: float = 5.0
    at_load: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> Sla:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise CampaignRefused(
                f"no SLA at {resolved}. A campaign cannot judge a result without one, "
                "and inventing a threshold would be the agent grading itself."
            )
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        objective = data.get("objective") or {}
        environment = data.get("environment") or {}
        noise = data.get("noise") or {}
        contention = data.get("host_contention") or {}
        if "p99_ms" not in objective:
            raise CampaignRefused(f"{resolved} declares no objective.p99_ms")
        if "p99_spread_pct" not in noise:
            raise CampaignRefused(
                f"{resolved} declares no noise.p99_spread_pct. Without a measured "
                "noise floor every improvement looks real, including the ones that "
                "are run-to-run variation."
            )
        return cls(
            name=str(data.get("name", resolved.stem)),
            endpoint=str(objective.get("endpoint", "")),
            p99_ms=float(objective["p99_ms"]),
            error_rate_pct=float(objective.get("error_rate_pct", 1.0)),
            environment_name=str(environment.get("name", "")),
            environment_kind=str(environment.get("kind", "")).strip().lower(),
            target_base_url=str(environment.get("target_base_url", "")),
            noise_p99_spread_pct=float(noise["p99_spread_pct"]),
            noise_measured_on=str(noise.get("measured_on", "")),
            noise_source=str(noise.get("source", "")),
            cpu_steal_abort_pct=float(contention.get("cpu_steal_abort_pct", 5.0)),
            at_load=dict(objective.get("at_load") or {}),
            source_path=resolved,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_path"] = str(self.source_path) if self.source_path else None
        return payload

    def met_by(self, p99_ms: float | None, error_rate_pct: float | None) -> bool | None:
        """Whether a measurement meets the objective. ``None`` when unmeasured.

        Three-valued on purpose. A missing measurement is not a failure and not a
        pass; principle 1 says verified and unverified must never look identical
        in a results table, and returning ``False`` here would collapse them.
        """
        if p99_ms is None:
            return None
        if p99_ms > self.p99_ms:
            return False
        if error_rate_pct is not None and error_rate_pct > self.error_rate_pct:
            return False
        return True


def check_environment(sla: Sla) -> None:
    """Refuse to run against production. Called before anything else happens."""
    kind = sla.environment_kind
    if not kind:
        raise CampaignRefused(
            f"environment {sla.environment_name or '(unnamed)'} declares no kind. "
            "Crucible edits configuration on a running service and restarts it; "
            "it runs only against an environment that has explicitly said it is "
            "safe to break (DESIGN.md 8). Declare environment.kind."
        )
    if kind == "production" or kind == "prod":
        raise CampaignRefused(
            f"environment {sla.environment_name!r} is declared {kind!r}. Crucible "
            "never runs against production: every guardrail in DESIGN.md 19 "
            "assumes the target can be broken and restored (19.1)."
        )
    if kind not in RUNNABLE_ENVIRONMENT_KINDS:
        raise CampaignRefused(
            f"environment kind {kind!r} is not one Crucible recognises "
            f"({', '.join(RUNNABLE_ENVIRONMENT_KINDS)}). An unrecognised kind is "
            "refused rather than assumed safe."
        )


# ---------------------------------------------------------------------------
# Verdict arithmetic. No model, no I/O -- testable on numbers alone.
# ---------------------------------------------------------------------------


def relative_change_pct(before: float, after: float) -> float:
    """Signed percentage change. Negative means faster, which is the improvement."""
    if before == 0:
        return 0.0
    return 100.0 * (after - before) / before


def margin_over_noise(
    before_p99_ms: float | None,
    after_p99_ms: float | None,
    noise_pct: float,
) -> float | None:
    """How many noise floors the measured move cleared. ``None`` if unmeasured.

    Operator decision W2-Q2, 18 September 2026. The verdict alone cannot tell a
    win that barely cleared the floor from one that cleared it tenfold, and on
    this environment the difference is two milliseconds: with a 2.08% floor on a
    98 ms baseline, "indistinguishable from noise" ends at 96 ms and "confidently
    real" starts at 94 ms. The three baseline runs that produced the floor were
    98, 98 and 96 -- one of them, with nothing changed, already read 96.

    So a result between 1x and 2x is IMPROVED but marginal, and that is the band
    where repeats earn their wall clock. Recorded as a number rather than as a
    new verdict value: the number carries more than a label, and a new verdict is
    one every reader and the scorer would have to learn.
    """
    if before_p99_ms is None or after_p99_ms is None or not noise_pct:
        return None
    return abs(relative_change_pct(before_p99_ms, after_p99_ms)) / noise_pct


def verdict_for(
    before_p99_ms: float | None,
    after_p99_ms: float | None,
    noise_pct: float,
) -> tuple[str, str]:
    """Compare two measured p99s against the environment's noise floor.

    Returns ``(verdict, why)``. The noise floor is what makes this honest: on the
    Oracle box three identical runs spread 2.08%, so a 1.5% "improvement" is not
    one. Reporting it as a win is how an agent accumulates a record of successes
    it did not earn -- the same failure mode as the LUCKY quadrant in section 4.7,
    reached by arithmetic instead of by reasoning.
    """
    if before_p99_ms is None or after_p99_ms is None:
        return NOT_MEASURED, (
            "one side of the comparison was never measured; verified and unverified "
            "are different outcomes and must not be shown as the same one"
        )
    delta = relative_change_pct(before_p99_ms, after_p99_ms)
    if abs(delta) < noise_pct:
        return INCONCLUSIVE, (
            f"p99 moved {delta:+.2f}% ({before_p99_ms:.0f} -> {after_p99_ms:.0f} ms), "
            f"inside this environment's measured noise floor of {noise_pct:.2f}%. "
            "A change smaller than run-to-run variation is not a result."
        )
    if delta < 0:
        return IMPROVED, (
            f"p99 fell {abs(delta):.2f}% ({before_p99_ms:.0f} -> {after_p99_ms:.0f} ms), "
            f"clear of the {noise_pct:.2f}% noise floor"
        )
    return WORSE, (
        f"p99 rose {delta:.2f}% ({before_p99_ms:.0f} -> {after_p99_ms:.0f} ms), "
        f"clear of the {noise_pct:.2f}% noise floor"
    )


def calibration_error_pct(predicted_ms: float | None, measured_ms: float | None) -> float | None:
    """How far the agent's prediction was from the measurement.

    Tracked, never acted on (section 4.5). It is the input to the calibration
    dimension of the score, and it is computed here rather than by the scorer only
    so that the manifest carries the pair side by side -- a later reader should
    not have to trust that the two numbers came from the same experiment.
    """
    if predicted_ms is None or measured_ms is None or measured_ms == 0:
        return None
    return 100.0 * (predicted_ms - measured_ms) / measured_ms


# ---------------------------------------------------------------------------
# The deploy-branch lock
# ---------------------------------------------------------------------------


@dataclass
class BranchLock:
    """One campaign per deploy branch at a time (section 19.10).

    A directory, not a file, because ``mkdir`` is atomic on both platforms and
    ``open(..., "x")`` has surprising behaviour on network filesystems. The lock
    records who holds it so a human hitting a refusal can find out whether it is
    a live run or a crashed one.
    """

    state_dir: Path
    branch: str
    run_id: str
    _held: bool = False

    @property
    def path(self) -> Path:
        safe = self.branch.replace("/", "_") or "default"
        return Path(self.state_dir) / "locks" / f"{safe}.lock"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            holder = "unknown"
            try:
                holder = (self.path / "holder.json").read_text(encoding="utf-8")
            except OSError:
                pass
            raise CampaignRefused(
                f"deploy branch {self.branch!r} is already locked by another campaign: "
                f"{holder}. Two campaigns pushing to one branch interleave commits and "
                f"invalidate both (DESIGN.md 19.10). Remove {self.path} if that run is dead."
            ) from None
        (self.path / "holder.json").write_text(
            json.dumps({"run_id": self.run_id, "since_epoch_s": time.time()}, indent=2),
            encoding="utf-8",
        )
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            (self.path / "holder.json").unlink(missing_ok=True)
            self.path.rmdir()
        except OSError:
            pass
        self._held = False

    def __enter__(self) -> BranchLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def abort_marker_path(state_dir: Path, run_id: str) -> Path:
    """Where ``crucible abort`` leaves its signal for a running campaign."""
    return Path(state_dir) / "aborts" / f"{run_id}.abort"


def request_abort(state_dir: Path, run_id: str, reason: str = "") -> Path:
    """Ask a running campaign to stop at its next experiment boundary.

    A file rather than a signal, for the same reason approvals are files: the
    campaign and the operator are different processes, often different
    terminals. A boundary rather than an interrupt because section 7 is explicit
    that abort discards the *in-flight* experiment only -- tearing down mid-apply
    would leave the target in a state no manifest describes, which is the one
    outcome worse than not aborting at all.
    """
    path = abort_marker_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"reason": reason, "requested_at_epoch_s": time.time()}, indent=2),
        encoding="utf-8",
    )
    return path


def abort_requested(state_dir: Path, run_id: str) -> str | None:
    """The abort reason if one has been requested, else ``None``."""
    path = abort_marker_path(state_dir, run_id)
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("reason", "")) or "operator abort"
    except (OSError, ValueError):
        return "operator abort"


def pause_marker_path(state_dir: Path, run_id: str) -> Path:
    """Where ``crucible pause`` leaves its signal for a running campaign."""
    return Path(state_dir) / "pauses" / f"{run_id}.pause"


def request_pause(state_dir: Path, run_id: str, reason: str = "") -> Path:
    """Ask a running campaign to pause at its next experiment boundary.

    Unlike abort, pause preserves every verified experiment and allows
    ``Campaign.resume()`` to continue from where it stopped (section 7).
    Only the measurement window that was actively in flight when the pause
    lands is discarded and re-run on resume.
    """
    path = pause_marker_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"reason": reason, "requested_at_epoch_s": time.time()}, indent=2),
        encoding="utf-8",
    )
    return path


def pause_requested(state_dir: Path, run_id: str) -> str | None:
    """The pause reason if one has been requested, else ``None``."""
    path = pause_marker_path(state_dir, run_id)
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("reason", "")) or "operator pause"
    except (OSError, ValueError):
        return "operator pause"


def paused_state_path(state_dir: Path, run_id: str) -> Path:
    """Where the paused loop state lives so ``Campaign.resume()`` can continue."""
    return Path(state_dir) / "pauses" / f"{run_id}.paused.json"


# ---------------------------------------------------------------------------
# Audit log (§13 — append-only, records guard refusals, approvals, applies, reverts)
# ---------------------------------------------------------------------------


class AuditLog:
    """Append-only JSONL record of every irreversible or significant action.

    §13 taxonomy: Audit memory captures guard refusals, approvals, applies, and
    reverts. One line per event, written atomically so a half-written line is
    never left behind. The log survives across campaigns on the same state_dir;
    each record carries the run_id and experiment number so lines can be
    correlated back to the manifest.

    All writes go to ``{state_dir}/audit.jsonl``.  Pass ``state_dir=None`` to
    create a no-op log (useful when a caller does not need persistence, such as
    the event-stream-only path in the NiceGUI UI).
    """

    def __init__(self, state_dir: "Path | None", run_id: str) -> None:
        self._path = (
            Path(state_dir) / "audit.jsonl" if state_dir is not None else None
        )
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

    def _append(self, kind: str, experiment: "int | None", **fields: object) -> None:
        if self._path is None:
            return
        record = {
            "kind": kind,
            "run_id": self.run_id,
            "experiment": experiment,
            "at_epoch_s": time.time(),
            **fields,
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def guard_refusal(self, experiment: int, reason: str) -> None:
        self._append("guard_refusal", experiment, reason=reason)

    def approval(self, experiment: int, *, approved: bool, reason: str = "") -> None:
        self._append("approval", experiment, approved=approved, reason=reason)

    def apply(self, experiment: int, *, prop: str, old_value: object, new_value: object) -> None:
        self._append("apply", experiment, prop=prop, old_value=old_value, new_value=new_value)

    def revert(self, experiment: int, *, reason: str) -> None:
        self._append("revert", experiment, reason=reason)

    def abort(self, *, reason: str) -> None:
        self._append("abort", None, reason=reason)

    def pause(self, *, reason: str) -> None:
        self._append("pause", None, reason=reason)

    def resume(self) -> None:
        self._append("resume", None)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


@dataclass
class ExperimentManifest:
    """One experiment, in the form the journal and the scorer read.

    Everything a later reader needs to decide whether to trust the number is on
    here, including the things that would be embarrassing: which model actually
    served the diagnosis, whether a human intervened, which commit was running,
    and what the verdict was judged against.
    """

    run_id: str
    experiment: int
    started_at_epoch_s: float
    collector_version: str = COLLECTOR_VERSION
    cause_family: str = ""
    #: False when the agent named a cause the profile has not declared
    #: (``DESIGN.md`` section 5). Permitted, because no list enumerated in
    #: advance survives contact with real services -- and recorded, because
    #: invented vocabulary must not accumulate quietly. A signal about the
    #: agent, in the same family as a guard refusal: worth counting, worth
    #: showing, never worth acting on by itself.
    cause_family_declared: bool = True
    proposal: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    apply_result: dict[str, Any] | None = None
    deploy: dict[str, Any] | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    verdict: str = NOT_MEASURED
    verdict_reason: str = ""
    kept: bool = False
    sla_met_before: bool | None = None
    sla_met_after: bool | None = None
    predicted_p99_ms: float | None = None
    calibration_error_pct: float | None = None
    noise_floor_pct: float | None = None
    #: How many noise floors the measured move cleared (W2-Q2). 1.0-2.0 is a
    #: marginal win; below 1.0 is INCONCLUSIVE by definition.
    margin_over_noise: float | None = None
    #: Set when the guard refused the proposal. Such an experiment is recorded
    #: (it is evidence about the agent) but does not consume a slot (W2-Q4).
    refused_by_guard: bool = False
    deployed_commit: str = ""
    manual_steps: list[str] = field(default_factory=list)
    #: The watchdog's record for this experiment's measured window, if one
    #: supervised it (section 6). Carried whether or not anything tripped: the
    #: observed CPU steal margin is on here, and section 6 requires it on every
    #: manifest because a run that stayed under the threshold is not the same
    #: claim as a run where nobody looked.
    watchdog: dict[str, Any] | None = None
    #: What the journal RAG (section 14) handed this diagnosis. Recorded because
    #: which history the agent was shown is part of what produced its answer:
    #: two campaigns reaching different conclusions from the same snapshot is
    #: explained by this field more often than by anything else.
    prior_findings: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CampaignResult:
    """The whole run. What the report and the scorer are built from."""

    run_id: str
    sla: dict[str, Any] = field(default_factory=dict)
    profile: str = ""
    scenario: str = ""
    baseline: dict[str, Any] = field(default_factory=dict)
    experiments: list[ExperimentManifest] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    #: True when the campaign was cut short by an abort signal or watchdog trip,
    #: not by completing its budget or meeting the SLA. Structured so the scorer
    #: and the report can treat aborted runs differently without parsing the free
    #: text in stopped_reason.
    aborted: bool = False
    abort_reason: str = ""
    #: True when the campaign was paused at an experiment boundary (§7). A paused
    #: run is resumable; an aborted one is not.
    paused: bool = False
    pause_reason: str = ""
    models_used: list[str] = field(default_factory=list)
    started_at_epoch_s: float = field(default_factory=time.time)
    finished_at_epoch_s: float | None = None

    @property
    def spans_multiple_models(self) -> bool:
        """Whether experiments in this campaign were diagnosed by different models.

        Section 3.2 permits a budget-driven downgrade but requires it be visible:
        a campaign whose experiments were diagnosed by different models is not
        internally comparable, and the report must flag that rather than
        averaging across it.
        """
        return len({m for m in self.models_used if m}) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sla": self.sla,
            "profile": self.profile,
            "scenario": self.scenario,
            "collector_version": COLLECTOR_VERSION,
            "baseline": self.baseline,
            "experiments": [e.as_dict() for e in self.experiments],
            "ruled_out": self.ruled_out,
            "stopped_reason": self.stopped_reason,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "models_used": self.models_used,
            "spans_multiple_models": self.spans_multiple_models,
            "comparability_warning": (
                "Experiments in this campaign were diagnosed by more than one model. "
                "They are not directly comparable with each other (DESIGN.md 3.2)."
                if self.spans_multiple_models else ""
            ),
            "started_at_epoch_s": self.started_at_epoch_s,
            "finished_at_epoch_s": self.finished_at_epoch_s,
        }

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# The campaign
# ---------------------------------------------------------------------------


@dataclass
class Campaign:
    """One investigation, start to finish.

    The collaborators are all injected. That is what makes the loop testable
    without a cloud box: a fake measure function, a scripted diagnoser and a
    pre-approving gate exercise every branch of the state machine, and the
    integrity rules are then asserted on the manifest rather than on a log.
    """

    profile: TargetProfile
    sla: Sla
    scenario: Scenario
    applicator: Applicator
    diagnoser: Diagnoser
    #: ``(scenario, run_id) -> (LoadResult, snapshot)``. Injected because the real
    #: one runs Locust for eight minutes and the tests must not.
    measure: Any
    approval_gate: ApprovalGate = field(default_factory=DenyingGate)
    deployer: Deployer | None = None
    max_experiments: int = 5
    run_id: str = ""
    state_dir: Path = field(default_factory=lambda: Path(os.getenv("CRUCIBLE_STATE_DIR", Path.home() / ".crucible")))
    results_dir: Path = field(default_factory=lambda: Path("results"))
    #: Revert a change whose effect could not be distinguished from noise. On by
    #: default: an unproven change left in place becomes the next experiment's
    #: baseline, so the campaign would then be measuring against something nobody
    #: verified. Turning it off is a deliberate choice to accumulate changes.
    revert_on_inconclusive: bool = True
    #: W2-Q8. When true a manual step pauses and waits for the operator to
    #: confirm; when false it aborts. Pausing is the section 11 behaviour and the
    #: default; aborting stays available for an unattended run where nobody is
    #: going to answer and an hour of waiting helps no one.
    wait_for_manual_steps: bool = True
    manual_step_timeout_s: float = 3600.0
    deploy_log: DeployLog = field(default_factory=DeployLog)
    #: Zero-arg async callable warming the gateway before the first diagnosis
    #: call, typically ``GatewayClient.warm_up``. Optional so a scripted test
    #: campaign, which never calls a real gateway, need not supply one.
    warm_up_gateway: Any = None
    #: The journal RAG (section 14): what EARLIER campaigns measured on this
    #: target, fed to diagnosis so the loop does not spend an experiment
    #: re-learning something already paid for. ``None`` loads it from
    #: ``results_dir``; pass an empty ``JournalIndex()`` to run without history,
    #: which is what a test wanting one variable at a time does.
    #:
    #: Loaded ONCE, before the baseline. A campaign writes its own manifest at
    #: the end, so re-reading the directory mid-run would be stable in practice
    #: and confusing in principle -- history should mean "before this campaign",
    #: not "whatever is on disk at the moment I looked".
    journal: JournalIndex | None = None
    #: The second lock on the SLA (DESIGN.md 4.4, AGENTS.md non-negotiable 4).
    #: When present the SLA is written here as a POLICY record at campaign start.
    #: The store refuses any later write from an agent-role principal, making the
    #: goalpost immovable even if the file guard is somehow bypassed.
    #: ``None`` skips the write -- acceptable only for tests, never for live runs.
    memory_store: Any = None
    #: Free text from whoever raised the investigation (B3 / D1). Framed as
    #: context, not authority: the model sees it in a "a stakeholder asks:"
    #: block and knows the SLA and measurements are the authority, not this.
    #: Never overrides the SLA; never survives into scored output as a fact.
    stakeholder_request: str = ""
    #: Per-experiment hooks (§10). before_each and after_each gate each
    #: experiment; on_abort fires when the campaign is cut short. A failing
    #: before_each or after_each raises HookFailed, which becomes a
    #: CampaignAborted — it blocks the experiment rather than silently
    #: continuing with a corrupt database state.
    hooks: HookSet = field(default_factory=HookSet)
    #: §11 manual DB reset. When non-empty the campaign blocks before each
    #: experiment with these instructions and waits for the operator to confirm
    #: rather than running an automated reset. Use this when no ``before_each``
    #: hook is available but experiments need a clean database state.
    #: The step is recorded on the manifest — a run with human intervention is
    #: not comparable to a fully autonomous one (§11).
    manual_db_reset_instructions: str = ""
    #: §11 manual revert. When True and a change must be undone, the campaign
    #: blocks with instructions for the operator to perform the revert by hand
    #: rather than auto-applying the inverse change. Recorded on the manifest.
    manual_revert: bool = False
    #: Optional callback fired at key moments in the loop. The NiceGUI live view
    #: wires an asyncio.Queue.put_nowait here. Each call receives one dict with a
    #: "kind" key identifying the event. Never relied on for correctness: a dropped
    #: event changes no behaviour, it only affects what the UI shows.
    on_event: Any = None
    #: Append-only audit log (§13). Created automatically in __post_init__ using
    #: state_dir; pass an explicit AuditLog to redirect or suppress output.
    audit_log: "AuditLog | None" = None

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = time.strftime("run-%Y%m%d-%H%M%S", time.gmtime())
        if self.journal is None:
            self.journal = JournalIndex.load(self.results_dir)
        if self.audit_log is None:
            self.audit_log = AuditLog(self.state_dir, self.run_id)

    def _emit(self, kind: str, **fields: object) -> None:
        """Fire the on_event callback if one is registered. Never raises."""
        if self.on_event is None:
            return
        try:
            self.on_event({"kind": kind, "run_id": self.run_id, **fields})
        except Exception:  # noqa: BLE001
            pass

    # -- the loop ---------------------------------------------------------

    async def run(self) -> CampaignResult:
        """Baseline, then up to ``max_experiments`` diagnose/apply/verify cycles."""
        check_environment(self.sla)

        # Second lock on the SLA (DESIGN.md 4.4). The file guard is the first.
        # Writing as POLICY before anything else runs means an agent-role principal
        # cannot overwrite it during the campaign -- the store refuses the write.
        if self.memory_store is not None:
            from ..core.memory.models import MemoryScope, Principal  # noqa: PLC0415
            from .policy import SlaPolicy, publish_sla  # noqa: PLC0415
            _policy = SlaPolicy.from_sla(self.sla)
            _scope = MemoryScope(
                tenant_id=self.sla.environment_name or "default",
                run_id=self.run_id,
            )
            _principal = Principal(id=f"campaign:{self.run_id}", role="system")
            publish_sla(self.memory_store, _policy, _scope, _principal)

        # `glc_v5` runs on Render's free tier and spins down when idle (DESIGN.md
        # 18); its cold start can take tens of seconds. Paying that cost here,
        # before the baseline is even measured, means it lands on wall clock
        # rather than inside the first diagnosis call the operator is timing.
        if self.warm_up_gateway is not None:
            await self.warm_up_gateway()

        result = CampaignResult(
            run_id=self.run_id,
            sla=self.sla.as_dict(),
            profile=self.profile.name,
            scenario=self.scenario.name,
        )
        lock = BranchLock(
            state_dir=self.state_dir,
            branch=getattr(self.profile.deploy, "branch", "") or "local",
            run_id=self.run_id,
        )
        self._emit("started", sla=self.sla.as_dict(), profile=self.profile.name)
        with lock:
            try:
                await self._run_locked(result)
                self._emit("done", experiments=len(result.experiments), stopped_reason=result.stopped_reason)
            except CampaignAborted as aborted:
                result.stopped_reason = str(aborted)
                result.aborted = True
                result.abort_reason = str(aborted)
                self.audit_log.abort(reason=str(aborted))  # type: ignore[union-attr]
                self._emit("aborted", reason=str(aborted))
                if self.hooks.has_hooks():
                    await self.hooks.run_on_abort()
                self._redeploy_last_good(result)
            except CampaignPaused as paused:
                result.stopped_reason = str(paused)
                result.paused = True
                result.pause_reason = str(paused)
                _state_path = paused_state_path(self.state_dir, self.run_id)
                _state_path.parent.mkdir(parents=True, exist_ok=True)
                _state_path.write_text(
                    json.dumps(paused.state, indent=2, default=str), encoding="utf-8"
                )
                self.audit_log.pause(reason=str(paused))  # type: ignore[union-attr]
                self._emit("paused", reason=str(paused))
            finally:
                result.finished_at_epoch_s = time.time()
                result.write(self.results_dir)
        return result

    async def resume(self) -> "CampaignResult":
        """Continue a paused campaign from where it stopped (section 7).

        Reads the pause state written by the paused run, re-acquires the deploy
        lock, skips the baseline and re-enters the experiment loop from the
        saved position. The result accumulates new experiments on top of what
        the paused run already completed on disk.
        """
        state_file = paused_state_path(self.state_dir, self.run_id)
        if not state_file.exists():
            raise CampaignRefused(
                f"no paused state found for {self.run_id!r}; "
                f"expected {state_file}. Has this run been paused?"
            )
        resume_state: dict[str, Any] = json.loads(state_file.read_text(encoding="utf-8"))
        # Remove the pause marker so the loop does not immediately re-pause.
        _pm = pause_marker_path(self.state_dir, self.run_id)
        if _pm.exists():
            _pm.unlink()

        result = CampaignResult(
            run_id=self.run_id,
            sla=self.sla.as_dict(),
            profile=self.profile.name,
            scenario=self.scenario.name,
        )
        lock = BranchLock(
            state_dir=self.state_dir,
            branch=getattr(self.profile.deploy, "branch", "") or "local",
            run_id=self.run_id,
        )
        self.audit_log.resume()  # type: ignore[union-attr]
        self._emit("resumed")
        with lock:
            try:
                await self._run_locked(result, _resume_state=resume_state)
                state_file.unlink(missing_ok=True)
                self._emit("done", experiments=len(result.experiments), stopped_reason=result.stopped_reason)
            except CampaignAborted as aborted:
                result.stopped_reason = str(aborted)
                result.aborted = True
                result.abort_reason = str(aborted)
                self.audit_log.abort(reason=str(aborted))  # type: ignore[union-attr]
                self._emit("aborted", reason=str(aborted))
                if self.hooks.has_hooks():
                    await self.hooks.run_on_abort()
                self._redeploy_last_good(result)
            except CampaignPaused as paused:
                result.stopped_reason = str(paused)
                result.paused = True
                result.pause_reason = str(paused)
                _state_path = paused_state_path(self.state_dir, self.run_id)
                _state_path.write_text(
                    json.dumps(paused.state, indent=2, default=str), encoding="utf-8"
                )
                self.audit_log.pause(reason=str(paused))  # type: ignore[union-attr]
                self._emit("paused", reason=str(paused))
            finally:
                result.finished_at_epoch_s = time.time()
                result.write(self.results_dir)
        return result

    async def _run_locked(
        self,
        result: CampaignResult,
        _resume_state: "dict[str, Any] | None" = None,
    ) -> None:
        if _resume_state:
            # Resuming from a pause: skip the baseline and restore loop state.
            # The baseline was already measured and is on disk in the paused run's
            # result JSON; we carry it on this result for the report to read.
            result.baseline = _resume_state.get("baseline", {})
            current_snapshot = _resume_state["current_snapshot"]
            current_load = _load_from_summary(
                _resume_state["current_load"],
                LoadResult(run_id=self.run_id, scenario=self.scenario.name),
            )
            number = _resume_state.get("number_prev", 0)
            charged = _resume_state.get("charged_prev", 0)
            consecutive_refusals = _resume_state.get("consecutive_refusals", 0)
        else:
            baseline_load, baseline_snapshot = self.measure(self.scenario, f"{self.run_id}-baseline")
            if baseline_load.aborted:
                # A watchdog abort during the baseline leaves a partial window
                # (section 6). Nothing after this point could be compared against it:
                # every later verdict is a difference FROM the baseline, so a
                # truncated one would silently mis-grade every experiment in the run.
                raise CampaignAborted(
                    f"the baseline measurement was aborted before it completed: "
                    f"{baseline_load.abort_reason}"
                )
            result.baseline = {
                "load": baseline_load.as_load_summary(),
                "snapshot": baseline_snapshot,
                "sla_met": self.sla.met_by(baseline_load.p99_ms, baseline_load.error_rate_pct),
            }
            self._emit(
                "baseline_done",
                p99_ms=baseline_load.p99_ms,
                sla_met=result.baseline["sla_met"],
            )

            if result.baseline["sla_met"]:
                # Nothing to fix. Section 20's ceiling discovery is what answers "how
                # much headroom is there", and it is gated on an explicit operator
                # flag because it changes the load profile -- which the agent may
                # never do on its own (4.4). So this stops here and says so.
                result.stopped_reason = (
                    f"baseline already meets the SLA (p99 {baseline_load.p99_ms:.0f} ms "
                    f"<= {self.sla.p99_ms:.0f} ms). Nothing to diagnose. To find out what "
                    "headroom exists, run a ceiling probe -- an operator-flagged campaign, "
                    "because it changes the load profile."
                )
                return

            current_load = baseline_load
            current_snapshot = baseline_snapshot
            number = 0
            charged = 0
            consecutive_refusals = 0

        # W2-Q4: a guard refusal is never applied and never measured, so it does
        # not consume one of the N experiments the operator asked for -- they
        # asked for N measurements.
        #
        # Two counters, deliberately. `number` identifies the experiment and only
        # ever increases: it names the manifest and the approval files, so
        # reusing it would let two different proposals park at the same
        # `001.request.json` and let one operator decision answer the other.
        # `charged` is what the budget is spent from, and a refusal is refunded
        # there. The consecutive cap is what stops a model looping on forbidden
        # proposals, and it fails with the real reason rather than disguising it
        # as an exhausted budget.
        while charged < self.max_experiments:
            number += 1
            charged += 1
            # Checked at the boundary, never mid-experiment. Aborting between
            # experiments leaves HEAD at the last verified one and the target
            # running it; aborting mid-apply would not (section 7). The same
            # boundary applies to pause: we hold both at the experiment seam so
            # the loop state is always self-consistent when we write it.
            reason = abort_requested(self.state_dir, self.run_id)
            if reason:
                raise CampaignAborted(f"aborted by operator before experiment {number}: {reason}")
            pause_reason = pause_requested(self.state_dir, self.run_id)
            if pause_reason:
                # Undo the pre-increment so that resume re-enters the loop at
                # the same experiment boundary. The budget and experiment counter
                # reflect only completed experiments, not the one we're about to run.
                raise CampaignPaused(
                    f"paused by operator before experiment {number}: {pause_reason}",
                    {
                        "run_id": self.run_id,
                        "paused_at_epoch_s": time.time(),
                        "paused_reason": pause_reason,
                        "number_prev": number - 1,
                        "charged_prev": charged - 1,
                        "consecutive_refusals": consecutive_refusals,
                        "current_snapshot": current_snapshot,
                        "current_load": current_load.as_load_summary(),
                        "baseline": result.baseline,
                        "experiments_count": len(result.experiments),
                    },
                )

            manifest = ExperimentManifest(
                run_id=self.run_id,
                experiment=number,
                started_at_epoch_s=time.time(),
                noise_floor_pct=self.sla.noise_p99_spread_pct,
                before={"load": current_load.as_load_summary(), "snapshot": current_snapshot},
                sla_met_before=self.sla.met_by(current_load.p99_ms, current_load.error_rate_pct),
            )
            result.experiments.append(manifest)
            self._emit("experiment_started", experiment=number, p99_before=current_load.p99_ms)

            if self.hooks.has_hooks():
                try:
                    hook_result = await self.hooks.run_before_each(number)
                    manifest.notes.append(
                        f"before_each ({hook_result.duration_s:.2f}s): ok"
                    )
                except HookFailed as hf:
                    manifest.notes.append(f"before_each failed: {hf}")
                    raise CampaignAborted(f"before_each hook failed at experiment {number}: {hf}") from hf

            if self.manual_db_reset_instructions:
                step = ManualStep(
                    run_id=self.run_id,
                    experiment=number,
                    instructions=self.manual_db_reset_instructions,
                    state_dir=self.state_dir,
                    timeout_s=self.manual_step_timeout_s,
                )
                try:
                    confirmation = step.wait()
                    manifest.manual_steps.append(
                        "DB reset confirmed by " + str(confirmation.get("responder", "unknown"))
                    )
                    manifest.notes.append(
                        "DESIGN.md §11: manual DB reset step recorded on this manifest; "
                        "this run is not comparable to a fully autonomous one"
                    )
                except ManualStepTimeout as timeout:
                    manifest.verdict = NOT_MEASURED
                    manifest.verdict_reason = str(timeout)
                    raise CampaignAborted(str(timeout)) from timeout

            keep_going = await self._one_experiment(result, manifest, current_snapshot)

            if self.hooks.has_hooks() and not manifest.refused_by_guard:
                try:
                    hook_result = await self.hooks.run_after_each(number)
                    manifest.notes.append(
                        f"after_each ({hook_result.duration_s:.2f}s): ok"
                    )
                except HookFailed as hf:
                    manifest.notes.append(f"after_each failed: {hf}")
                    raise CampaignAborted(f"after_each hook failed at experiment {number}: {hf}") from hf

            # W2-Q4. A guard refusal is not an experiment: nothing was applied
            # and nothing measured, so it is given the slot back. The cap below
            # is what stops a model looping on forbidden proposals, and it fails
            # with the real reason rather than "budget exhausted".
            if manifest.refused_by_guard:
                charged -= 1
                consecutive_refusals += 1
                if consecutive_refusals >= MAX_CONSECUTIVE_REFUSALS:
                    result.stopped_reason = (
                        f"the agent could not produce a permitted proposal: "
                        f"{consecutive_refusals} refusals in a row. The experiment "
                        "budget is untouched -- this is a diagnosis failure, not an "
                        "exhausted campaign."
                    )
                    return
                continue
            consecutive_refusals = 0

            if manifest.kept and manifest.after.get("load"):
                # The kept change becomes the baseline for the next experiment,
                # because it is now what the service is actually running.
                current_snapshot = manifest.after.get("snapshot") or current_snapshot
                current_load = _load_from_summary(manifest.after["load"], current_load)

            if manifest.sla_met_after:
                result.stopped_reason = (
                    f"SLA met after experiment {number}: p99 "
                    f"{manifest.after.get('load', {}).get('p99_ms')} ms"
                )
                return
            if not keep_going:
                return

        result.stopped_reason = f"reached the experiment ceiling of {self.max_experiments}"

    async def _one_experiment(
        self,
        result: CampaignResult,
        manifest: ExperimentManifest,
        snapshot: dict[str, Any],
    ) -> bool:
        """One diagnose/approve/apply/verify cycle. Returns whether to continue."""
        # Section 14. The filter is on exact tokens -- this profile, this
        # scenario -- because a finding about a FastAPI target says nothing about
        # a JVM one, and feeding it across would have the agent eliminate a cause
        # on evidence from a different runtime. That is the 4.3 failure arriving
        # through the history instead of through the snapshot.
        prior = (self.journal or JournalIndex()).prior_findings(
            profile=self.profile.name,
            scenario=self.scenario.name,
            exclude_run_id=self.run_id,
        )
        if prior:
            manifest.notes.append(
                f"diagnosis saw {len(prior)} prior finding(s) from earlier campaigns "
                "on this profile and scenario"
            )
        diagnosis: Diagnosis = await self.diagnoser.diagnose(
            snapshot,
            self.sla.as_dict(),
            ruled_out=tuple(result.ruled_out),
            prior_findings=render_for_prompt(prior),
            stakeholder_request=self.stakeholder_request,
        )
        # Recorded on the manifest, not just used. Which history a diagnosis was
        # given is part of what produced it, and a later reader comparing two
        # campaigns needs to know that one of them had been told about the other.
        manifest.prior_findings = [f.as_dict() for f in prior]
        manifest.diagnosis = diagnosis.as_dict()
        manifest.cause_family = diagnosis.proposal.cause_family
        invented = novel_cause(self.profile, diagnosis.proposal)
        manifest.cause_family_declared = not invented
        if invented:
            manifest.notes.append(
                f"the agent named {invented!r}, which profile {self.profile.name!r} "
                "does not declare. Permitted (DESIGN.md 5) and recorded: the change "
                "was still bounded by allowed_properties, approved by a human, and "
                "judged on a re-measurement. Promoting this name into profile.yaml "
                "is a human's decision."
            )
        manifest.proposal = diagnosis.proposal.as_dict()
        manifest.predicted_p99_ms = diagnosis.proposal.predicted_p99_ms
        if diagnosis.model:
            result.models_used.append(diagnosis.model)

        proposal = diagnosis.proposal
        if proposal.abstained:
            manifest.verdict_reason = f"agent abstained: {proposal.abstain_reason}"
            result.stopped_reason = (
                "the agent declined to propose a change. Abstention on insufficient "
                "evidence is a correct outcome, not a failure (DESIGN.md principle 2)."
            )
            return False

        refusal = guard_proposal(self.profile, proposal)
        if refusal:
            # Recorded and fed back. The next diagnosis sees it in ruled_out, so
            # the model does not spend a second experiment on the same refused idea.
            manifest.refused_by_guard = True
            manifest.verdict_reason = f"guard refused: {refusal}"
            manifest.notes.append(f"refused before approval: {refusal}")
            result.ruled_out.append(f"{proposal.summary()} (refused by guard: {refusal})")
            self.audit_log.guard_refusal(manifest.experiment, reason=refusal)  # type: ignore[union-attr]
            self._emit("guard_refusal", experiment=manifest.experiment, reason=refusal)
            return True

        # Fill in what each property is currently set to, so the card can say
        # "2 -> 20" rather than "None -> 20". Found during the local end-to-end
        # rehearsal: the model does not supply `previous`, and the applicator only
        # resolves it at apply time -- which is AFTER the operator has decided.
        # Approving a change without being shown what it changes FROM is most of
        # the way to approving it blind.
        #
        # Display only. The binding surface is the property/value pairs, which are
        # untouched, and the applicator still re-reads `previous` from disk at
        # apply time -- this value could be stale by then, and the revert must use
        # the one that was actually in force.
        proposal = self._with_current_values(proposal)

        request = ApprovalRequest.from_proposal(proposal, self.run_id, manifest.experiment)
        try:
            decision = self.approval_gate.request(request)
        except ApprovalTimeout as timeout:
            raise CampaignAborted(str(timeout)) from timeout
        manifest.approval = decision.as_dict()
        self.audit_log.approval(manifest.experiment, approved=decision.approved, reason=decision.reason)  # type: ignore[union-attr]
        self._emit("approval", experiment=manifest.experiment, approved=decision.approved, reason=decision.reason)
        if not decision.approved:
            manifest.verdict_reason = f"operator declined: {decision.reason}"
            result.stopped_reason = f"operator declined experiment {manifest.experiment}"
            return False

        apply_result = self.applicator.apply(
            proposal,
            # Subject only. The applicator appends the property/value detail from
            # what it read off disk, so the log records the values that were
            # really in force rather than the ones the proposal assumed.
            commit_message=f"experiment {manifest.experiment} [{self.run_id}] {proposal.cause_family}",
        )
        manifest.apply_result = apply_result.as_dict()
        for _ch in (apply_result.as_dict().get("changes") or []):
            self.audit_log.apply(  # type: ignore[union-attr]
                manifest.experiment,
                prop=_ch.get("prop", ""),
                old_value=_ch.get("previous"),
                new_value=_ch.get("value"),
            )
        self._emit("applying", experiment=manifest.experiment, cause=proposal.cause_family)
        if apply_result.manual_step:
            # A manual restart means the change is written but NOT YET IN FORCE.
            # Carrying on would measure the previous configuration and attribute
            # the numbers to this change -- the same silent error as measuring
            # before a deploy lands (section 19.6), and just as plausible-looking.
            #
            # Found by attempting the cloud run: the local rehearsal used an
            # automated restarter and never reached this branch, and before the
            # fix the loop recorded the manual step and measured anyway.
            #
            # W2-Q8: this PAUSES rather than aborting. Section 11 says the
            # campaign "blocks with instructions rather than failing" and
            # section 7's pause "holds without discarding", so a long campaign is
            # not restarted from its baseline because somebody had to bounce a
            # JVM by hand.
            manifest.manual_steps.append(apply_result.reason)
            if not self.wait_for_manual_steps:
                manifest.verdict = NOT_MEASURED
                manifest.verdict_reason = (
                    "a manual restart is required before the change is in force; "
                    "no measurement may begin until it is"
                )
                raise CampaignAborted(
                    f"experiment {manifest.experiment} needs a manual restart "
                    f"before anything can be measured: {apply_result.reason}"
                )

            step = ManualStep(
                run_id=self.run_id,
                experiment=manifest.experiment,
                instructions=apply_result.reason,
                state_dir=self.state_dir,
                timeout_s=self.manual_step_timeout_s,
            )
            try:
                confirmation = step.wait()
            except ManualStepTimeout as timeout:
                manifest.verdict = NOT_MEASURED
                manifest.verdict_reason = str(timeout)
                raise CampaignAborted(str(timeout)) from timeout

            manifest.manual_steps.append(
                "confirmed by " + str(confirmation.get("responder", "unknown"))
            )
            manifest.notes.append(
                "a human performed the restart; this run is not comparable with a "
                "fully autonomous one (DESIGN.md 11)"
            )

        if apply_result.needs_human:
            raise CampaignAborted(
                f"experiment {manifest.experiment} left the target in a state no "
                f"manifest describes: {apply_result.reason}"
            )
        if not apply_result.applied or apply_result.reverted:
            manifest.verdict_reason = apply_result.reason
            result.ruled_out.append(f"{proposal.summary()} (could not be applied)")
            return True

        deployed = self._deploy(manifest, apply_result)
        if deployed is False:
            return False

        # Re-measure. A fresh run of the ACTUAL proposed value -- never an
        # adjacent one, and never the prediction (section 4.5).
        after_load, after_snapshot = self.measure(
            self.scenario, f"{self.run_id}-exp{manifest.experiment:02d}"
        )
        if after_load.aborted:
            # The watchdog stopped this window part-way through (section 6). The
            # partial statistics are real numbers over a window nobody chose, and
            # judging a change on them would be the K3 class of error again: a
            # plausible figure attributed to something it is not about. The
            # experiment is recorded ABORTED and the campaign stops, which is what
            # takes section 19.9's redeploy of the last good commit.
            manifest.after = {
                "load": after_load.as_load_summary(),
                "snapshot": after_snapshot,
                "partial": True,
                "note": (
                    "the measured window was cut short by the watchdog; these numbers "
                    "cover an arbitrary fraction of it and are evidence about the abort, "
                    "never about the change"
                ),
            }
            manifest.verdict = ABORTED
            manifest.verdict_reason = after_load.abort_reason
            if after_load.watchdog:
                manifest.watchdog = after_load.watchdog
            raise CampaignAborted(
                f"experiment {manifest.experiment}: {after_load.abort_reason}"
            )

        manifest.after = {"load": after_load.as_load_summary(), "snapshot": after_snapshot}
        manifest.sla_met_after = self.sla.met_by(after_load.p99_ms, after_load.error_rate_pct)

        before_p99 = manifest.before.get("load", {}).get("p99_ms")
        manifest.verdict, manifest.verdict_reason = verdict_for(
            before_p99, after_load.p99_ms, self.sla.noise_p99_spread_pct
        )
        manifest.margin_over_noise = margin_over_noise(
            before_p99, after_load.p99_ms, self.sla.noise_p99_spread_pct
        )
        manifest.calibration_error_pct = calibration_error_pct(
            manifest.predicted_p99_ms, after_load.p99_ms
        )

        self._keep_or_revert(manifest, proposal, result)
        self._emit(
            "verdict",
            experiment=manifest.experiment,
            verdict=manifest.verdict,
            kept=manifest.kept,
            p99_after=after_load.p99_ms,
        )
        return True

    def _with_current_values(self, proposal: Proposal) -> Proposal:
        """Return ``proposal`` with each change's ``previous`` read from the file.

        Best-effort: a config file that cannot be read leaves ``previous`` as
        ``None`` and the card degrades to showing only the target value. Raising
        here would turn a display concern into a campaign failure.
        """
        try:
            current = self.applicator.current_values([c.prop for c in proposal.changes])
        except (OSError, ApplyError):
            return proposal
        return replace(
            proposal,
            changes=tuple(
                replace(change, previous=current.get(change.prop))
                for change in proposal.changes
            ),
        )

    # -- steps ------------------------------------------------------------

    def _deploy(self, manifest: ExperimentManifest, apply_result: ApplyResult) -> bool | None:
        """Push and prove the target is running the new commit. ``False`` stops the loop."""
        if self.deployer is None:
            manifest.deployed_commit = apply_result.commit
            manifest.notes.append(
                "no deployer configured; the change was applied to the workspace and "
                "the local restarter is what put it in force"
            )
            return None
        try:
            deploy_result: DeployResult = self.deployer.deploy(apply_result.commit)
        except DeployBlocked as blocked:
            # Section 11: "the campaign blocks with instructions rather than
            # failing". A manual deploy is not a failure condition; it is the
            # declared mode for an environment where automation is not wired up.
            # Aborting here would discard the applied change and all prior
            # measured experiments -- exactly what section 7 says pause avoids.
            manifest.manual_steps.append(str(blocked))
            manifest.deploy = {"manual": True, "reason": str(blocked)}
            if not self.wait_for_manual_steps:
                # Unattended run: nobody is present to perform the step.
                manifest.verdict = NOT_MEASURED
                manifest.verdict_reason = (
                    "a manual deploy is required before measurement can begin; "
                    "re-run with wait_for_manual_steps=True to pause here"
                )
                raise CampaignAborted(
                    f"experiment {manifest.experiment} needs a manual deploy: {blocked}"
                ) from blocked
            step = ManualStep(
                run_id=self.run_id,
                experiment=manifest.experiment,
                instructions=str(blocked),
                state_dir=self.state_dir,
                timeout_s=self.manual_step_timeout_s,
            )
            try:
                confirmation = step.wait()
            except ManualStepTimeout as timeout:
                manifest.verdict = NOT_MEASURED
                manifest.verdict_reason = str(timeout)
                raise CampaignAborted(str(timeout)) from timeout
            manifest.manual_steps.append(
                "deploy confirmed by " + str(confirmation.get("responder", "unknown"))
            )
            manifest.notes.append(
                "DESIGN.md §11: manual deploy step recorded on this manifest; "
                "this run is not comparable to a fully autonomous one"
            )
            # There is no DeployResult here — the operator carried out the
            # deploy, so we have no verified commit sha. Record what we know
            # and continue; the verify-in-force check below still runs.
            manifest.deployed_commit = apply_result.commit
            return None

        self.deploy_log.record(deploy_result)
        manifest.deploy = deploy_result.as_dict()
        manifest.deployed_commit = deploy_result.commit
        if not deploy_result.verified:
            # Section 19.6. Measuring now would attribute the previous
            # configuration's numbers to this change, and the resulting figure
            # would look entirely plausible.
            manifest.verdict = NOT_MEASURED
            manifest.verdict_reason = deploy_result.reason
            raise CampaignAborted(
                f"experiment {manifest.experiment}: {deploy_result.reason}"
            )
        # Rung-2 check (§19.6 ladder): read the changed property back from
        # /actuator/env to prove *this specific change* is in force, not just
        # that the right commit is running. Stronger than a sha; the sha only
        # proves the artifact. A read error falls through to rung 3 (sha), which
        # already passed above — we record the attempt but do not abort.
        if apply_result.changes and self.sla.target_base_url:
            from .deploy import verify_properties_via_env
            prop_ok, prop_detail = verify_properties_via_env(
                apply_result.changes,
                self.sla.target_base_url,
            )
            deploy_result.property_verified = prop_ok
            deploy_result.property_reason = prop_detail
            manifest.deploy = deploy_result.as_dict()
            manifest.notes.append(
                f"property read-back (§19.6 rung 2): {prop_detail}"
            )
        return True

    def _keep_or_revert(
        self, manifest: ExperimentManifest, proposal: Proposal, result: CampaignResult
    ) -> None:
        """Keep a change that beat the noise floor; undo anything else."""
        if manifest.verdict == IMPROVED:
            manifest.kept = True
            return

        if manifest.verdict == INCONCLUSIVE and not self.revert_on_inconclusive:
            manifest.kept = True
            manifest.notes.append(
                "kept despite an inconclusive verdict because revert_on_inconclusive "
                "is off; the next experiment's baseline now includes an unproven change"
            )
            return

        # Put back exactly what was on disk before. Those values were read from
        # the file by the applicator, never taken from the proposal, so a model
        # that misremembered the old value cannot corrupt the revert.
        reverting = Proposal(
            cause_family=proposal.cause_family,
            changes=_changes_from_manifest(manifest),
            reasoning=f"revert: {manifest.verdict_reason}",
        )
        if not reverting.changes:
            manifest.notes.append("nothing recorded to revert")
            return

        if self.manual_revert:
            # §11: operator performs the revert; campaign blocks until confirmed.
            changes_desc = ", ".join(
                f"{c.prop}: {c.value} -> {c.previous}" for c in reverting.changes if c.previous is not None
            )
            instructions = (
                f"Experiment {manifest.experiment} verdict is {manifest.verdict}. "
                f"Please manually revert the following change(s): {changes_desc}. "
                "Then confirm."
            )
            step = ManualStep(
                run_id=self.run_id,
                experiment=manifest.experiment,
                instructions=instructions,
                state_dir=self.state_dir,
                timeout_s=self.manual_step_timeout_s,
            )
            try:
                confirmation = step.wait()
                manifest.manual_steps.append(
                    "manual revert confirmed by " + str(confirmation.get("responder", "unknown"))
                )
                manifest.notes.append(
                    "DESIGN.md §11: manual revert recorded on this manifest; "
                    "this run is not comparable to a fully autonomous one"
                )
            except ManualStepTimeout as timeout:
                manifest.verdict_reason = str(timeout)
                raise CampaignAborted(str(timeout)) from timeout
        else:
            revert_result = self.applicator.apply(
                reverting,
                commit_message=f"revert experiment {manifest.experiment}: {manifest.verdict}",
            )
            manifest.notes.append(f"reverted: {revert_result.reason}")

        manifest.kept = False
        self.audit_log.revert(manifest.experiment, reason=manifest.verdict_reason)  # type: ignore[union-attr]
        self._emit("reverted", experiment=manifest.experiment, reason=manifest.verdict_reason)
        result.ruled_out.append(
            f"{proposal.summary()} -> {manifest.verdict} ({manifest.verdict_reason})"
        )

    def _redeploy_last_good(self, result: CampaignResult) -> None:
        """Section 19.9: once a change has been deployed, abort is not purely local.

        The workspace revert leaves HEAD at the last good experiment, but the box
        is still running the aborted one. An abort that stopped at the local
        revert would leave the environment in a state no manifest describes --
        worse than not aborting, because the next campaign would measure it and
        attribute the result to something else.
        """
        if self.deployer is None:
            return
        last_good = self.deploy_log.last_good_commit
        if not last_good:
            result.stopped_reason += (
                " | no verified commit to roll back to; the target is running whatever "
                "the aborted experiment deployed and needs a human"
            )
            return
        try:
            rollback = self.deployer.deploy(last_good)
            result.stopped_reason += (
                f" | rolled the target back to {last_good[:12]} "
                f"({'verified' if rollback.verified else 'UNVERIFIED - needs a human'})"
            )
        except DeployBlocked as blocked:
            result.stopped_reason += f" | rollback needs a manual deploy: {blocked}"


def _changes_from_manifest(manifest: ExperimentManifest) -> tuple[Change, ...]:
    """The inverse of what was applied: each property back to its recorded previous.

    Read off the manifest rather than out of memory so a revert always restores
    the value that was actually on disk, including on a resumed run where the
    in-memory objects are long gone.

    A property with no recorded ``previous`` is skipped. That is the case where
    the applicator *added* a line the file did not have, and there is no earlier
    value to restore -- writing a guessed default would be worse than leaving the
    added line, which at least appears in the diff the operator reads.
    """
    raw = (manifest.apply_result or {}).get("changes") or []
    return tuple(
        Change(prop=item.get("prop", ""), value=item.get("previous"), unit=item.get("unit", ""))
        for item in raw
        if item.get("prop") and item.get("previous") is not None
    )


def _load_from_summary(summary: dict[str, Any], template: LoadResult) -> LoadResult:
    """Rehydrate a LoadResult from the summary a manifest carries."""
    return LoadResult(
        run_id=template.run_id,
        scenario=template.scenario,
        p50_ms=summary.get("p50_ms"),
        p95_ms=summary.get("p95_ms"),
        p99_ms=summary.get("p99_ms"),
        mean_ms=summary.get("mean_ms"),
        max_ms=summary.get("max_ms"),
        rps=summary.get("rps"),
        request_count=int(summary.get("request_count") or 0),
        failure_count=int(summary.get("failure_count") or 0),
        error_rate_pct=summary.get("error_rate_pct"),
    )
