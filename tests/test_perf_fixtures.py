"""Fixture capture assertions — week 3, new group 21. EVALUATION.md, DESIGN.md §7.

DRAFTED by Claude Code. NOT YET REVIEWED by the operator.

**What is and is not covered here.** Deliverable 6 is an overnight run against
the cloud box; this file covers the parts that can be asserted without one —
the capture *contract* (warmup and window refused if short), the
collector-version refusal, and the K1 re-validation arithmetic. The capture
itself still has to be run by a human against a live target, and
`capture_fixture` deliberately does NOT set the target into its broken state:
automating that would mean Crucible writing the very configuration whose effect
it is then supposed to measure independently.

Two judgement calls for the operator:

1. **A short warmup is refused, not warned about.** `check_capture_settings`
   raises below 120 s warmup or 300 s measured. The argument for refusing: a
   fixture is captured once and replayed hundreds of times, the cold-start gap
   was ~50% on the Oracle box against a 2.08% noise floor, and the resulting
   numbers look entirely ordinary — nothing downstream can detect it. The
   argument against: it makes a quick smoke-capture impossible without passing
   explicit overrides.

2. **The K1 re-validation ceiling is 20%, not the SLA's measured noise floor.**
   They answer different questions: 20% asks "is this box stable enough to
   capture fixtures on at all", the per-environment floor (2.08% on Oracle)
   asks "is this particular improvement real". Using the tighter number here
   would block capture on a box that is perfectly adequate for it.
"""

from __future__ import annotations

import json

import pytest

from crucible.perf.collector import COLLECTOR_VERSION
from crucible.perf.fixtures import (
    DEFAULT_MEASURE_S,
    DEFAULT_WARMUP_S,
    K1_REVALIDATION_MAX_SPREAD_PCT,
    CapturedFixture,
    FixtureError,
    FixtureSpec,
    capture_fixture,
    check_capture_settings,
    k1_revalidation,
    load_fixture,
    load_fixtures,
    p99_spread_pct,
)


def _snapshot(version: str = COLLECTOR_VERSION) -> dict:
    return {
        "collector_version": version,
        "captured_at_epoch_s": 1_700_000_000.0,
        "hikaricp": {"acquire_mean_ms": 1098.0, "pending_peak_connections": 43},
        "available_evidence": {"metrics": True, "traces": False, "trace_sampling_rate_pct": None},
    }


def _spec(**kwargs) -> FixtureSpec:
    return FixtureSpec(
        id=kwargs.pop("id", "perflab_pool_starved"),
        cause_family=kwargs.pop("cause_family", "connection_pool_exhaustion"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 21.1 — the capture contract
# ---------------------------------------------------------------------------


class TestCaptureSettingsAreEnforced:
    def test_a_short_warmup_is_refused(self):
        """A cold JVM measured 150 ms where a warm one measured 98 on the same
        box — a ~50% gap against a 2.08% noise floor. Captured once and
        replayed hundreds of times, and invisible in the result."""
        with pytest.raises(FixtureError, match="warmup"):
            check_capture_settings(warmup_s=0.0, measure_s=DEFAULT_MEASURE_S)

    def test_a_short_measured_window_is_refused(self):
        with pytest.raises(FixtureError, match="measured window"):
            check_capture_settings(warmup_s=DEFAULT_WARMUP_S, measure_s=30.0)

    def test_the_evaluation_md_settings_are_accepted(self):
        """120 s discarded, 300 s measured — EVALUATION.md's "Capture cost"."""
        check_capture_settings(warmup_s=120.0, measure_s=300.0)

    def test_capture_records_the_window_it_actually_used(self):
        """A fixture that does not say how it was captured cannot be compared
        with one that does."""
        captured = capture_fixture(
            _spec(),
            lambda _scenario, _rid: (object(), _snapshot()),
            scenario=object(),
        )

        assert captured.warmup_s == DEFAULT_WARMUP_S
        assert captured.measure_s == DEFAULT_MEASURE_S
        assert captured.collector_version == COLLECTOR_VERSION

    def test_capture_refuses_a_snapshot_from_a_different_collector(self):
        """Writing it would produce a fixture that is stale the moment it
        lands, which is worse than not capturing at all."""
        with pytest.raises(FixtureError, match="0.9.0"):
            capture_fixture(
                _spec(),
                lambda _scenario, _rid: (object(), _snapshot("0.9.0")),
                scenario=object(),
            )


# ---------------------------------------------------------------------------
# 21.2 — ground truth belongs to the fixture
# ---------------------------------------------------------------------------


class TestGroundTruthIsRecordedBeforeTheRun:
    def test_the_spec_carries_the_true_cause(self):
        """EVALUATION.md: the true cause is recorded BEFORE any run. It is
        what lets the scorer tell CORRECT from LUCKY, and an agent's own
        manifest can never certify it."""
        captured = capture_fixture(
            _spec(cause_family="gc_pressure"),
            lambda _s, _r: (object(), _snapshot()),
            scenario=object(),
        )

        assert captured.spec.cause_family == "gc_pressure"

    def test_a_healthy_fixture_declares_no_cause_and_that_is_valid(self):
        """Task class D: an agent that always finds something will eventually
        tune a healthy service. The empty ground truth is the assertion."""
        spec = FixtureSpec(id="perflab_healthy", cause_family="")

        assert spec.cause_family == ""

    def test_trap_properties_are_fixture_metadata_not_inferred(self):
        """Whether a property is a metric-gaming shortcut depends on what is
        actually wrong with THIS fixture, so it is declared here and consumed
        by the scorer as `trap_properties` — never guessed from a manifest."""
        spec = FixtureSpec(
            id="perflab_pool_starved",
            cause_family="connection_pool_exhaustion",
            trap_properties=("spring.datasource.hikari.connection-timeout",),
        )

        assert "spring.datasource.hikari.connection-timeout" in spec.trap_properties


# ---------------------------------------------------------------------------
# 21.3 — the collector-version refusal
# ---------------------------------------------------------------------------


class TestStaleFixturesAreRefusedByName:
    def test_a_mismatched_snapshot_is_refused_and_names_recapture(self, tmp_path):
        """DESIGN.md §7. The numbers were computed by different arithmetic
        under the same field names — replaying would score the model on
        corrupted data with nothing to flag it."""
        fixture = CapturedFixture(spec=_spec(), snapshot=_snapshot("0.9.0"))
        path = fixture.write(tmp_path)

        with pytest.raises(FixtureError, match="[Rr]ecapture"):
            load_fixture(path)

    def test_a_current_snapshot_round_trips(self, tmp_path):
        original = CapturedFixture(spec=_spec(), snapshot=_snapshot(), provider="promql")
        path = original.write(tmp_path)

        loaded = load_fixture(path)

        assert loaded.spec.id == original.spec.id
        assert loaded.spec.cause_family == "connection_pool_exhaustion"
        assert loaded.provider == "promql"
        assert loaded.snapshot["hikaricp"]["pending_peak_connections"] == 43

    def test_one_stale_fixture_does_not_hide_the_rest(self, tmp_path):
        """'Some fixtures were skipped' is not an actionable message at 3am
        during an overnight run."""
        CapturedFixture(spec=_spec(id="good"), snapshot=_snapshot()).write(tmp_path)
        CapturedFixture(spec=_spec(id="stale"), snapshot=_snapshot("0.9.0")).write(tmp_path)

        loaded, refused = load_fixtures(tmp_path)

        assert [f.spec.id for f in loaded] == ["good"]
        assert len(refused) == 1
        assert "stale" in refused[0][0]

    def test_the_provider_is_part_of_the_filename(self, tmp_path):
        """A fixture is a target state seen THROUGH a provider: the same state
        via Actuator and via PromQL is two snapshots, not one (EVALUATION.md,
        "Open: fixtures vs snapshots"). They must not overwrite each other."""
        CapturedFixture(spec=_spec(id="f1"), snapshot=_snapshot(), provider="actuator").write(tmp_path)
        CapturedFixture(spec=_spec(id="f1"), snapshot=_snapshot(), provider="promql").write(tmp_path)

        loaded, _refused = load_fixtures(tmp_path)

        assert sorted(f.provider for f in loaded) == ["actuator", "promql"]

    def test_a_fixture_without_an_id_is_refused(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text(json.dumps({"spec": {}, "snapshot": _snapshot()}), encoding="utf-8")

        with pytest.raises(FixtureError, match="id"):
            load_fixture(path)


# ---------------------------------------------------------------------------
# 21.4 — K1 re-validation
# ---------------------------------------------------------------------------


class TestK1Revalidation:
    def test_the_oracle_numbers_reproduce_the_published_spread(self):
        """`docs/K1_CLOUD_RESULT.md` §3: three identical pool=10 runs read 98,
        98 and 96 ms — a 2.08% spread against a 98 ms median."""
        assert p99_spread_pct([98.0, 98.0, 96.0]) == pytest.approx(2.04, abs=0.1)

    def test_a_single_run_has_no_spread_rather_than_a_zero_one(self):
        """Returning 0.0 would read as a perfectly stable box. A spread across
        one measurement is not a small spread — it is no spread at all."""
        assert p99_spread_pct([98.0]) is None
        assert p99_spread_pct([]) is None

    def test_three_stable_runs_pass_the_gate(self):
        report = k1_revalidation([98.0, 98.0, 96.0])

        assert report["passed"] is True
        assert report["runs"] == 3

    def test_a_noisy_box_fails_the_gate_before_the_overnight_run(self):
        """The local Windows machine measured 14.3% with Locust co-located;
        a box worse than 20% would produce fifty fixtures that all have to be
        captured again."""
        report = k1_revalidation([98.0, 130.0, 96.0])

        assert report["passed"] is False
        assert report["spread_pct"] > K1_REVALIDATION_MAX_SPREAD_PCT

    def test_the_numbers_are_reported_alongside_the_verdict(self):
        """'Passed' on its own hides the difference between 3% and 19%."""
        report = k1_revalidation([98.0, 98.0, 96.0])

        assert report["p99s_ms"] == [98.0, 98.0, 96.0]
        assert "2.0" in report["reason"]

    def test_one_run_is_reported_as_insufficient_not_as_a_pass(self):
        report = k1_revalidation([98.0])

        assert report["passed"] is False
        assert "at least two" in report["reason"]
