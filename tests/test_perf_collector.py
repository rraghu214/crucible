"""Collector assertions — GROUP 1 of docs/CRUCIBLE_TEST_ASSERTIONS.md.

REVIEWED AND APPROVED by the operator (week 1; re-confirmed 20 September 2026). Three specific questions are raised in the assertions doc under
"Open questions for review" — field naming (1.2 vs 1.4), the build_snapshot
signature, and whether MAX should be carried at all.

Every test below traces to a failure that actually happened during the K1/K3
spike, or to a design rule that exists because of one.
"""

import pytest

from crucible.perf.collector import (
    COLLECTOR_VERSION,
    UNIT_SUFFIXES,
    AvailableEvidence,
    build_derived_hikari,
    build_snapshot,
    compute_window,
    peak_during_load,
    redact_runtime_config,
    seconds_to_ms,
)
from crucible.perf.profile import TargetProfile

#: Metric names come from the shipped profile, never from this test -- the same
#: rule the package is held to. Passing them also keeps the snapshot tests
#: honest: with no metric_keys, build_snapshot looks nothing up and assertions
#: about "no raw tuple survives" would pass vacuously.
METRIC_KEYS = TargetProfile.named("spring-boot").snapshot_metrics

#: The real K3 attempt-1 numbers. TOTAL_TIME 3499.07 s over 3186 acquisitions is
#: 1098 ms mean; MAX 2.4056 s is 2406 ms. The model read that MAX as 2.4 ms.
K3_ACQUIRE_RAW = {"COUNT": 3186, "TOTAL_TIME": 3499.07, "MAX": 2.4056}


class TestUnitConversion:
    """1.1 — Micrometer timers are seconds. This is the K3 attempt-1 failure."""

    def test_acquire_timer_is_converted_from_seconds_to_milliseconds(self):
        """Reading 2.4 as milliseconds made the model call a saturated pool healthy."""
        derived = build_derived_hikari(K3_ACQUIRE_RAW)

        assert derived["acquire_mean_ms"] == pytest.approx(1098, rel=0.01)
        assert derived["acquire_max_recent_ms"] == pytest.approx(2406, rel=0.01)

    def test_seconds_to_ms_preserves_none(self):
        """A missing timer must not become 0.0 ms on the way through the converter."""
        assert seconds_to_ms(None) is None
        assert seconds_to_ms(2.4056) == pytest.approx(2405.6)

    def test_no_raw_micrometer_tuple_survives_into_the_snapshot(self):
        """AGENTS.md non-negotiable 1: the model never sees COUNT/TOTAL_TIME/MAX."""
        snapshot = build_snapshot(
            {"hikaricp.connections.acquire": K3_ACQUIRE_RAW}, metric_keys=METRIC_KEYS
        )

        # Guard against a hollow pass: if the metric were not read at all, the
        # snapshot would contain no raw tuple for the trivial reason that it
        # contains nothing. Prove the conversion actually happened first.
        assert snapshot["hikaricp"]["acquire_mean_ms"] == pytest.approx(1098, rel=0.01)

        rendered = repr(snapshot)
        assert "TOTAL_TIME" not in rendered
        assert "COUNT" not in rendered


class TestUnitsInFieldNames:
    """1.2 — a number without a unit is an invitation to guess."""

    def test_no_derived_field_omits_its_unit(self):
        """A field named 'acquire_mean' invites the reader to assume a unit."""
        derived = build_derived_hikari(K3_ACQUIRE_RAW)

        for key in derived:
            if key == "note":
                continue
            assert key.endswith(UNIT_SUFFIXES), f"{key} has no unit suffix"


class TestGaugeSampling:
    """1.3 and 1.4 — the second half of the K3 failure, and the S18 lesson."""

    def test_gauge_peak_is_recorded_not_the_final_reading(self):
        """pending drains to 0 the moment load stops. The peak is the evidence."""
        samples = [0, 12, 43, 38, 41, 0]

        assert peak_during_load(samples) == 43

    def test_an_unsampled_gauge_is_null_never_zero(self):
        """A clean zero and an untested zero must not look identical."""
        snapshot = build_snapshot({}, gauge_samples={})

        assert snapshot["hikaricp"]["pending_peak_connections"] is None
        assert snapshot["hikaricp"]["pending_peak_connections"] != 0

    def test_a_sampled_zero_is_zero_not_null(self):
        """The other direction. We looked, and there genuinely were no waiters.

        Without this test the previous one passes trivially by making every gauge
        null, which would destroy the distinction rather than preserve it.
        """
        snapshot = build_snapshot({}, gauge_samples={"pending": [0, 0, 0]})

        assert snapshot["hikaricp"]["pending_peak_connections"] == 0
        assert snapshot["hikaricp"]["pending_peak_connections"] is not None

    def test_unsampled_gauges_are_named_in_a_note(self):
        """Null is only honest if the agent is told which gauges were not sampled."""
        derived = build_derived_hikari(K3_ACQUIRE_RAW, gauge_samples={"pending": [3]})

        assert "active" in derived["note"]
        assert "idle" in derived["note"]
        assert "pending" not in derived["note"]


class TestMeasurementWindow:
    """1.5 — JIT warmup makes early requests slow; including them poisons the baseline."""

    def test_max_is_named_recent_because_it_decays(self):
        """Q3. Micrometer's MAX is a rolling ~2-minute max that forgets.

        A 1800 ms acquire early in a 5-minute window has aged out by the time the
        window ends, so this value is NOT the worst in the window. The name has to
        say so, or the agent reads it as one and rules out a tail-latency cause on
        a number that already forgot the evidence.
        """
        window = compute_window({"COUNT": 0, "TOTAL_TIME": 0.0},
                                {"COUNT": 100, "TOTAL_TIME": 5.0, "MAX": 0.25})

        assert "max_ms" not in window
        assert window["max_recent_ms"] == pytest.approx(250.0)
        assert "decays" in window["max_recent_ms_note"] or "aged out" in window["max_recent_ms_note"]

    def test_metrics_are_a_delta_across_the_measurement_window(self):
        at_warmup_end = {"COUNT": 1000, "TOTAL_TIME": 60.0}
        at_measure_end = {"COUNT": 5000, "TOTAL_TIME": 260.0}

        window = compute_window(at_warmup_end, at_measure_end)

        assert window["count"] == 4000
        assert window["mean_ms"] == pytest.approx(50.0)

    def test_a_window_with_no_requests_has_no_mean(self):
        """Zero requests means no mean exists. 0.0 ms would read as 'instant'."""
        window = compute_window({"COUNT": 1000, "TOTAL_TIME": 60.0},
                                {"COUNT": 1000, "TOTAL_TIME": 60.0})

        assert window["count"] == 0
        assert window["mean_ms"] is None


class TestCollectorVersion:
    """1.6 — replayed snapshots are only valid for the collector that produced them."""

    def test_snapshot_carries_the_collector_version_that_built_it(self):
        snapshot = build_snapshot(
            {"hikaricp.connections.acquire": K3_ACQUIRE_RAW}, metric_keys=METRIC_KEYS
        )

        assert snapshot["collector_version"] == COLLECTOR_VERSION

    def test_every_snapshot_carries_it_even_when_nothing_was_collected(self):
        """An empty snapshot is still a snapshot, and still gets replayed."""
        assert build_snapshot()["collector_version"] == COLLECTOR_VERSION


class TestAvailableEvidence:
    """DESIGN.md 4.3 — the agent must know what it never looked at."""

    def test_evidence_declares_missing_traces_with_a_reason(self):
        snapshot = build_snapshot({})

        evidence = snapshot["available_evidence"]
        assert evidence["traces"] is False
        assert evidence["trace_reason"]

    def test_absent_gauge_sampling_is_declared_not_merely_implied(self):
        """The agent should not have to infer 'never sampled' from a field of nulls."""
        snapshot = build_snapshot({}, gauge_samples={})

        evidence = snapshot["available_evidence"]
        assert evidence["gauge_sampling"] is False
        assert any("null" in note for note in evidence["notes"])

    def test_trace_sampling_rate_travels_with_the_evidence(self):
        """A p99 outlier is rare by definition, so 1-10% sampling can miss it entirely.

        'No slow spans were found' is a different claim at 100% sampling than at 5%,
        and the agent cannot tell them apart without this number.
        """
        evidence = AvailableEvidence(metrics=True, traces=True, trace_sampling_rate_pct=5.0)
        snapshot = build_snapshot({}, evidence=evidence)

        assert snapshot["available_evidence"]["trace_sampling_rate_pct"] == 5.0


class TestRedaction:
    """DESIGN.md 12 — allowlist, never blocklist, and before the journal write."""

    def test_only_allowlisted_properties_survive(self):
        """/actuator/env returns datasource passwords next to pool sizes."""
        props = {
            "spring.datasource.hikari.maximum-pool-size": 2,
            "spring.datasource.password": "hunter2",
            "some.api.key": "sk-live-abcdef",
        }

        redacted = redact_runtime_config(props, ["spring.datasource.hikari.maximum-pool-size"])

        assert redacted["spring.datasource.hikari.maximum-pool-size"] == 2
        assert "spring.datasource.password" not in redacted
        assert "hunter2" not in repr(redacted)

    def test_an_unknown_secret_shaped_key_is_dropped_by_default(self):
        """The point of an allowlist: it fails closed on the key nobody thought of.

        A blocklist matching /password|secret|key/ would pass this one straight
        through to the model and into the journal.
        """
        redacted = redact_runtime_config({"acme.tenant.bearer-token": "abc123"}, [])

        assert "acme.tenant.bearer-token" not in redacted
        assert "abc123" not in repr(redacted)

    def test_the_number_of_dropped_properties_is_disclosed(self):
        """Silent redaction would let the agent believe it saw the whole config."""
        redacted = redact_runtime_config({"a": 1, "b": 2, "c": 3}, ["a"])

        assert redacted["_redacted_count"] == 2
