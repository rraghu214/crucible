"""The report, and the diff between two of them. Reads manifests; calls no model.

Screen 16's stated audience is "the teammate who asks *why did you change the
pool size?*", and everything here is shaped by that one sentence. Such a reader
is not hostile, but they are entitled to be unconvinced, and the report has to
survive being argued with by someone who was not in the room.

**Six sections, and the fifth is the one that matters.**

``headline`` / ``experiments`` / ``measurement`` / ``calibration`` are what any
tool would print. :func:`limits_of_this_result` is the section most products
omit, and it is what makes the rest defensible: no trace source means a slow
downstream call was never *checked* rather than *eliminated*; one repeat means
there are no variance bounds on the number in the headline; a single pinned
instance means service-wide behaviour is untested. This is
``available_evidence`` (section 4.3) surfacing one last time, at the point where
somebody might act on the result.

``reproduction`` is the sixth, and it exists because EVALUATION.md's claim format
takes every one of its fields as an input: change the model and it is a different
claim, change the collector and the old numbers were computed by different
arithmetic. A report that did not record them would be unreproducible the moment
anything moved.

**No model, by construction.** Same rule as the scorer (section 4.6) and for the
same reason: a report is a rendering of what was measured, and a rendering that
could paraphrase could also soften. Every number here is read from a manifest and
formatted; nothing is summarised by anything that could be wrong.

**The diff refuses more than it compares.** Section 8 is explicit that History
"flags a diff across environments rather than silently allowing comparison across
them", and the same applies to every other input to the claim. Two campaigns run
against different SLAs, different collectors, different models or different noise
floors are not two measurements of one thing -- they are two different
experiments, and a table putting them side by side under a "before / after"
heading is a lie told by layout rather than by text. :func:`compare` therefore
leads with what differs about the *setup* and only then shows the numbers, and it
marks the comparison unsafe when the setup moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .collector import COLLECTOR_VERSION
from .verdicts import ABORTED, IMPROVED, INCONCLUSIVE, NOT_MEASURED, WORSE


class ReportError(RuntimeError):
    """A manifest could not be read or does not look like a campaign."""


def load_campaign(path: str | Path) -> dict[str, Any]:
    """One campaign manifest from disk."""
    resolved = Path(path)
    if not resolved.exists():
        raise ReportError(f"no manifest at {resolved}")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"{resolved.name} could not be read: {exc}") from exc
    if not isinstance(data, dict) or "experiments" not in data:
        raise ReportError(
            f"{resolved.name} is not a campaign manifest (no 'experiments' key). "
            "A replay result or a score file is a different shape and would render "
            "as an empty campaign rather than as an error."
        )
    return data


def find_campaign(journal_dir: str | Path, run_id: str = "") -> dict[str, Any]:
    """A campaign by run id, or the most recent one when no id is given."""
    root = Path(journal_dir)
    candidates = []
    for path in sorted(root.glob("*.json")):
        try:
            data = load_campaign(path)
        except ReportError:
            continue
        if run_id and data.get("run_id") != run_id:
            continue
        candidates.append(data)
    if not candidates:
        where = f"run {run_id!r} in {root}" if run_id else f"any campaign in {root}"
        raise ReportError(f"found no {where}")
    candidates.sort(key=lambda c: float(c.get("started_at_epoch_s") or 0.0))
    return candidates[-1]


# ---------------------------------------------------------------------------
# Reading numbers off a manifest, carefully
# ---------------------------------------------------------------------------


def _load_of(block: dict[str, Any] | None) -> dict[str, Any]:
    return ((block or {}).get("load") or {})


def _kept(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in campaign.get("experiments", []) if e.get("kept")]


def final_measurement(campaign: dict[str, Any]) -> dict[str, Any]:
    """Baseline versus the last KEPT experiment's re-run. ``{}`` when unmeasured.

    The last *kept* experiment, never simply the last one. An experiment that was
    reverted left the target where it started, so quoting its "after" numbers as
    the campaign's result would report a measurement of a configuration that is
    no longer in force -- a true number about a state nobody is running.
    """
    baseline = _load_of(campaign.get("baseline"))
    kept = _kept(campaign)
    if not baseline or not kept:
        return {}
    after = _load_of(kept[-1].get("after"))
    if not after:
        return {}
    rows = {}
    for key in ("p50_ms", "p95_ms", "p99_ms", "rps", "error_rate_pct"):
        before, now = baseline.get(key), after.get(key)
        if before is None or now is None:
            continue
        rows[key] = {"before": before, "after": now}
    return rows


def headline(campaign: dict[str, Any]) -> str:
    """One sentence a teammate can read without the rest of the report."""
    experiments = campaign.get("experiments", [])
    kept = _kept(campaign)
    sla = campaign.get("sla") or {}
    target = sla.get("p99_ms")
    measured = final_measurement(campaign)
    p99 = measured.get("p99_ms")

    if (campaign.get("baseline") or {}).get("sla_met"):
        return (
            "The baseline already met the SLA, so nothing was diagnosed and nothing "
            "was changed. This campaign says the service was within its objective "
            "when it was measured; it says nothing about how much headroom there is."
        )
    if not kept:
        return (
            f"{len(experiments)} hypothesis(es) tested, none kept. "
            f"{campaign.get('stopped_reason') or 'The campaign ended without a change that beat the noise floor.'} "
            "Nothing was changed on the target."
        )
    changed = sum(len((e.get("proposal") or {}).get("changes") or []) for e in kept)
    if p99 is None:
        return (
            f"{len(experiments)} hypothesis(es) tested, {len(kept)} kept, "
            f"{changed} line(s) of configuration changed. The p99 comparison is not "
            "available on this manifest, so the result is not quantified here."
        )
    verdict = (
        f"p99 fell from {p99['before']:.0f} ms to {p99['after']:.0f} ms"
        if p99["after"] < p99["before"]
        else f"p99 moved from {p99['before']:.0f} ms to {p99['after']:.0f} ms"
    )
    against = f" against a {target:.0f} ms goal" if target else ""
    return (
        f"{len(experiments)} hypothesis(es) tested, {len(kept)} kept. {verdict}{against}, "
        f"verified by a re-run on the identical load profile. "
        f"{changed} line(s) of configuration changed."
    )


def experiment_lines(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    """Each experiment as a row: what was tried, what happened, and why.

    A guard refusal is included and marked. It is evidence about the agent and
    about the guardrails, and a report that dropped it would show a campaign
    that looked more decisive than it was.
    """
    rows = []
    for experiment in campaign.get("experiments", []):
        proposal = experiment.get("proposal") or {}
        changes = proposal.get("changes") or []
        rows.append(
            {
                "experiment": experiment.get("experiment"),
                "cause_family": experiment.get("cause_family") or "(none named)",
                "kept": bool(experiment.get("kept")),
                "verdict": experiment.get("verdict") or NOT_MEASURED,
                "refused_by_guard": bool(experiment.get("refused_by_guard")),
                "changes": {c.get("prop"): c.get("value") for c in changes if c.get("prop")},
                "previous": {
                    c.get("prop"): c.get("previous") for c in changes if c.get("prop")
                },
                "why": experiment.get("verdict_reason") or "",
                "reasoning": (experiment.get("diagnosis") or {}).get("reasoning", ""),
                "margin_over_noise": experiment.get("margin_over_noise"),
                "manual_steps": experiment.get("manual_steps") or [],
            }
        )
    return rows


def calibration(campaign: dict[str, Any]) -> dict[str, Any]:
    """Predicted against measured, for every experiment that predicted anything.

    Shown rather than hidden, which is screen 16's explicit point: most tools
    hide a missed prediction, and showing it turns the prediction into a tracked
    signal rather than a claim. It is never a decision input (section 4.5) -- the
    verdict came from the measurement, and this section exists so a reader learns
    how much to trust the NEXT prediction.
    """
    pairs = []
    for experiment in campaign.get("experiments", []):
        predicted = experiment.get("predicted_p99_ms")
        measured = _load_of(experiment.get("after")).get("p99_ms")
        if predicted is None or measured is None:
            continue
        pairs.append(
            {
                "experiment": experiment.get("experiment"),
                "predicted_p99_ms": predicted,
                "measured_p99_ms": measured,
                "error_pct": experiment.get("calibration_error_pct"),
                "direction": "conservative" if predicted > measured else "optimistic",
            }
        )
    conservative = sum(1 for p in pairs if p["direction"] == "conservative")
    return {
        "pairs": pairs,
        "predictions": len(pairs),
        "conservative": conservative,
        "note": (
            ""
            if not pairs
            else f"conservative in {conservative} of {len(pairs)} prediction(s) in this campaign. "
            "One campaign is not a calibration curve (EVALUATION.md); treat this as a "
            "sanity check, not as a baseline to grade against."
        ),
    }


def limits_of_this_result(campaign: dict[str, Any]) -> list[str]:
    """What this result does NOT establish. The section most products omit.

    Every line is derived from the manifest rather than written by hand, because
    a hand-written limitations list is one that goes stale the first time the
    setup changes and nobody notices. This is principle 2 -- the agent knows what
    it cannot see -- applied at the point where a human might act on the answer.
    """
    limits: list[str] = []
    baseline_snapshot = (campaign.get("baseline") or {}).get("snapshot") or {}
    evidence = baseline_snapshot.get("available_evidence") or {}

    if evidence and not evidence.get("traces"):
        reason = evidence.get("trace_reason") or "no trace provider configured"
        limits.append(
            f"No trace source ({reason}), so a slow downstream call was never checked "
            "-- not eliminated."
        )
    rate = evidence.get("trace_sampling_rate_pct")
    if evidence.get("traces") and rate is not None and rate < 100:
        limits.append(
            f"Traces were sampled at {rate:g}%. A p99 outlier is by definition rare, so "
            "'no slow spans' can be false even with tracing on."
        )
    if evidence and not evidence.get("gauge_sampling"):
        limits.append(
            "Gauges were not sampled during load, so every gauge peak is null rather "
            "than zero -- no cause was ruled out on a gauge in this campaign."
        )
    if evidence and not evidence.get("endpoint_breakdown"):
        limits.append(
            "No per-endpoint breakdown, so latency could not be attributed to one "
            "endpoint rather than assumed to be spread across them."
        )

    scenario_repeats = (campaign.get("sla") or {}).get("at_load", {}).get("repeats")
    if not scenario_repeats or int(scenario_repeats or 1) <= 1:
        limits.append(
            "One measurement per state. There are no variance bounds on any figure "
            "in this report -- only the environment's noise floor, which is a "
            "property of the box rather than of this run."
        )

    noise = (campaign.get("sla") or {}).get("noise_p99_spread_pct")
    measured_on = (campaign.get("sla") or {}).get("noise_measured_on")
    if noise:
        limits.append(
            f"Judged against a {noise:.2f}% noise floor measured on "
            f"{measured_on or 'an unrecorded date'}. That number is a property of this "
            "box; moving environments means re-measuring it."
        )

    if campaign.get("spans_multiple_models"):
        limits.append(
            "Experiments in this campaign were diagnosed by more than one model, so "
            "they are not directly comparable with each other (DESIGN.md 3.2)."
        )

    # A guard refusal is NOT an unverified experiment. Nothing was applied and
    # nothing deployed, so saying it "rests on the deploy pipeline having done
    # what it said" would be false -- there was no deploy. The refusal is real
    # evidence and is reported in `reproduction` as a refusal, which is what it
    # is: the guardrail working, not a measurement that failed.
    unverified = [
        e
        for e in campaign.get("experiments", [])
        if e.get("verdict") in (NOT_MEASURED, ABORTED) and not e.get("refused_by_guard")
    ]
    if unverified:
        limits.append(
            f"{len(unverified)} experiment(s) were never verified by measurement "
            f"(verdicts: {', '.join(sorted({str(e.get('verdict')) for e in unverified}))}). "
            "Those rest on the deploy pipeline having done what it said."
        )
    novel = sorted(
        {
            str(e.get("cause_family"))
            for e in campaign.get("experiments", [])
            if e.get("cause_family") and e.get("cause_family_declared") is False
        }
    )
    if novel:
        limits.append(
            f"The agent named {len(novel)} cause(s) the profile does not declare "
            f"({', '.join(novel)}). That is permitted (DESIGN.md 5) and the change was "
            "still bounded, approved and re-measured -- but the NAME is the agent's own "
            "and has not been reviewed, so do not read it as an established cause family."
        )
    refused = [e for e in campaign.get("experiments", []) if e.get("refused_by_guard")]
    if refused:
        limits.append(
            f"The guard refused {len(refused)} proposal(s). Those cost no experiment "
            "and changed nothing, but they are part of what the agent tried and are "
            "counted in the refusal total below."
        )

    manual = [e for e in campaign.get("experiments", []) if e.get("manual_steps")]
    if manual:
        limits.append(
            f"A human intervened in {len(manual)} experiment(s). A run with manual "
            "steps is not comparable with a fully autonomous one (DESIGN.md 11)."
        )

    steal = _steal_summary(campaign)
    if steal["observed"] is None and steal["watched"]:
        limits.append(
            "CPU steal was never read during this campaign. On shared vCPU that is "
            "unknown, not clean -- a co-tenant may have been affecting these numbers."
        )
    elif steal["observed"] is not None:
        limits.append(
            f"Peak CPU steal was {steal['observed']:.1f}% against a "
            f"{steal['threshold']}% abort threshold. The margin is part of how much "
            "to trust the figures above."
        )
    if not steal["watched"]:
        limits.append(
            "No watchdog record on this campaign, so nothing observed host contention, "
            "the error-rate trend or load-generator health while it ran."
        )

    limits.append(
        "Load was pinned to a single instance. Percentiles from one instance do not "
        "combine into service-wide percentiles, so service-wide behaviour is untested "
        "(DESIGN.md 5)."
    )
    return limits


def _steal_summary(campaign: dict[str, Any]) -> dict[str, Any]:
    """Observed CPU steal across every experiment that carried a watchdog record."""
    observed: list[float] = []
    threshold: Any = None
    watched = False
    for experiment in campaign.get("experiments", []):
        watchdog = experiment.get("watchdog") or {}
        if not watchdog:
            continue
        watched = True
        value = watchdog.get("observed_cpu_steal_pct")
        if value is not None:
            observed.append(float(value))
        threshold = threshold or watchdog.get("cpu_steal_abort_pct")
    return {
        "observed": max(observed) if observed else None,
        "threshold": threshold,
        "watched": watched,
    }


def reproduction(campaign: dict[str, Any], *, harness_sha: str = "") -> dict[str, Any]:
    """Every input EVALUATION.md's claim format takes. Change one, different claim."""
    sla = campaign.get("sla") or {}
    models = sorted({m for m in (campaign.get("models_used") or []) if m})
    experiments = campaign.get("experiments", [])
    return {
        "run_id": campaign.get("run_id", ""),
        "harness": harness_sha or "(not recorded)",
        "model": ", ".join(models) or "(none recorded)",
        "failover": "off (AGENTS.md non-negotiable 3)",
        "collector_version": campaign.get("collector_version") or COLLECTOR_VERSION,
        "collector_matches_this_process": (
            campaign.get("collector_version") in (None, "", COLLECTOR_VERSION)
        ),
        "sla": sla.get("name", ""),
        "sla_source": sla.get("source_path", ""),
        "environment": f"{sla.get('environment_name', '')} ({sla.get('environment_kind', '')})",
        "profile": campaign.get("profile", ""),
        "scenario": campaign.get("scenario", ""),
        "at_load": sla.get("at_load", {}),
        "noise_floor_pct": sla.get("noise_p99_spread_pct"),
        "experiments": len(experiments),
        "guard_refusals": sum(1 for e in experiments if e.get("refused_by_guard")),
        "approvals": sum(1 for e in experiments if (e.get("approval") or {}).get("approved")),
        "deployed_commits": [e.get("deployed_commit") for e in experiments if e.get("deployed_commit")],
        "prior_findings_shown": sum(len(e.get("prior_findings") or []) for e in experiments),
    }


@dataclass
class Report:
    """A whole report, as data. Rendering to text is separate and dumb."""

    run_id: str
    headline: str = ""
    experiments: list[dict[str, Any]] = field(default_factory=list)
    measurement: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    reproduction: dict[str, Any] = field(default_factory=dict)
    ruled_out: list[str] = field(default_factory=list)
    stopped_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "headline": self.headline,
            "experiments": self.experiments,
            "measurement": self.measurement,
            "calibration": self.calibration,
            "limits_of_this_result": self.limits,
            "reproduction": self.reproduction,
            "ruled_out": self.ruled_out,
            "stopped_reason": self.stopped_reason,
        }


def build_report(campaign: dict[str, Any], *, harness_sha: str = "") -> Report:
    """Assemble the six sections. Pure: no model, no I/O, no clock."""
    return Report(
        run_id=str(campaign.get("run_id", "")),
        headline=headline(campaign),
        experiments=experiment_lines(campaign),
        measurement=final_measurement(campaign),
        calibration=calibration(campaign),
        limits=limits_of_this_result(campaign),
        reproduction=reproduction(campaign, harness_sha=harness_sha),
        ruled_out=list(campaign.get("ruled_out") or []),
        stopped_reason=str(campaign.get("stopped_reason") or ""),
    )


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------

#: Inputs to EVALUATION.md's claim. If any of these differ between two
#: campaigns, they are not two measurements of one thing and the numbers must
#: not be shown side by side as though they were.
COMPARABILITY_FIELDS = (
    ("environment", "the environment, so these ran against different targets"),
    ("sla", "the SLA, so they were judged against different objectives"),
    ("profile", "the target profile, so different properties were permitted"),
    ("scenario", "the scenario, so different load was applied"),
    ("collector_version", "the collector, so the numbers were computed by different arithmetic"),
    ("noise_floor_pct", "the noise floor, so 'a real change' meant different things"),
    ("model", "the model, which DESIGN.md 3.2 says makes results non-comparable"),
    ("at_load", "the load profile, so throughput and latency are not comparable"),
)


def comparability(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """What differs about the SETUP of two campaigns, in plain words.

    Section 8: History flags a diff across environments rather than silently
    allowing comparison across them. This generalises that to every input the
    claim format names -- environment is merely the one that bites first.

    Returns the differences. An empty list means the two are comparable, which is
    a claim worth being able to make positively rather than inferring from the
    absence of a warning.
    """
    left, right = reproduction(a), reproduction(b)
    differences = []
    for key, why in COMPARABILITY_FIELDS:
        if left.get(key) != right.get(key):
            differences.append(f"{key} differs ({left.get(key)!r} vs {right.get(key)!r}) -- {why}")
    return differences


@dataclass
class Comparison:
    """Two campaigns, what differs about their setup, and only then their numbers."""

    run_a: str
    run_b: str
    differences: list[str] = field(default_factory=list)
    measurement_a: dict[str, Any] = field(default_factory=dict)
    measurement_b: dict[str, Any] = field(default_factory=dict)
    outcome_a: str = ""
    outcome_b: str = ""

    @property
    def comparable(self) -> bool:
        return not self.differences

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "comparable": self.comparable,
            "differences": self.differences,
            "warning": (
                ""
                if self.comparable
                else "These campaigns did not measure the same thing. The figures below "
                "are each campaign's own result and MUST NOT be read as a before/after "
                "pair (DESIGN.md 8)."
            ),
            "measurement_a": self.measurement_a,
            "measurement_b": self.measurement_b,
            "outcome_a": self.outcome_a,
            "outcome_b": self.outcome_b,
        }


def summarise_outcome(campaign: dict[str, Any]) -> str:
    """A one-word shape of what happened. Not the scorer's outcome -- that calls
    for ground truth the report does not have (section 4.7)."""
    kept = _kept(campaign)
    if (campaign.get("baseline") or {}).get("sla_met"):
        return "NOTHING_TO_FIX"
    if not kept:
        return "NO_CHANGE_KEPT"
    verdicts = {e.get("verdict") for e in kept}
    if IMPROVED in verdicts:
        return "CHANGE_KEPT"
    if INCONCLUSIVE in verdicts:
        return "CHANGE_KEPT_UNPROVEN"
    if WORSE in verdicts:
        return "CHANGE_KEPT_DESPITE_REGRESSION"
    return "CHANGE_KEPT_UNVERIFIED"


def compare(a: dict[str, Any], b: dict[str, Any]) -> Comparison:
    """Two campaigns, setup differences first.

    Deliberately does not compute a delta between the two p99s. Where the setups
    match, a reader can subtract; where they do not, a delta is the exact thing
    that must not be produced, and offering one "with a warning attached" is how
    a number escapes its caveat and ends up in a slide.
    """
    return Comparison(
        run_a=str(a.get("run_id", "")),
        run_b=str(b.get("run_id", "")),
        differences=comparability(a, b),
        measurement_a=final_measurement(a),
        measurement_b=final_measurement(b),
        outcome_a=summarise_outcome(a),
        outcome_b=summarise_outcome(b),
    )


__all__ = [
    "COMPARABILITY_FIELDS",
    "Comparison",
    "Report",
    "ReportError",
    "build_report",
    "calibration",
    "comparability",
    "compare",
    "experiment_lines",
    "final_measurement",
    "find_campaign",
    "headline",
    "limits_of_this_result",
    "load_campaign",
    "reproduction",
    "summarise_outcome",
]
