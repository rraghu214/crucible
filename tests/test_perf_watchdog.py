"""Watchdog assertions -- new group, week 4. DESIGN.md §6, §20.5, §4.2, §19.9.

REVIEW NEEDED: not yet reviewed by the operator. See
`docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 23 for what each of these is claiming
and why, and for the four judgement calls flagged there.

The watchdog is arithmetic, so almost all of it is assertable on numbers with no
box, no load generator and no clock. That is the point of the module's shape:
the only class that touches the outside world is `LiveReadings`, and everything
that DECIDES anything sits above it as a pure function.

Four groups carry the weight:

- **Unknown is not OK.** A tripwire with nothing to read reports `UNKNOWN`. This
  is §4.2's null-is-not-zero rule moved from the collector to the watchdog, and
  it is the one most likely to be "simplified" away by someone who reads the
  four statuses as three plus an edge case.
- **Suspension on a ceiling probe, and its exact boundary.** The four subject
  tripwires go quiet; the three instrument ones do not. A suspended tripwire
  still carries its reading, because on a ceiling probe that reading is the
  deliverable.
- **Abort does all four things, and says when one of them failed.** Stop load,
  revert the workspace, mark the campaign, keep the last healthy reading.
- **An aborted window never becomes a verdict.** The campaign records `ABORTED`
  and stops, rather than grading a change on a window somebody cut short.
"""

import json
from pathlib import Path

import pytest

from crucible.perf.applicator import Applicator, ApplyError
from crucible.perf.campaign import ABORTED, Sla, abort_requested
from crucible.perf.profile import TargetProfile
from crucible.perf.runner import Scenario
from crucible.perf.scorer import UNREACHABLE, score_outcome
from crucible.perf.watchdog import (
    OK,
    SUSPENDED,
    SUSPENDED_ON_CEILING_PROBE,
    TRIPPED,
    TRIPWIRES,
    UNKNOWN,
    WARN,
    LiveReadings,
    Watchdog,
    WatchdogReading,
    WatchdogThresholds,
    abort_manifest,
    check_error_rate,
    check_error_rate_trend,
    check_host_contention,
    check_latency_ceiling,
    check_load_generator_alive,
    check_target_reachable,
    check_throughput_collapse,
    cpu_steal_pct,
    error_trend_pct_per_check,
    for_scenario,
    read_locust_history,
    read_proc_stat,
    stop_locust,
)

THRESHOLDS = WatchdogThresholds(
    error_rate_pct=0.5,
    cpu_steal_abort_pct=5.0,
    latency_ceiling_ms=70_000.0,
    throughput_collapse_pct=25.0,
)


def healthy_reading(**overrides):
    """A reading on which nothing trips. Overridden one field at a time."""
    base = dict(
        elapsed_s=300.0,
        reachable=True,
        http_status=200,
        error_rate_pct=0.0,
        throughput_rps=15.0,
        p99_ms=120.0,
        load_generator_users=50,
        load_generator_beat_age_s=4.0,
        cpu_steal_pct=1.3,
    )
    base.update(overrides)
    return WatchdogReading(**base)


def statuses(check):
    return {t.name: t.status for t in check.tripwires}


# ---------------------------------------------------------------------------
# 23.1 The seven, and only the seven
# ---------------------------------------------------------------------------


def test_every_check_evaluates_all_seven_tripwires_in_screen_order():
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    check = watchdog.check(healthy_reading())
    assert [t.name for t in check.tripwires] == list(TRIPWIRES)
    assert len(TRIPWIRES) == 7


def test_a_clean_check_is_healthy_and_trips_nothing():
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    check = watchdog.check(healthy_reading())
    assert check.tripped == []
    assert check.aborting is False
    assert check.healthy is True


def test_every_tripwire_reading_carries_its_limit_alongside_its_value():
    """A status without its number cannot be argued with by the person reading it."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    check = watchdog.check(healthy_reading())
    for tripwire in check.tripwires:
        assert tripwire.reading, f"{tripwire.name} reported no reading"


# ---------------------------------------------------------------------------
# 23.2 Unknown is not OK (DESIGN.md §4.2, principle 2)
# ---------------------------------------------------------------------------


def test_an_unread_tripwire_is_unknown_never_ok():
    """The whole reading is empty: nothing observed anything."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS)
    check = watchdog.check(WatchdogReading(elapsed_s=300.0))
    assert set(statuses(check).values()) == {UNKNOWN}
    assert check.aborting is False


def test_unread_cpu_steal_is_unknown_and_says_nobody_looked():
    tripwire = check_host_contention(None, THRESHOLDS)
    assert tripwire.status == UNKNOWN
    assert "nobody looked" in tripwire.detail
    # The threshold it WOULD have been judged against is still reported: §6 says
    # 4% under a 5% limit and 4% under a 10% limit are different claims, and the
    # same is true of "unknown against 5%" and "unknown against 10%".
    assert "5.0%" in tripwire.reading


def test_a_run_that_never_read_steal_says_so_rather_than_reporting_clean():
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS)
    watchdog.check(healthy_reading(cpu_steal_pct=None))
    run = _run_of(watchdog)
    assert run.observed_cpu_steal_pct is None
    assert "unknown, not clean" in run.as_dict()["cpu_steal_note"]


def test_observed_steal_is_recorded_on_a_run_that_never_tripped():
    """§6: a run under the threshold is not the same claim as a run nobody watched."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    watchdog.check(healthy_reading(cpu_steal_pct=1.3))
    watchdog.check(healthy_reading(cpu_steal_pct=4.1))
    payload = _run_of(watchdog).as_dict()
    assert payload["observed_cpu_steal_pct"] == 4.1
    assert payload["cpu_steal_abort_pct"] == 5.0
    assert payload["aborted"] is False


def _run_of(watchdog):
    """The WatchdogRun view of a watchdog driven check-by-check in a test."""
    from crucible.perf.watchdog import WatchdogRun

    return WatchdogRun(
        run_id=watchdog.run_id,
        thresholds=watchdog.thresholds.as_dict(),
        ceiling_probe=watchdog.ceiling_probe,
        checks=watchdog._checks,
    )


# ---------------------------------------------------------------------------
# 23.3 Each tripwire's own arithmetic
# ---------------------------------------------------------------------------


def test_target_reachable_needs_consecutive_failures_not_one():
    """One refused handshake on a shared box is not an outage."""
    assert check_target_reachable(0, THRESHOLDS).status == OK
    assert check_target_reachable(1, THRESHOLDS).status == WARN
    assert check_target_reachable(2, THRESHOLDS).status == WARN
    assert check_target_reachable(3, THRESHOLDS).status == TRIPPED


def test_a_single_recovery_resets_the_unreachable_count():
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    watchdog.check(healthy_reading(reachable=False))
    watchdog.check(healthy_reading(reachable=False))
    watchdog.check(healthy_reading(reachable=True))
    check = watchdog.check(healthy_reading(reachable=False))
    assert statuses(check)["target_reachable"] == WARN


def test_error_rate_trips_above_the_scenario_budget():
    assert check_error_rate(0.71, THRESHOLDS).status == TRIPPED
    assert check_error_rate(0.50, THRESHOLDS).status == WARN
    assert check_error_rate(0.10, THRESHOLDS).status == OK


def test_a_trend_needs_three_points_and_two_is_unknown():
    """Two points are a difference. Calling one a trend is how a watchdog aborts on noise."""
    assert error_trend_pct_per_check([0.1, 0.9]) is None
    assert check_error_rate_trend([0.1, 0.9], THRESHOLDS).status == UNKNOWN


def test_the_error_trend_is_a_least_squares_slope_per_check():
    assert error_trend_pct_per_check([0.1, 0.3, 0.5, 0.7]) == pytest.approx(0.2)
    # The screen's example: +0.19 per check against a +0.15 limit.
    assert check_error_rate_trend([0.10, 0.29, 0.48], THRESHOLDS).status == TRIPPED


def test_one_spiky_check_does_not_carry_a_flat_trend_over_the_line():
    """Least squares rather than last-minus-first, so a single spike cannot decide."""
    flat_then_spike = [0.10, 0.10, 0.10, 0.40]
    assert error_trend_pct_per_check(flat_then_spike) < 0.15
    assert check_error_rate_trend(flat_then_spike, THRESHOLDS).status != TRIPPED


def test_throughput_collapse_is_measured_against_a_reference_not_an_absolute():
    # The screen's reading: 594 tpm against a 900 target is a 34% fall.
    assert check_throughput_collapse(594.0, 900.0, THRESHOLDS).status == TRIPPED
    assert check_throughput_collapse(800.0, 900.0, THRESHOLDS).status == OK


def test_throughput_collapse_is_unknown_until_a_reference_exists():
    """Nothing to compare against is not the same as nothing wrong."""
    assert check_throughput_collapse(594.0, None, THRESHOLDS).status == UNKNOWN


def test_the_reference_throughput_is_the_median_of_the_first_checks():
    """One cold first check must not set the bar for a six-hour run."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_checks=3)
    watchdog.check(healthy_reading(throughput_rps=2.0))  # cold
    watchdog.check(healthy_reading(throughput_rps=15.0))
    watchdog.check(healthy_reading(throughput_rps=16.0))
    assert watchdog._reference() == 15.0


def test_an_explicit_reference_beats_the_observed_one():
    """The campaign's measured baseline rps is a better reference than the first checks."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=20.0)
    watchdog.check(healthy_reading(throughput_rps=2.0))
    assert watchdog._reference() == 20.0


def test_the_latency_ceiling_is_absolute_and_far_above_any_sla():
    """Missing the SLA is what the campaign measures; it must never abort the run."""
    sla_p99 = 120.0
    assert check_latency_ceiling(sla_p99 * 100, THRESHOLDS).status == OK
    assert check_latency_ceiling(28_400.0, THRESHOLDS).status == OK
    assert check_latency_ceiling(70_001.0, THRESHOLDS).status == TRIPPED


def test_a_load_generator_with_no_users_trips_immediately():
    """Zero users means nothing is being measured, whatever the other readings say."""
    tripwire = check_load_generator_alive(0, 1.0, THRESHOLDS)
    assert tripwire.status == TRIPPED
    assert "nothing is being measured" in tripwire.detail


def test_a_silent_load_generator_trips_on_heartbeat_age():
    assert check_load_generator_alive(50, 4.0, THRESHOLDS).status == OK
    assert check_load_generator_alive(50, 61.0, THRESHOLDS).status == TRIPPED


def test_host_contention_trips_on_the_environments_threshold_not_a_constant():
    """§6: 5% default, 10% on Oracle free tier where steal runs 4-6% under load."""
    oracle = WatchdogThresholds(cpu_steal_abort_pct=10.0)
    assert check_host_contention(6.0, THRESHOLDS).status == TRIPPED
    assert check_host_contention(6.0, oracle).status == OK


def test_host_contention_aborts_even_when_the_application_looks_fine():
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    check = watchdog.check(healthy_reading(cpu_steal_pct=7.0))
    assert check.tripped == ["host_contention"]
    assert check.aborting is True


# ---------------------------------------------------------------------------
# 23.4 The ceiling probe (DESIGN.md §6, §20.5)
# ---------------------------------------------------------------------------


def test_a_ceiling_probe_suspends_the_four_subject_tripwires():
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, ceiling_probe=True, reference_rps=900.0
    )
    check = watchdog.check(
        healthy_reading(error_rate_pct=14.2, throughput_rps=100.0, p99_ms=90_000.0)
    )
    by_name = statuses(check)
    for name in SUSPENDED_ON_CEILING_PROBE:
        assert by_name[name] == SUSPENDED, name
    assert check.aborting is False


def test_a_ceiling_probe_keeps_the_instrument_tripwires_armed():
    """Reachability, the load generator and host contention. §6's 'only' list."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, ceiling_probe=True)
    check = watchdog.check(healthy_reading(cpu_steal_pct=9.0))
    assert check.tripped == ["host_contention"]

    watchdog = Watchdog(run_id="run-2", thresholds=THRESHOLDS, ceiling_probe=True)
    check = watchdog.check(healthy_reading(load_generator_users=0))
    assert check.tripped == ["load_generator_alive"]


def test_a_suspended_tripwire_still_carries_its_reading():
    """On a ceiling probe the suspended numbers ARE the deliverable (§20.4)."""
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, ceiling_probe=True, reference_rps=900.0
    )
    check = watchdog.check(healthy_reading(error_rate_pct=14.2))
    suspended = next(t for t in check.tripwires if t.name == "error_rate")
    assert "14.20%" in suspended.reading
    assert "would otherwise be tripped" in suspended.detail


def test_the_probe_intent_is_declared_by_push_beyond_never_inferred():
    """§20.1. expect_possible_failure only tolerates a non-zero load-generator exit."""
    sla = _sla()
    tolerant = Scenario(name="s", host="http://x", expect_possible_failure=True)
    probe = Scenario(name="s", host="http://x", push_beyond=True)
    assert for_scenario(tolerant, sla, "run-1").ceiling_probe is False
    assert for_scenario(probe, sla, "run-1").ceiling_probe is True


# ---------------------------------------------------------------------------
# 23.5 Thresholds come from the SLA, which the agent cannot write
# ---------------------------------------------------------------------------


SLA_YAML = """\
name: watchdog-test
environment:
  name: box-a
  kind: pre-prod
  target_base_url: http://10.0.0.79:8080
objective:
  endpoint: /api/db
  p99_ms: 120
  error_rate_pct: 0.5
noise:
  p99_spread_pct: 2.08
host_contention:
  cpu_steal_abort_pct: 10.0
"""


def _sla(tmp_path=None):
    directory = tmp_path or Path(__file__).parent
    path = Path(directory) / "_watchdog_slo.yaml"
    path.write_text(SLA_YAML, encoding="utf-8")
    try:
        return Sla.load(path)
    finally:
        path.unlink(missing_ok=True)


def test_the_error_budget_and_steal_ceiling_come_from_the_sla(tmp_path):
    sla = _sla(tmp_path)
    thresholds = WatchdogThresholds.from_sla(sla)
    assert thresholds.error_rate_pct == 0.5
    assert thresholds.cpu_steal_abort_pct == 10.0
    assert "config/slo.yaml" in thresholds.source


def test_every_threshold_a_run_was_judged_against_is_recorded_with_it(tmp_path):
    """A verdict whose thresholds are lost cannot be argued with a year later."""
    sla = _sla(tmp_path)
    watchdog = for_scenario(Scenario(name="burst", host="http://x"), sla, "run-1")
    watchdog.check(healthy_reading())
    payload = _run_of(watchdog).as_dict()
    assert payload["thresholds"]["cpu_steal_abort_pct"] == 10.0
    assert payload["thresholds"]["error_rate_pct"] == 0.5


# ---------------------------------------------------------------------------
# 23.6 The adjudicator seam (DESIGN.md §6's one permitted model call)
# ---------------------------------------------------------------------------


def test_a_default_watchdog_calls_no_model_at_all():
    """288 checks at $0.002 would cost more than the campaign it is protecting."""
    watchdog = Watchdog(run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0)
    check = watchdog.check(healthy_reading(error_rate_pct=0.45))  # inside the warn band
    assert check.warned == ["error_rate"]
    assert check.adjudications == []
    assert check.aborting is False


def test_the_adjudicator_is_called_only_on_the_transition_into_warn():
    calls = []

    def adjudicator(check, tripwire):
        calls.append((check.number, tripwire.name))
        return False, "still inside the band; keep measuring"

    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, reference_rps=15.0, adjudicator=adjudicator
    )
    watchdog.check(healthy_reading(error_rate_pct=0.45))
    watchdog.check(healthy_reading(error_rate_pct=0.46))
    watchdog.check(healthy_reading(error_rate_pct=0.47))
    assert calls == [(1, "error_rate")]


def test_an_adjudicated_abort_becomes_a_tripped_wire_and_says_who_decided():
    watchdog = Watchdog(
        run_id="run-1",
        thresholds=THRESHOLDS,
        reference_rps=15.0,
        adjudicator=lambda check, tripwire: (True, "errors are clustered on one endpoint"),
    )
    check = watchdog.check(healthy_reading(error_rate_pct=0.45))
    assert check.tripped == ["error_rate"]
    tripwire = next(t for t in check.tripwires if t.name == "error_rate")
    assert tripwire.detail.startswith("adjudicated:")
    assert check.adjudications[0]["called"] is True


def test_the_adjudication_budget_is_capped_and_the_refusal_is_recorded():
    """A flapping signal must not turn the cheap watchdog into the expensive one."""
    watchdog = Watchdog(
        run_id="run-1",
        thresholds=THRESHOLDS,
        reference_rps=15.0,
        max_adjudications=1,
        adjudicator=lambda check, tripwire: (False, "keep going"),
    )
    watchdog.check(healthy_reading(error_rate_pct=0.45))
    watchdog.check(healthy_reading(error_rate_pct=0.10))  # back to OK
    check = watchdog.check(healthy_reading(error_rate_pct=0.45))  # WARN again
    assert check.adjudications[0]["called"] is False
    assert "budget" in check.adjudications[0]["reason"]


# ---------------------------------------------------------------------------
# 23.7 Supervision
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_supervision_polls_at_the_interval_and_stops_at_the_end_of_the_scenario():
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )
    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(elapsed_s=elapsed),
        duration_s=1800.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert len(run.checks) == 6
    assert run.aborted is False


def test_supervision_stops_the_load_the_moment_a_wire_trips():
    clock = FakeClock()
    stopped = []
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )

    def read(elapsed):
        return healthy_reading(elapsed_s=elapsed, cpu_steal_pct=9.0 if elapsed >= 900 else 1.0)

    run = watchdog.supervise(
        read=read,
        duration_s=86_400.0,
        stop_load=lambda: stopped.append("stopped"),
        now=clock.now,
        sleep=clock.sleep,
    )
    assert run.aborted is True
    assert run.abort.tripwires == ["host_contention"]
    assert run.abort.check_number == 3
    assert stopped == ["stopped"]
    # A 24-hour scenario is not run to completion once the measurement is invalid.
    assert len(run.checks) == 3


# ---------------------------------------------------------------------------
# 23.8 Abort does all four things (the watchdog screen's on-abort list)
# ---------------------------------------------------------------------------


def test_abort_stops_the_load_reverts_the_workspace_and_marks_the_campaign(tmp_path):
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1",
        thresholds=THRESHOLDS,
        interval_s=300.0,
        reference_rps=15.0,
        state_dir=tmp_path,
    )
    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(elapsed_s=elapsed, load_generator_users=0),
        duration_s=3600.0,
        stop_load=lambda: None,
        revert=lambda: "application.properties restored to HEAD",
        now=clock.now,
        sleep=clock.sleep,
    )
    actions = " | ".join(run.abort.actions)
    assert "load generator stopped" in actions
    assert "workspace reverted" in actions
    assert "abort requested of the campaign" in actions
    # The campaign's own boundary check is what takes over -- including §19.9's
    # redeploy of the last good commit, which is deliberately not duplicated here.
    assert abort_requested(tmp_path, "run-1")


def test_an_abort_that_could_not_stop_the_load_says_so_rather_than_claiming_it_did():
    """A manifest implying the box is idle when it is still under load is worse than silence."""
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )

    def refuses_to_stop():
        raise OSError("no such process")

    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(elapsed_s=elapsed, cpu_steal_pct=9.0),
        duration_s=3600.0,
        stop_load=refuses_to_stop,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert any(a.startswith("FAILED to stop") for a in run.abort.actions)


def test_the_last_healthy_reading_is_attached_to_the_abort():
    """The screen's 'you are told, with the last healthy reading attached'."""
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )

    def read(elapsed):
        return healthy_reading(elapsed_s=elapsed, cpu_steal_pct=9.0 if elapsed >= 900 else 1.0)

    run = watchdog.supervise(
        read=read, duration_s=3600.0, now=clock.now, sleep=clock.sleep
    )
    healthy = run.abort.last_healthy_check
    assert healthy is not None
    assert healthy["number"] == 2
    assert healthy["reading"]["cpu_steal_pct"] == 1.0


def test_the_abort_reason_names_every_tripwire_that_fired():
    """Errors AND a throughput collapse together mean something errors alone do not (§6)."""
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=900.0
    )
    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(
            elapsed_s=elapsed, error_rate_pct=0.71, throughput_rps=594.0
        ),
        duration_s=3600.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert run.abort.tripwires == ["error_rate", "throughput_collapse"]
    # Both readings are in the reason, because the conjunction is what says the
    # service is falling over rather than merely running slowly.
    assert "0.71%" in run.abort.reason
    assert "594.0 rps" in run.abort.reason


# ---------------------------------------------------------------------------
# 23.9 The manifest an abort leaves behind
# ---------------------------------------------------------------------------


def test_an_aborted_scenario_writes_a_manifest_with_verdict_aborted_and_the_tripwire():
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )
    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(elapsed_s=elapsed, cpu_steal_pct=9.0),
        duration_s=3600.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    manifest = abort_manifest(run.abort, run, experiment=11)
    assert manifest.verdict == ABORTED
    assert "host_contention" in manifest.verdict_reason
    assert manifest.experiment == 11
    assert manifest.kept is False


def test_the_watchdog_record_survives_serialisation_onto_the_manifest():
    """The scorer reads manifests off disk (§4.6); a field that vanishes in asdict is not there."""
    clock = FakeClock()
    watchdog = Watchdog(
        run_id="run-1", thresholds=THRESHOLDS, interval_s=300.0, reference_rps=15.0
    )
    run = watchdog.supervise(
        read=lambda elapsed: healthy_reading(elapsed_s=elapsed, cpu_steal_pct=9.0),
        duration_s=3600.0,
        now=clock.now,
        sleep=clock.sleep,
    )
    payload = json.loads(json.dumps(abort_manifest(run.abort, run, experiment=11).as_dict()))
    assert payload["watchdog"]["aborted"] is True
    assert payload["watchdog"]["abort"]["tripwires"] == ["host_contention"]
    assert payload["watchdog"]["observed_cpu_steal_pct"] == 9.0


def test_a_watchdog_abort_scores_as_unreachable_not_as_an_honest_failure():
    """EVALUATION.md: an agent whose measurement was invalidated did not fail the task."""
    campaign = {
        "baseline": {"sla_met": False, "snapshot": {"available_evidence": {"metrics": True}}},
        "experiments": [{"verdict": ABORTED, "kept": False}],
        "stopped_reason": "watchdog aborted at check 3: host_contention",
    }
    assert score_outcome(campaign) == UNREACHABLE


# ---------------------------------------------------------------------------
# 23.10 The thin I/O layer
# ---------------------------------------------------------------------------


def test_cpu_steal_is_a_delta_never_the_since_boot_average():
    """A box up for three weeks would otherwise report three weeks of steal."""
    previous = {"user": 1000.0, "system": 100.0, "idle": 8000.0, "steal": 50.0}
    current = {"user": 1050.0, "system": 110.0, "idle": 8830.0, "steal": 60.0}
    # 10 steal ticks out of 900 elapsed ticks.
    assert cpu_steal_pct(previous, current) == pytest.approx(1.111, abs=0.01)


def test_cpu_steal_is_none_when_there_is_no_previous_reading():
    assert cpu_steal_pct({}, {"user": 1.0, "steal": 0.0}) is None


def test_proc_stat_is_none_off_linux_rather_than_zero(tmp_path):
    """No /proc means unknown, which the tripwire then reports honestly."""
    assert read_proc_stat(tmp_path / "nothing-here") is None


def test_proc_stat_parses_the_aggregate_cpu_line(tmp_path):
    path = tmp_path / "stat"
    path.write_text(
        "cpu  100 0 50 900 10 0 0 40\ncpu0 50 0 25 450 5 0 0 20\n", encoding="utf-8"
    )
    assert read_proc_stat(path)["steal"] == 40.0


def test_locust_history_gives_the_live_view_of_a_run_with_no_summary_yet(tmp_path):
    path = tmp_path / "run_stats_history.csv"
    path.write_text(
        "Timestamp,User Count,Type,Name,Requests/s,99%,Total Request Count,"
        "Total Failure Count\n"
        "1758000000,50,,Aggregated,14.2,880,1000,0\n"
        "1758000001,50,,Aggregated,15.1,920,1200,6\n",
        encoding="utf-8",
    )
    reading = read_locust_history(path)
    assert reading["users"] == 50
    assert reading["throughput_rps"] == 15.1
    assert reading["p99_ms"] == 920
    assert reading["error_rate_pct"] == pytest.approx(0.5)


def test_a_missing_history_file_is_none_so_the_tripwire_reports_no_heartbeat(tmp_path):
    assert read_locust_history(tmp_path / "absent.csv") is None
    reading = LiveReadings(stats_history_csv=tmp_path / "absent.csv")(elapsed_s=0.0)
    assert reading.load_generator_beat_age_s is None
    assert reading.throughput_rps is None


def test_stopping_locust_terminates_before_it_kills():
    """Locust writes its statistics on shutdown; killing it discards the evidence."""
    events = []

    class FakeProcess:
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append("wait")

    stop_locust(FakeProcess())
    assert events == ["terminate", "wait"]
    stop_locust(None)  # no process is not an error


def test_a_non_2xx_answer_still_counts_as_reachable():
    """The service is up and saying no. That is the error tripwire's business, not this one."""
    from crucible.perf.watchdog import probe_http

    assert probe_http("") == (None, None)


# ---------------------------------------------------------------------------
# 23.11 The workspace revert an abort performs
# ---------------------------------------------------------------------------


PROFILE_YAML = """\
name: spring-boot
runtime: jvm
cause_families: [connection_pool_exhaustion]
config_file: src/main/resources/application.properties
allowed_properties:
  spring.datasource.hikari.maximum-pool-size:
    type: int
    min: 1
    max: 100
protected_paths: [config/slo.yaml]
deploy:
  mode: pipeline
  remote: perftest
  branch: perftest_sandbox
  base_branch: main
"""


def _applicator(tmp_path, git):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE_YAML, encoding="utf-8")
    config = tmp_path / "src" / "main" / "resources"
    config.mkdir(parents=True)
    (config / "application.properties").write_text(
        "spring.datasource.hikari.maximum-pool-size=2\n", encoding="utf-8"
    )
    return Applicator(profile=TargetProfile.load(profile_path), workspace=tmp_path, _git=git)


def test_the_abort_revert_checks_out_one_path_and_never_the_whole_tree(tmp_path):
    """§19.1b: the workspace is the TARGET's repository. Nothing else in it is ours to discard."""
    calls = []

    def git(argv):
        calls.append(argv)
        return 0, ""

    result = _applicator(tmp_path, git).discard_uncommitted()
    assert calls == [
        ["git", "checkout", "--", "src/main/resources/application.properties"]
    ]
    assert "restored to HEAD" in result


def test_a_failed_revert_raises_rather_than_reporting_success(tmp_path):
    def git(argv):
        return 1, "error: pathspec did not match"

    with pytest.raises(ApplyError):
        _applicator(tmp_path, git).discard_uncommitted()
