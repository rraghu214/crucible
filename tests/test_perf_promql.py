"""PromQL adapter assertions — new group, week 2. DESIGN.md §4.1, §4.8, §5.

REVIEWED AND APPROVED by the operator, 20 September 2026.

The adapter's job is to look exactly like the Actuator one to the collector, so
that switching provider changes the provider and nothing else. Every integrity
rule in the collector then applies unchanged, which is the whole value of the
adapter seam.

Two things here are worth arguing about, and both are asserted rather than
assumed:

**Series names are declared, not derived.** Micrometer's Prometheus registry
appends each meter's base unit, so `hikaricp.connections.acquire` becomes
`..._seconds` while `hikaricp.connections.pending` gains nothing and
`jvm.memory.used` gains `_bytes`. No single rule produces all three. A derived
name that is wrong returns no data, which the collector faithfully records as
"never measured" — so the agent would be told it has no evidence about a meter
Prometheus is scraping perfectly well. That is a worse failure than an error,
because it looks like honesty.

**More than one series matching is an error, not a choice.** If the selector did
not pin a single instance, picking the first would report one machine's numbers
as the service's. §5 is explicit that percentiles cannot be averaged across
instances, and this is the same mistake one level down.
"""

import pytest

from crucible.perf.profile import TargetProfile
from crucible.perf.providers.promql import (
    SUPPORTS_SERVICE_WIDE_PERCENTILES,
    PromQLError,
    PromQLMetricsProvider,
    SeriesMapping,
)

PROMQL = {
    "hikaricp.connections.acquire": {
        "count": "hikaricp_connections_acquire_seconds_count",
        "total": "hikaricp_connections_acquire_seconds_sum",
        "max": "hikaricp_connections_acquire_seconds_max",
        "unit": "seconds",
    },
    "hikaricp.connections.pending": "hikaricp_connections_pending",
    "custom.pool.wait": {"count": "custom_pool_wait_count", "total": "custom_pool_wait_ticks",
                         "unit": "jiffies"},
}


def a_provider(vectors=None, **over) -> PromQLMetricsProvider:
    base = {
        "base_url": "http://prom:9090",
        "series": {k: SeriesMapping.from_mapping(v) for k, v in PROMQL.items()},
        "selector": {"instance": "10.0.0.79:8080"},
        "_client": _FakeClient(vectors or {}),
    }
    return PromQLMetricsProvider(**(base | over))


class TestItLooksLikeTheActuatorProviderToTheCollector:
    """One interface, several backends. The collector must not know which."""

    def test_a_timer_comes_back_in_the_count_total_max_shape(self):
        """The same keys Actuator returns, so `derive_timer_ms` divides them the
        same way and the K3 unit conversion happens in exactly one place."""
        provider = a_provider({
            "hikaricp_connections_acquire_seconds_count": 3186.0,
            "hikaricp_connections_acquire_seconds_sum": 3499.07,
            "hikaricp_connections_acquire_seconds_max": 2.4066,
        })

        raw = provider.fetch("hikaricp.connections.acquire")

        assert raw == {"COUNT": 3186.0, "TOTAL_TIME": 3499.07, "MAX": 2.4066}

    def test_the_collector_turns_that_into_the_k3_number(self):
        """End to end on the numbers that caused the original failure. 3499.07 s
        over 3186 acquisitions is 1098 ms, not the 2.4 the raw MAX appeared to
        say. If the adapter returned seconds under a different key, this would
        silently be wrong by 1000x."""
        from crucible.perf.collector import derive_timer_ms

        provider = a_provider({
            "hikaricp_connections_acquire_seconds_count": 3186.0,
            "hikaricp_connections_acquire_seconds_sum": 3499.07,
            "hikaricp_connections_acquire_seconds_max": 2.4066,
        })

        derived = derive_timer_ms(provider.fetch("hikaricp.connections.acquire"), "acquire")

        assert derived["acquire_mean_ms"] == pytest.approx(1098.0, abs=1.0)
        assert derived["acquire_max_recent_ms"] == pytest.approx(2406.6, abs=1.0)

    def test_a_gauge_comes_back_as_a_value_mapping(self):
        provider = a_provider({"hikaricp_connections_pending": 43.0})

        assert provider.fetch("hikaricp.connections.pending") == {"VALUE": 43.0}

    def test_a_gauge_with_no_data_is_none_not_zero(self):
        """The distinction the whole design rests on. `None` reaches the collector
        as null — 'we never looked' — and 0 would say the pool had no waiters."""
        assert a_provider({}).fetch("hikaricp.connections.pending") is None


class TestSeriesNamesAreDeclaredNeverDerived:
    """§5. A metric name is a property of the runtime, not of Crucible."""

    def test_a_metric_the_profile_does_not_declare_returns_none(self):
        """Not an attempt at `jvm_gc_pause_seconds`. Guessing produces a plausible
        name that returns nothing, and the agent is then told it has no evidence
        about a meter that is being scraped."""
        assert a_provider({"jvm_gc_pause_seconds_count": 5.0}).fetch("jvm.gc.pause") is None

    def test_the_real_profile_declares_all_three_naming_shapes(self):
        """The reason derivation cannot work, asserted on the shipped profile:
        one meter gains `_seconds`, one gains nothing, one gains `_bytes`."""
        promql = TargetProfile.named("spring-boot").promql

        assert promql["hikaricp.connections.acquire"]["count"].endswith("_seconds_count")
        assert promql["hikaricp.connections.pending"] == "hikaricp_connections_pending"
        assert promql["jvm.memory.used"]["value"] == "jvm_memory_used_bytes"

    def test_a_profile_with_no_promql_section_fails_at_construction(self):
        """Eight minutes into a load run is a very expensive place to discover
        that no metric name resolves."""
        profile = TargetProfile.from_mapping(
            {"name": "x", "runtime": "jvm", "cause_families": ["gc_pressure"]}
        )

        with pytest.raises(PromQLError, match="no `promql:` section"):
            PromQLMetricsProvider.from_profile("http://prom:9090", profile)

    def test_from_profile_loads_every_declared_series(self):
        provider = PromQLMetricsProvider.from_profile(
            "http://prom:9090", TargetProfile.named("spring-boot"), client=_FakeClient({})
        )

        assert "hikaricp.connections.acquire" in provider.series
        assert provider.series["hikaricp.connections.acquire"].is_timer
        assert provider.series["hikaricp.connections.pending"].is_gauge


class TestAnUnknownUnitIsDeclaredNotGuessed:
    """§4.8. Reading seconds as milliseconds is how a saturated pool looked healthy."""

    def test_a_series_with_an_unconvertible_unit_is_not_returned(self):
        """Excluded from every derived value rather than passed through as though
        it were seconds. A wrong magnitude is worse than a missing number,
        because a wrong magnitude gets reasoned about."""
        provider = a_provider({"custom_pool_wait_count": 10.0, "custom_pool_wait_ticks": 500.0})

        assert provider.fetch("custom.pool.wait") is None

    def test_the_unreadable_metric_is_recorded_rather_than_dropped_silently(self):
        """Dropping it leaves the agent reasoning from a picture whose edges it
        cannot see. It has to know something was there that it could not read."""
        provider = a_provider({"custom_pool_wait_count": 10.0, "custom_pool_wait_ticks": 500.0})
        provider.fetch("custom.pool.wait")

        assert "custom.pool.wait" in provider.unreadable
        assert provider.unreadable["custom.pool.wait"]["unit"] == "unknown"
        assert provider.unreadable["custom.pool.wait"]["declared_unit"] == "jiffies"

    def test_a_seconds_timer_is_not_marked_unreadable(self):
        provider = a_provider({
            "hikaricp_connections_acquire_seconds_count": 1.0,
            "hikaricp_connections_acquire_seconds_sum": 1.0,
        })
        provider.fetch("hikaricp.connections.acquire")

        assert provider.unreadable == {}


class TestMetricsThatSplitAcrossLabels:
    """Found by running against a real Prometheus, not by reasoning.

    Micrometer splits one logical meter across many label combinations --
    `http_server_requests_seconds_count` per uri/status/method/outcome,
    `jvm_memory_used_bytes` per memory pool. A bare series name then returns
    dozens of series and `scalar()` correctly refuses to pick one, so the metric
    reads as MISSING. Every fake-client test passed throughout, because a fake
    answers whatever it was asked for.
    """

    def test_a_declared_aggregate_wraps_the_query(self):
        mapping = SeriesMapping.from_mapping(
            {"value": "jvm_memory_used_bytes", "aggregate": "sum"}
        )
        provider = a_provider(series={"jvm.memory.used": mapping})

        assert provider.expression(mapping, mapping.value, "VALUE") == (
            'sum(jvm_memory_used_bytes{instance="10.0.0.79:8080"})'
        )

    def test_declared_labels_narrow_the_query(self):
        """`area: heap` is not cosmetic. The collector's field is
        `heap_used_peak_bytes`, and quietly summing non-heap pools in as well
        would make the number not the thing its name claims."""
        mapping = SeriesMapping.from_mapping(
            {"value": "jvm_memory_used_bytes", "labels": {"area": "heap"}, "aggregate": "sum"}
        )
        provider = a_provider(series={"jvm.memory.used": mapping})

        assert provider.expression(mapping, mapping.value, "VALUE") == (
            'sum(jvm_memory_used_bytes{area="heap",instance="10.0.0.79:8080"})'
        )

    def test_max_is_maxed_not_summed(self):
        """The arithmetic differs per statistic and getting it wrong produces a
        plausible number. 200 requests across four URIs really is 200 requests,
        so COUNT sums. But the slowest request in the service is the LARGEST
        per-URI maximum, not the total of them -- summing would report a latency
        nothing ever experienced."""
        mapping = SeriesMapping.from_mapping({
            "count": "http_server_requests_seconds_count",
            "total": "http_server_requests_seconds_sum",
            "max": "http_server_requests_seconds_max",
            "unit": "seconds", "aggregate": "sum",
        })
        provider = a_provider(series={"http.server.requests": mapping})

        assert provider.expression(mapping, mapping.count, "COUNT").startswith("sum(")
        assert provider.expression(mapping, mapping.total, "TOTAL_TIME").startswith("sum(")
        assert provider.expression(mapping, mapping.maximum, "MAX").startswith("max(")

    def test_no_aggregate_means_expect_exactly_one_series(self):
        """The safer default. A metric that unexpectedly splits should refuse
        rather than silently report one label's value as the whole -- which is
        how `hikaricp_connections_pending` should behave, since there is one
        pool."""
        mapping = SeriesMapping.from_mapping("hikaricp_connections_pending")
        provider = a_provider(series={"hikaricp.connections.pending": mapping})

        assert provider.expression(mapping, mapping.value, "VALUE") == (
            'hikaricp_connections_pending{instance="10.0.0.79:8080"}'
        )

    def test_the_shipped_profile_aggregates_the_two_that_need_it(self):
        """Regression guard on the actual finding. Both of these returned nothing
        from Box A's Prometheus until aggregation was declared."""
        promql = TargetProfile.named("spring-boot").promql

        assert promql["http.server.requests"]["aggregate"] == "sum"
        assert promql["jvm.memory.used"]["aggregate"] == "sum"
        assert promql["jvm.memory.used"]["labels"] == {"area": "heap"}

    def test_the_single_series_metrics_are_left_unaggregated(self):
        """Over-applying `sum` would hide a metric that started splitting."""
        promql = TargetProfile.named("spring-boot").promql

        assert promql["hikaricp.connections.pending"] == "hikaricp_connections_pending"


class TestPinningToOneInstance:
    """§5. p99 of instance A and p99 of instance B do not combine."""

    def test_the_selector_is_applied_to_every_query(self):
        provider = a_provider({"hikaricp_connections_pending": 43.0})
        provider.fetch("hikaricp.connections.pending")

        assert provider._client.queries == ['hikaricp_connections_pending{instance="10.0.0.79:8080"}']

    def test_more_than_one_matching_series_is_none_rather_than_the_first(self):
        """If the selector did not pin one instance, taking the first would report
        one machine's numbers as the service's — quietly, and with no way for a
        later reader to tell."""
        provider = a_provider()
        provider._client.multi = True

        assert provider.fetch("hikaricp.connections.pending") is None

    def test_an_empty_selector_produces_no_label_matcher(self):
        provider = a_provider(selector={})

        assert provider._selector_text() == ""

    def test_labels_are_sorted_so_the_same_selector_always_renders_identically(self):
        """Queries end up in logs and in journals that get diffed. A matcher whose
        order depended on dict insertion would make identical queries look
        different."""
        provider = a_provider(selector={"job": "perflab", "instance": "a:8080"})

        assert provider._selector_text() == '{instance="a:8080",job="perflab"}'


class TestWhatPrometheusCanDoThatActuatorCannot:
    """The reason a second metrics adapter is worth building at all."""

    def test_it_advertises_service_wide_percentile_support(self):
        """Actuator declares False. A campaign reads this to decide whether
        multi-instance load is a valid measurement."""
        from crucible.perf.providers.actuator import (
            SUPPORTS_SERVICE_WIDE_PERCENTILES as ACTUATOR_SUPPORT,
        )

        assert SUPPORTS_SERVICE_WIDE_PERCENTILES is True
        assert ACTUATOR_SUPPORT is False

    def test_the_quantile_sums_buckets_before_taking_the_percentile(self):
        """The only arithmetically valid order. Averaging two instances'
        pre-computed p99s produces a number that is not a percentile of
        anything."""
        provider = a_provider()
        provider._client.answers = {"__any__": 0.058}

        provider.service_quantile("http_server_requests_seconds_bucket", 0.99)
        query = provider._client.queries[-1]

        assert query.startswith("histogram_quantile(0.99, sum by (le) (rate(")
        assert "http_server_requests_seconds_bucket" in query

    def test_the_quantile_is_returned_in_milliseconds(self):
        """Every duration Crucible carries is already converted and named with its
        unit. An adapter that returned seconds would put the K3 failure back in,
        one level below where it was fixed."""
        provider = a_provider()
        provider._client.answers = {"__any__": 0.058}

        assert provider.service_quantile("http_server_requests_seconds_bucket", 0.99) == pytest.approx(58.0)

    def test_no_breakdown_series_returns_none_rather_than_an_empty_dict(self):
        """`None` makes `available_evidence.endpoint_breakdown` false, so the
        agent knows it cannot attribute latency to one endpoint. An empty dict
        would read as 'looked, found no endpoints'."""
        assert a_provider().endpoint_breakdown("") is None


class TestFailureIsAlwaysWeDoNotKnow:
    """A scrape hiccup must not abort a campaign, and must not fake a number."""

    def test_an_http_error_yields_no_data_rather_than_raising(self):
        provider = a_provider()
        provider._client.explode = True

        assert provider.fetch("hikaricp.connections.pending") is None

    def test_a_non_success_status_yields_no_data(self):
        provider = a_provider()
        provider._client.status = "error"

        assert provider.query("up") == []


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Answers instant queries from a dict keyed by the bare series name."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.queries: list[str] = []
        self.multi = False
        self.explode = False
        self.status = "success"

    def get(self, url, params=None):
        query = (params or {}).get("query", "")
        self.queries.append(query)
        if self.explode:
            import httpx

            raise httpx.ConnectError("prometheus unreachable")
        bare = query.split("{")[0]
        value = self.answers.get(bare, self.answers.get("__any__"))
        results = []
        if value is not None:
            results = [{"metric": {}, "value": [0, str(value)]}]
            if self.multi:
                results = results * 2
        return _FakeResponse({"status": self.status, "data": {"result": results}})

    def close(self):
        pass


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload
