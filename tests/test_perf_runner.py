"""Load runner and gauge sampler assertions — new, extends GROUP 1.

REVIEWED AND APPROVED by the operator (week 1; re-confirmed 20 September 2026).

These cover the half of the K3 fix that lives in the runner rather than the
collector: a gauge is only evidence if something read it while load was running.
No locust process is started here — the subprocess launch is stubbed, because a
test that needs a live target is a test that will be skipped in CI and will
therefore never catch anything.
"""

import time

import pytest

from crucible.perf.collector import build_derived_hikari
from crucible.perf.runner import GaugeSampler, LoadResult, Scenario, measurement_window


def wait_for_calls(provider, count, timeout_s=5.0):
    """Block until the sampler has read `count` times, or fail the test.

    A bare `while provider.calls < n: pass` hangs CI forever if the sampler
    thread dies, which is precisely the failure these tests exist to detect.
    DEBT.md already records sleep-ordered tests as a known problem here; this
    waits on the condition and bounds it.
    """
    deadline = time.monotonic() + timeout_s
    while provider.calls < count:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"sampler made {provider.calls} of {count} reads in {timeout_s}s; "
                "the sampling thread is not running"
            )
        time.sleep(0.005)


class FakeProvider:
    """Replays a fixed series of gauge readings, one per fetch."""

    def __init__(self, series: list[float], fail_after: int | None = None):
        self.series = series
        self.calls = 0
        self.fail_after = fail_after

    def fetch(self, name: str) -> dict[str, float] | None:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("target went away")
        if not self.series:
            return None
        return {"VALUE": self.series[min(self.calls - 1, len(self.series) - 1)]}


class TestGaugeSampler:
    """DESIGN.md 4.2 — gauges are sampled during load and the peak is recorded."""

    def test_the_sampler_records_the_peak_not_the_last_reading(self):
        """pending was 43 mid-run and 0 after. The runner must catch the 43."""
        provider = FakeProvider([0, 12, 43, 38, 0])
        sampler = GaugeSampler(provider, {"pending": "hikaricp.connections.pending"}, interval_s=0.01)

        sampler.start()
        wait_for_calls(provider, 5)
        samples = sampler.stop()

        assert max(samples["pending"]) == 43

    def test_a_gauge_that_was_never_sampled_is_absent_not_empty(self):
        """An empty list would reach the collector and could be read as 'all zero'.

        Omitting the key routes it to the collector's 'never looked' branch, which
        is what turns it into null rather than 0.
        """
        sampler = GaugeSampler(FakeProvider([]), {"pending": "hikaricp.connections.pending"})

        samples = sampler.stop()

        assert "pending" not in samples

    def test_a_read_failure_is_counted_not_raised(self):
        """A sampler that dies mid-run leaves a partly-sampled gauge looking whole.

        That is worse than no sampling at all: the peak would be the peak of the
        first ten seconds, presented as the peak of the run.
        """
        provider = FakeProvider([5, 6, 7], fail_after=3)
        sampler = GaugeSampler(provider, {"pending": "x"}, interval_s=0.01)

        sampler.start()
        wait_for_calls(provider, 6)
        sampler.stop()

        assert sampler.read_failures > 0

    def test_sampler_output_feeds_the_collector_unchanged(self):
        """The seam between the two modules, tested end to end without a target."""
        provider = FakeProvider([0, 43, 12])
        sampler = GaugeSampler(provider, {"pending": "hikaricp.connections.pending"}, interval_s=0.01)
        sampler.start()
        wait_for_calls(provider, 3)
        samples = sampler.stop()

        derived = build_derived_hikari({"COUNT": 10, "TOTAL_TIME": 1.0, "MAX": 0.5}, gauge_samples=samples)

        assert derived["pending_peak_connections"] == 43


class TestScenarioOwnsItsDuration:
    """DESIGN.md 7 — duration and repeats belong to the scenario, not the plan."""

    def test_a_scenario_carries_its_own_warmup_and_measure_windows(self):
        scenario = Scenario(name="db-sustained", host="http://localhost:8080",
                            warmup_s=120.0, measure_s=300.0)

        assert scenario.warmup_s == 120.0
        assert scenario.measure_s == 300.0

    def test_a_scenario_is_immutable(self):
        """A plan that could shorten a scenario would change what was measured
        while still calling it the same experiment."""
        scenario = Scenario(name="db-sustained", host="http://localhost:8080")

        with pytest.raises(Exception):
            scenario.measure_s = 30.0  # type: ignore[misc]

    def test_a_ceiling_probe_declares_that_failure_is_expected(self):
        """DESIGN.md 6 — aborting a push_beyond scenario on errors discards the answer."""
        scenario = Scenario(name="ceiling", host="http://localhost:8080",
                            push_beyond=True, expect_possible_failure=True)

        assert scenario.push_beyond and scenario.expect_possible_failure


class TestMeasurementWindowFromARun:
    """1.5, at the runner seam."""

    def test_the_window_is_the_delta_between_the_two_readings(self):
        result = LoadResult(
            run_id="r1", scenario="db",
            warmup_s=120.0, measure_s=300.0,
            metrics_at_warmup_end={"http.server.requests": {"COUNT": 1000, "TOTAL_TIME": 60.0}},
            metrics_at_measure_end={"http.server.requests": {"COUNT": 5000, "TOTAL_TIME": 260.0}},
        )

        window = measurement_window(result)

        assert window["count"] == 4000
        assert window["mean_ms"] == pytest.approx(50.0)
        assert window["warmup_s"] == 120.0

    def test_a_run_with_no_metrics_provider_says_so(self):
        """Silence here would look identical to a window that measured nothing."""
        window = measurement_window(LoadResult(run_id="r1", scenario="db"))

        assert "no metrics provider" in window["note"]


class TestLoadSummary:
    """What reaches the snapshot from the load generator."""

    def test_error_rate_is_a_percentage_with_its_unit_in_the_name(self):
        result = LoadResult(run_id="r1", scenario="db", request_count=1000, failure_count=25)
        result.error_rate_pct = 100.0 * 25 / 1000

        assert result.as_load_summary()["error_rate_pct"] == pytest.approx(2.5)

    def test_a_run_with_no_requests_has_no_error_rate(self):
        """0% and 'nothing ran' are different outcomes; the watchdog acts on one."""
        result = LoadResult(run_id="r1", scenario="db", request_count=0)

        assert result.as_load_summary()["error_rate_pct"] is None
