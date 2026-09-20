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
from .profile import TargetProfile
from .runner import LoadResult, Scenario

#: Environment kinds a campaign will run against. Anything else is refused, and
#: the list is a *whitelist* on purpose: a new environment kind nobody has
#: thought about should stop a campaign, not be waved through.
RUNNABLE_ENVIRONMENT_KINDS = ("pre-prod", "preprod", "staging", "dev", "test", "lab")

#: Verdicts. Strings rather than an enum so a manifest read years later needs no
#: import to be legible.
IMPROVED = "IMPROVED"
WORSE = "WORSE"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_MEASURED = "NOT_MEASURED"

#: How many guard refusals in a row end the campaign (W2-Q4). Refusals do not
#: consume the experiment budget, so something has to stop a model that keeps
#: proposing forbidden properties -- and stopping with the real reason is more
#: useful than letting it exhaust a budget it never spent.
MAX_CONSECUTIVE_REFUSALS = 3


class CampaignRefused(RuntimeError):
    """The campaign will not start. Refusals are configuration errors, not failures."""


class CampaignAborted(RuntimeError):
    """The campaign stopped early. Everything already verified stands."""


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

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = time.strftime("run-%Y%m%d-%H%M%S", time.gmtime())

    # -- the loop ---------------------------------------------------------

    async def run(self) -> CampaignResult:
        """Baseline, then up to ``max_experiments`` diagnose/apply/verify cycles."""
        check_environment(self.sla)

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
        with lock:
            try:
                await self._run_locked(result)
            except CampaignAborted as aborted:
                result.stopped_reason = str(aborted)
                self._redeploy_last_good(result)
            finally:
                result.finished_at_epoch_s = time.time()
                result.write(self.results_dir)
        return result

    async def _run_locked(self, result: CampaignResult) -> None:
        baseline_load, baseline_snapshot = self.measure(self.scenario, f"{self.run_id}-baseline")
        result.baseline = {
            "load": baseline_load.as_load_summary(),
            "snapshot": baseline_snapshot,
            "sla_met": self.sla.met_by(baseline_load.p99_ms, baseline_load.error_rate_pct),
        }

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
        number = 0
        charged = 0
        consecutive_refusals = 0
        while charged < self.max_experiments:
            number += 1
            charged += 1
            # Checked at the boundary, never mid-experiment. Aborting between
            # experiments leaves HEAD at the last verified one and the target
            # running it; aborting mid-apply would not (section 7).
            reason = abort_requested(self.state_dir, self.run_id)
            if reason:
                raise CampaignAborted(f"aborted by operator before experiment {number}: {reason}")

            manifest = ExperimentManifest(
                run_id=self.run_id,
                experiment=number,
                started_at_epoch_s=time.time(),
                noise_floor_pct=self.sla.noise_p99_spread_pct,
                before={"load": current_load.as_load_summary(), "snapshot": current_snapshot},
                sla_met_before=self.sla.met_by(current_load.p99_ms, current_load.error_rate_pct),
            )
            result.experiments.append(manifest)

            keep_going = await self._one_experiment(result, manifest, current_snapshot)

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
        diagnosis: Diagnosis = await self.diagnoser.diagnose(
            snapshot, self.sla.as_dict(), ruled_out=tuple(result.ruled_out)
        )
        manifest.diagnosis = diagnosis.as_dict()
        manifest.cause_family = diagnosis.proposal.cause_family
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
            manifest.manual_steps.append(str(blocked))
            manifest.deploy = {"manual": True, "reason": str(blocked)}
            raise CampaignAborted(
                f"experiment {manifest.experiment} needs a manual deploy: {blocked}"
            ) from blocked

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
        revert_result = self.applicator.apply(
            reverting,
            commit_message=f"revert experiment {manifest.experiment}: {manifest.verdict}",
        )
        manifest.notes.append(f"reverted: {revert_result.reason}")
        manifest.kept = False
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
