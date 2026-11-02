"""Ceiling discovery: §20. The knee is a pair, never a single number.

Key behaviours:
- The result records last_passing_users and first_failing_users, not one number.
- Each step is a full measured run (same measure callable as Campaign).
- The probe stops at the first SLA miss — it does not push beyond.
- A watchdog trip (load.aborted) also stops the probe and records why.
- Running a ceiling probe against a production target is refused (§20.1).
- A probe where all steps pass within the declared bounds is reported as-is,
  with the ceiling stated as "above max_users".
"""

from __future__ import annotations

import pytest

from crucible.perf.campaign import CampaignRefused, Sla
from crucible.perf.ceiling import CeilingProbe, CeilingResult
from crucible.perf.runner import LoadResult, Scenario

# ---------------------------------------------------------------------------
# SLA and scenario fixtures
# ---------------------------------------------------------------------------

SLA_YAML = """\
name: perflab
environment:
  name: staging
  kind: pre-prod
  target_base_url: http://localhost:8080
objective:
  endpoint: /orders
  p99_ms: 150
  error_rate_pct: 5.0
noise:
  p99_spread_pct: 2.08
  measured_on: 2026-09-12
host_contention:
  cpu_steal_abort_pct: 10.0
"""


@pytest.fixture
def sla(tmp_path):
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML, encoding="utf-8")
    return Sla.load(path)


@pytest.fixture
def scenario():
    return Scenario(name="db", host="http://target", users=50, warmup_s=0, measure_s=0)


def _probe(
    sla,
    scenario,
    tmp_path,
    *,
    p99s: list[float],
    step_size: int = 50,
    max_users: int = 500,
    error_rates: list[float] | None = None,
    abort_at: int | None = None,
) -> CeilingResult:
    """Build and run a CeilingProbe with scripted measurements."""
    seq_p99 = iter(p99s)
    seq_err = iter(error_rates or [0.0] * len(p99s))

    def measure(sc, run_id):
        p99 = next(seq_p99)
        err = next(seq_err)
        load = LoadResult(
            run_id=run_id,
            scenario=sc.name,
            p50_ms=p99 * 0.6,
            p99_ms=p99,
            mean_ms=p99 * 0.7,
            error_rate_pct=err,
            rps=100.0,
            aborted=(abort_at is not None and sc.users >= abort_at),
            abort_reason=(
                f"watchdog: CPU steal exceeded threshold at {sc.users} users"
                if abort_at is not None and sc.users >= abort_at
                else None
            ),
        )
        return load, {"p99_ms": p99}

    probe = CeilingProbe(
        sla=sla,
        base_scenario=scenario,
        measure=measure,
        min_users=scenario.users,
        max_users=max_users,
        step_size=step_size,
        run_id="ceiling-test",
        results_dir=tmp_path / "results",
    )
    return probe.run()


# ---------------------------------------------------------------------------
# The result is a knee (pair), never a single number
# ---------------------------------------------------------------------------


class TestTheCeilingResultIsAKnee:
    """§20.4. Two tested points bound the true ceiling; the step size limits precision."""

    def test_last_passing_and_first_failing_are_both_recorded(self, sla, scenario, tmp_path):
        """p99s: 100, 100, 100, 200 ms — the SLA is 150 ms.
        Levels: 50, 100, 150, 200 users. SLA fails at 200 users."""
        result = _probe(sla, scenario, tmp_path, p99s=[100.0, 100.0, 100.0, 200.0])

        assert result.last_passing_users == 150
        assert result.first_failing_users == 200

    def test_knee_property_returns_the_pair(self, sla, scenario, tmp_path):
        result = _probe(sla, scenario, tmp_path, p99s=[100.0, 200.0])

        assert result.knee == (50, 100)

    def test_stopped_reason_names_both_levels(self, sla, scenario, tmp_path):
        result = _probe(sla, scenario, tmp_path, p99s=[100.0, 200.0])

        assert "50 users" in result.stopped_reason
        assert "100 users" in result.stopped_reason


# ---------------------------------------------------------------------------
# Probe stops at the first SLA miss
# ---------------------------------------------------------------------------


class TestStopsAtFirstSLAMiss:
    """§20.5. The probe found what it came for; running further adds no knowledge."""

    def test_probe_runs_exactly_the_steps_needed_to_find_the_knee(
        self, sla, scenario, tmp_path
    ):
        # SLA fails at step 2 (100 users). Probe must not continue to 150.
        result = _probe(sla, scenario, tmp_path, p99s=[100.0, 200.0, 100.0])

        assert len(result.steps) == 2  # step 1 passes, step 2 fails, stop

    def test_first_step_fails_means_no_passing_level(self, sla, scenario, tmp_path):
        """last_passing_users is None when the very first level already fails."""
        result = _probe(sla, scenario, tmp_path, p99s=[200.0])

        assert result.last_passing_users is None
        assert result.first_failing_users == 50

    def test_high_error_rate_fails_even_at_good_latency(self, sla, scenario, tmp_path):
        """§20.5 stop conditions include error rate, not just p99."""
        result = _probe(
            sla, scenario, tmp_path,
            p99s=[100.0, 100.0],
            error_rates=[0.0, 10.0],  # 10% errors at step 2 (> 5% threshold)
        )

        assert result.first_failing_users == 100
        assert result.last_passing_users == 50


# ---------------------------------------------------------------------------
# Watchdog aborts the probe
# ---------------------------------------------------------------------------


class TestWatchdogAbort:
    """A watchdog trip during a ceiling step stops the probe and records why."""

    def test_a_watchdog_abort_stops_the_probe(self, sla, scenario, tmp_path):
        """CPU steal exceeds the threshold at 100 users."""
        result = _probe(
            sla, scenario, tmp_path,
            p99s=[100.0, 100.0],
            abort_at=100,
        )

        assert any(s.aborted for s in result.steps)
        assert "watchdog" in result.stopped_reason.lower()

    def test_the_aborted_step_is_recorded_in_steps(self, sla, scenario, tmp_path):
        result = _probe(
            sla, scenario, tmp_path,
            p99s=[100.0, 100.0],
            abort_at=100,
        )

        aborted_steps = [s for s in result.steps if s.aborted]
        assert len(aborted_steps) == 1
        assert aborted_steps[0].users == 100


# ---------------------------------------------------------------------------
# All steps pass within declared bounds
# ---------------------------------------------------------------------------


class TestAllStepsPass:
    """When the service doesn't break within the declared bounds, say so."""

    def test_first_failing_users_is_none_when_all_steps_pass(
        self, sla, scenario, tmp_path
    ):
        result = _probe(
            sla, scenario, tmp_path,
            p99s=[100.0, 100.0, 100.0],
            max_users=150,
        )

        assert result.first_failing_users is None
        assert result.last_passing_users == 150

    def test_stopped_reason_says_ceiling_is_above_max_users(
        self, sla, scenario, tmp_path
    ):
        result = _probe(
            sla, scenario, tmp_path,
            p99s=[100.0, 100.0, 100.0],
            max_users=150,
        )

        assert "above" in result.stopped_reason
        assert "150" in result.stopped_reason


# ---------------------------------------------------------------------------
# Production is refused
# ---------------------------------------------------------------------------


class TestProductionIsRefused:
    """§20.1 / §19.1. A ceiling probe can destroy a service; never on prod."""

    def test_running_against_a_production_target_raises_campaign_refused(
        self, tmp_path, scenario
    ):
        path = tmp_path / "prod.yaml"
        path.write_text(
            SLA_YAML.replace("kind: pre-prod", "kind: production"),
            encoding="utf-8",
        )
        prod_sla = Sla.load(path)

        probe = CeilingProbe(
            sla=prod_sla,
            base_scenario=scenario,
            measure=lambda sc, rid: (
                LoadResult(run_id=rid, scenario=sc.name, p99_ms=100.0),
                {},
            ),
            results_dir=tmp_path / "results",
        )

        with pytest.raises(CampaignRefused, match="production"):
            probe.run()


# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------


class TestCeilingResultSerialisation:
    """write() and as_dict() round-trip correctly."""

    def test_result_is_written_to_disk(self, sla, scenario, tmp_path):
        _probe(sla, scenario, tmp_path, p99s=[100.0, 200.0])

        out = tmp_path / "results" / "ceiling-test.ceiling.json"
        assert out.exists()

    def test_kind_field_distinguishes_it_from_an_experiment_result(
        self, sla, scenario, tmp_path
    ):
        """§20.6: a ceiling result must not look like an experiment manifest."""
        result = _probe(sla, scenario, tmp_path, p99s=[100.0, 200.0])
        assert result.as_dict()["kind"] == "ceiling"
