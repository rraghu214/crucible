"""Report and diff assertions -- new group, week 3 (late). DESIGN.md §4.6, §8, §3.2.

REVIEWED AND APPROVED by the operator, 11 October 2026 (with 26.5 revised). See
`docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 26.

Screen 16 names the audience: "the teammate who asks *why did you change the
pool size?*". Such a reader is not hostile but is entitled to be unconvinced, and
the report has to survive being argued with by someone who was not in the room.

Three things carry the weight here:

- **`limits_of_this_result` is derived, never written by hand.** A hand-written
  limitations list goes stale the first time the setup changes and nobody
  notices. Every line comes off the manifest, so a campaign that gained a trace
  provider stops claiming it had none.
- **The report calls no model.** Same rule as the scorer (§4.6), same reason: a
  rendering that could paraphrase could also soften.
- **The diff refuses more than it compares.** §8 requires a diff across
  environments to be flagged rather than silently allowed; every other input to
  EVALUATION.md's claim format gets the same treatment, because a changed
  collector or model is just as disqualifying and far less visible. And no delta
  is computed even when the setups match -- a delta offered "with a warning
  attached" is how a number escapes its caveat and ends up on a slide.
"""

import json

import pytest

from crucible.perf.collector import COLLECTOR_VERSION
from crucible.perf.report import (
    COMPARABILITY_FIELDS,
    ReportError,
    build_report,
    calibration,
    comparability,
    compare,
    final_measurement,
    find_campaign,
    headline,
    limits_of_this_result,
    load_campaign,
    summarise_outcome,
)
from crucible.perf.verdicts import ABORTED, IMPROVED, INCONCLUSIVE, NOT_MEASURED, WORSE


def campaign(**overrides):
    """A campaign that found a real problem and fixed it. Overridden per test."""
    base = {
        "run_id": "run-A",
        "profile": "spring-boot",
        "scenario": "steady",
        "collector_version": COLLECTOR_VERSION,
        "started_at_epoch_s": 1_759_000_000.0,
        "sla": {
            "name": "perflab-db-latency",
            "p99_ms": 120,
            "noise_p99_spread_pct": 2.08,
            "noise_measured_on": "2026-09-12",
            "environment_name": "oracle-ashburn-boxa",
            "environment_kind": "pre-prod",
            "at_load": {"users": 50},
        },
        "baseline": {
            "sla_met": False,
            "load": {"p50_ms": 1200, "p95_ms": 1300, "p99_ms": 1300, "rps": 35},
            "snapshot": {
                "available_evidence": {
                    "metrics": True,
                    "traces": False,
                    "trace_reason": "no trace provider configured",
                    "gauge_sampling": True,
                    "endpoint_breakdown": True,
                }
            },
        },
        "models_used": ["gemini-2.5-flash"],
        "ruled_out": [],
        "stopped_reason": "SLA met after experiment 1",
        "experiments": [kept_experiment()],
    }
    base.update(overrides)
    return base


def kept_experiment(**overrides):
    base = {
        "experiment": 1,
        "cause_family": "connection_pool_exhaustion",
        "verdict": IMPROVED,
        "kept": True,
        "margin_over_noise": 45.2,
        "proposal": {
            "changes": [
                {"prop": "spring.datasource.hikari.maximum-pool-size", "value": 20, "previous": "2"}
            ]
        },
        "verdict_reason": "p99 fell 92.8% (1300 -> 93 ms)",
        "predicted_p99_ms": 140,
        "calibration_error_pct": 50.5,
        "after": {"load": {"p50_ms": 63, "p95_ms": 78, "p99_ms": 93, "rps": 186}},
        "deployed_commit": "9b53cb4aa1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 26.1 Reading a manifest, and refusing what is not one
# ---------------------------------------------------------------------------


def test_a_human_approval_is_counted_from_the_state_the_manifest_stores():
    """Found on the live runs of 10 October 2026: every experiment was approved
    by an operator, and the report said `approvals: 0`, because it read a
    property that is never serialised. Reporting a human-approved run as
    unapproved understates principle 4; the reverse would overstate it."""
    from crucible.perf.report import reproduction

    human = kept_experiment(approval={"state": "approved", "responder": "raghu"})
    auto = kept_experiment(experiment=2, approval={"state": "approved", "responder": "preapproved"})
    refused = kept_experiment(experiment=3, approval={"state": "rejected", "responder": "raghu"})

    repro = reproduction(campaign(experiments=[human, auto, refused]))

    assert repro["approvals"] == 1
    assert repro["preapproved"] == 1


def test_a_replay_result_is_refused_rather_than_rendered_as_an_empty_campaign(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps({"cases": [], "task_set": "v1"}), encoding="utf-8")
    with pytest.raises(ReportError, match="not a campaign manifest"):
        load_campaign(path)


def test_a_missing_manifest_names_what_was_looked_for(tmp_path):
    with pytest.raises(ReportError, match="no manifest at"):
        load_campaign(tmp_path / "absent.json")


def test_the_most_recent_campaign_is_the_default(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps(campaign(run_id="old", started_at_epoch_s=1.0)), encoding="utf-8"
    )
    (tmp_path / "b.json").write_text(
        json.dumps(campaign(run_id="new", started_at_epoch_s=2.0)), encoding="utf-8"
    )
    assert find_campaign(tmp_path)["run_id"] == "new"
    assert find_campaign(tmp_path, "old")["run_id"] == "old"


def test_an_unknown_run_id_is_refused_by_name(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(campaign()), encoding="utf-8")
    with pytest.raises(ReportError, match="run 'nope'"):
        find_campaign(tmp_path, "nope")


# ---------------------------------------------------------------------------
# 26.2 The measurement quoted is the one still in force
# ---------------------------------------------------------------------------


def test_the_result_comes_from_the_last_KEPT_experiment_not_the_last_one():
    """A reverted experiment left the target where it started.

    Quoting its "after" numbers would report a measurement of a configuration
    nobody is running -- a true number about a state that does not exist.
    """
    run = campaign(
        experiments=[
            kept_experiment(),
            kept_experiment(
                experiment=2,
                kept=False,
                verdict=WORSE,
                after={"load": {"p99_ms": 4000, "p50_ms": 9, "p95_ms": 9, "rps": 1}},
            ),
        ]
    )
    assert final_measurement(run)["p99_ms"]["after"] == 93


def test_a_campaign_that_kept_nothing_quotes_no_measurement():
    run = campaign(experiments=[kept_experiment(kept=False, verdict=WORSE)])
    assert final_measurement(run) == {}
    assert "none kept" in headline(run)


def test_a_baseline_that_already_met_the_sla_says_nothing_about_headroom():
    run = campaign(baseline={"sla_met": True, "load": {"p99_ms": 90}}, experiments=[])
    text = headline(run)
    assert "already met the SLA" in text
    assert "headroom" in text


def test_the_headline_names_the_goal_it_was_judged_against():
    assert "120 ms goal" in headline(campaign())


# ---------------------------------------------------------------------------
# 26.3 limits_of_this_result -- the section most products omit
# ---------------------------------------------------------------------------


def test_no_trace_source_is_never_checked_not_eliminated():
    limits = " | ".join(limits_of_this_result(campaign()))
    assert "never checked -- not eliminated" in limits


def test_sampled_traces_are_flagged_because_a_p99_outlier_is_rare():
    run = campaign()
    run["baseline"]["snapshot"]["available_evidence"].update(
        {"traces": True, "trace_sampling_rate_pct": 5}
    )
    limits = " | ".join(limits_of_this_result(run))
    assert "sampled at 5%" in limits
    assert "can be false even with tracing on" in limits


def test_unsampled_gauges_are_declared_as_null_rather_than_zero():
    run = campaign()
    run["baseline"]["snapshot"]["available_evidence"]["gauge_sampling"] = False
    limits = " | ".join(limits_of_this_result(run))
    assert "null rather than zero" in limits


def test_a_single_repeat_means_no_variance_bounds():
    limits = " | ".join(limits_of_this_result(campaign()))
    assert "no variance bounds" in limits


def test_the_noise_floor_and_where_it_was_measured_are_both_stated():
    limits = " | ".join(limits_of_this_result(campaign()))
    assert "2.08% noise floor measured on 2026-09-12" in limits
    assert "property of this box" in limits


def test_a_campaign_spanning_two_models_says_so():
    run = campaign(spans_multiple_models=True)
    assert any("more than one model" in limit for limit in limits_of_this_result(run))


def test_a_manual_step_makes_the_run_not_comparable_with_an_autonomous_one():
    run = campaign(experiments=[kept_experiment(manual_steps=["operator restarted the JVM"])])
    limits = " | ".join(limits_of_this_result(run))
    assert "A human intervened" in limits


def test_a_guard_refusal_is_not_reported_as_an_unverified_experiment():
    """Nothing was applied and nothing deployed.

    Saying it "rests on the deploy pipeline having done what it said" would be
    false: there was no deploy. The refusal is the guardrail working, not a
    measurement that failed.
    """
    run = campaign(
        experiments=[
            kept_experiment(),
            {"experiment": 2, "verdict": NOT_MEASURED, "refused_by_guard": True, "kept": False},
        ]
    )
    limits = " | ".join(limits_of_this_result(run))
    assert "rest on the deploy pipeline" not in limits
    assert "guard refused 1 proposal" in limits


def test_a_genuinely_unverified_experiment_does_say_it_rests_on_the_pipeline():
    run = campaign(
        experiments=[
            kept_experiment(),
            {"experiment": 2, "verdict": ABORTED, "kept": False},
        ]
    )
    assert "rest on the deploy pipeline" in " | ".join(limits_of_this_result(run))


def test_unread_cpu_steal_is_unknown_not_clean():
    run = campaign(
        experiments=[kept_experiment(watchdog={"observed_cpu_steal_pct": None, "cpu_steal_abort_pct": 10.0})]
    )
    limits = " | ".join(limits_of_this_result(run))
    assert "unknown, not clean" in limits


def test_the_steal_margin_is_reported_when_it_was_watched():
    """§6: 4% under a 5% limit and 4% under a 10% limit are different confidences."""
    run = campaign(
        experiments=[kept_experiment(watchdog={"observed_cpu_steal_pct": 4.2, "cpu_steal_abort_pct": 10.0})]
    )
    limits = " | ".join(limits_of_this_result(run))
    assert "4.2% against a 10.0% abort threshold" in limits


def test_a_campaign_with_no_watchdog_says_nothing_watched_it():
    limits = " | ".join(limits_of_this_result(campaign()))
    assert "No watchdog record" in limits


def test_single_instance_percentiles_are_always_declared():
    """Always present: two instances' p99s do not combine into a service p99 (§5)."""
    assert any("single instance" in limit for limit in limits_of_this_result(campaign()))


# ---------------------------------------------------------------------------
# 26.4 Calibration is shown, never acted on
# ---------------------------------------------------------------------------


def test_a_missed_prediction_is_shown_rather_than_hidden():
    """Screen 16: showing it turns the prediction into a tracked signal, not a claim."""
    result = calibration(campaign())
    assert result["predictions"] == 1
    assert result["pairs"][0]["direction"] == "conservative"


def test_one_campaign_is_not_a_calibration_curve():
    assert "not a calibration curve" in calibration(campaign())["note"]


def test_an_experiment_that_predicted_nothing_is_not_counted():
    run = campaign(experiments=[kept_experiment(predicted_p99_ms=None)])
    assert calibration(run)["predictions"] == 0


# ---------------------------------------------------------------------------
# 26.5 The diff refuses more than it compares
# ---------------------------------------------------------------------------


def test_two_identical_setups_are_comparable():
    assert comparability(campaign(), campaign(run_id="run-B")) == []
    assert compare(campaign(), campaign(run_id="run-B")).comparable is True


def test_a_different_environment_is_flagged_not_silently_allowed():
    """§8, the case it names explicitly."""
    other = campaign(run_id="run-B")
    other["sla"] = dict(other["sla"], environment_name="hetzner-cx32")
    differences = comparability(campaign(), other)
    assert any("environment differs" in d for d in differences)


def test_a_different_collector_is_flagged_because_the_arithmetic_changed():
    other = campaign(run_id="run-B", collector_version="0.9.0")
    assert any("collector_version differs" in d for d in comparability(campaign(), other))


def test_a_different_model_is_flagged_per_design_3_2():
    other = campaign(run_id="run-B", models_used=["gemini-2.0-flash"])
    assert any("model differs" in d for d in comparability(campaign(), other))


def test_a_different_sla_is_flagged_because_the_objective_moved():
    other = campaign(run_id="run-B")
    other["sla"] = dict(other["sla"], name="a-looser-goal")
    assert any("sla differs" in d for d in comparability(campaign(), other))


def test_a_different_load_profile_is_flagged():
    """Fewer users is not a fix, and it destroys comparability (§4.4)."""
    other = campaign(run_id="run-B")
    other["sla"] = dict(other["sla"], at_load={"users": 10})
    assert any("at_load differs" in d for d in comparability(campaign(), other))


def test_every_claim_input_is_checked():
    """EVALUATION.md: change any one input and it is a different claim."""
    checked = {name for name, _blocks, _why in COMPARABILITY_FIELDS}
    assert {"environment", "sla", "collector_version", "model", "at_load", "scenario"} <= checked


def test_only_scenario_and_load_profile_block_a_comparison():
    """Operator decision, 11 October 2026. The diff compares the load generator's
    figures. Only a different scenario or load profile changes what those
    figures measure; the rest change the circumstances and are flagged."""
    blocking = {name for name, blocks, _why in COMPARABILITY_FIELDS if blocks}
    assert blocking == {"scenario", "at_load"}


@pytest.mark.parametrize(
    "change",
    [
        {"collector_version": "1.1.0"},
        {"models_used": ["gemini-2.0-flash"]},
        {"profile": "fastapi"},
    ],
)
def test_a_non_blocking_difference_is_compared_and_named(change):
    """A collector bump that only added fields, a different model, a different
    profile: the latency figures still measure the same endpoints at the same
    load, so they are shown, with the difference stated above them."""
    result = compare(campaign(), campaign(run_id="run-B", **change))

    assert result.comparable is True
    assert result.identical_setup is False
    assert result.differences and not result.blocking
    assert "Compared with differences" in result.as_dict()["warning"]


def test_a_different_environment_is_compared_and_named():
    other = campaign(run_id="run-B")
    other["sla"] = dict(other["sla"], environment_name="hetzner-cx32")
    result = compare(campaign(), other)
    assert result.comparable is True
    assert any("environment differs" in d for d in result.differences)


def test_a_collector_difference_says_the_compared_figures_do_not_depend_on_it():
    """The figures compared are load-generator figures (report._load_of). If
    the diff ever starts comparing snapshot fields, this reason becomes false
    and collector_version has to block again."""
    other = campaign(run_id="run-B", collector_version="1.1.0")
    (line,) = comparability(campaign(), other)
    assert "load generator" in line
    assert set(final_measurement(campaign())) <= {"p50_ms", "p95_ms", "p99_ms", "rps", "error_rate_pct"}


def test_a_different_scenario_blocks():
    other = campaign(run_id="run-B", scenario="slow-endpoint")
    result = compare(campaign(), other)
    assert result.comparable is False
    assert any("scenario differs" in d for d in result.blocking)


def test_no_delta_is_computed_even_when_the_setups_match():
    """A delta offered 'with a warning attached' is how a number escapes its caveat."""
    result = compare(campaign(), campaign(run_id="run-B")).as_dict()
    assert "delta" not in result
    assert set(result) >= {"measurement_a", "measurement_b", "comparable", "differences"}


def test_an_incomparable_diff_carries_an_explicit_warning():
    other = campaign(run_id="run-B")
    other["sla"] = dict(other["sla"], at_load={"users": 10})
    result = compare(campaign(), other).as_dict()
    assert result["comparable"] is False
    assert "MUST NOT be read as a before/after pair" in result["warning"]


def test_a_comparable_diff_carries_no_warning():
    assert compare(campaign(), campaign(run_id="run-B")).as_dict()["warning"] == ""


# ---------------------------------------------------------------------------
# 26.6 Outcome shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "experiments,expected",
    [
        ([kept_experiment()], "CHANGE_KEPT"),
        ([kept_experiment(kept=False, verdict=WORSE)], "NO_CHANGE_KEPT"),
        ([kept_experiment(verdict=INCONCLUSIVE)], "CHANGE_KEPT_UNPROVEN"),
        ([kept_experiment(verdict=NOT_MEASURED)], "CHANGE_KEPT_UNVERIFIED"),
    ],
)
def test_the_outcome_shape_distinguishes_kept_from_proven(experiments, expected):
    assert summarise_outcome(campaign(experiments=experiments)) == expected


def test_a_baseline_that_met_the_sla_is_not_one_of_the_outcomes():
    run = campaign(baseline={"sla_met": True, "load": {}}, experiments=[])
    assert summarise_outcome(run) == "NOTHING_TO_FIX"


# ---------------------------------------------------------------------------
# 26.7 Reproduction, and the whole report
# ---------------------------------------------------------------------------


def test_the_report_records_every_input_the_claim_format_names():
    repro = build_report(campaign(), harness_sha="9b53cb4").reproduction
    for key in ("harness", "model", "collector_version", "sla", "environment", "profile"):
        assert repro[key], f"{key} is empty"
    assert repro["failover"].startswith("off")


def test_a_stale_collector_is_surfaced_on_the_report():
    repro = build_report(campaign(collector_version="0.9.0")).reproduction
    assert repro["collector_matches_this_process"] is False


def test_the_report_is_json_serialisable_because_the_ui_reads_it():
    payload = json.loads(json.dumps(build_report(campaign()).as_dict(), default=str))
    assert payload["run_id"] == "run-A"
    assert payload["limits_of_this_result"]


def test_the_report_calls_no_model(monkeypatch):
    """§4.6, asserted the same way the scorer's is: no gateway, no transport."""
    import crucible.gateway as gateway_module

    def explode(*args, **kwargs):
        raise AssertionError("the report must not call a model")

    monkeypatch.setattr(gateway_module.GatewayClient, "chat", explode, raising=False)
    monkeypatch.setattr(gateway_module.GatewayClient, "complete", explode, raising=False)
    report = build_report(campaign())
    assert report.headline
