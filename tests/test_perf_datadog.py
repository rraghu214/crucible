"""Datadog adapter assertions — week 3, new group 19. DESIGN.md §4.1, §4.8, §5, §16.

DRAFTED by Claude Code. NOT YET REVIEWED by the operator.

The third metrics provider, and the first that shares neither PromQL's API nor
Micrometer's units. Two things are being asserted here and they carry very
different weight:

**The unit conversion, which is the K3 failure in a new coat.** Micrometer's
Datadog registry publishes timer base units in NANOSECONDS, where the same
meter under Prometheus or Actuator is seconds. An adapter that passed those
through would have the collector multiply nanoseconds by 1000 as though they
were seconds, and the K3 pool would read 2.4 *billion* milliseconds instead of
2406. `test_the_k3_numbers_survive_the_nanosecond_adapter` is the assertion
that matters most in this file.

The judgement call worth the operator's attention: **this adapter converts,
where the PromQL adapter refuses to.** DESIGN.md §4.8 says an unconvertible
unit is declared, never guessed — and `providers/promql.py` implements that by
returning `None` for anything that is not `seconds`. Datadog needed a
different answer, because nanoseconds is not an *unknown* unit there, it is the
normal one: refusing it would make the adapter useless against every timer
Datadog actually holds. The line drawn is that conversion is permitted only
from a unit the profile **declared** (`unit: nanoseconds` in the `datadog:`
block), and an undeclared or unrecognised unit is still refused and recorded in
`provider.unreadable`. If the operator disagrees, the alternative is to widen
the collector to take a source unit instead — a larger change, and one that
puts a second unit into the module DESIGN.md §4.1 says must have exactly one.

**Everything else is the seam holding**: same `fetch` shape as the other two
providers, same refusal when a query returns more than one series (§5 — a
percentile does not average across instances), same "we do not know" on an
unreachable backend rather than a raised exception mid-campaign.
"""

from __future__ import annotations

import json

import httpx
import pytest

from crucible.perf.collector import derive_timer_ms
from crucible.perf.profile import TargetProfile
from crucible.perf.providers.datadog import (
    FREE_TIER_HOSTS,
    FREE_TIER_RETENTION_DAYS,
    SUPPORTS_SERVICE_WIDE_PERCENTILES,
    DatadogError,
    DatadogMetricsProvider,
    DatadogSeriesMapping,
)


def _provider(handler, **kwargs) -> DatadogMetricsProvider:
    """A provider wired to a scripted transport, with one declared timer."""
    series = kwargs.pop(
        "series",
        {
            "hikaricp.connections.acquire": DatadogSeriesMapping(
                count="hikaricp.connections.acquire.count",
                total="hikaricp.connections.acquire.sum",
                maximum="hikaricp.connections.acquire.max",
                unit="nanoseconds",
            )
        },
    )
    return DatadogMetricsProvider(
        base_url="https://api.datadoghq.com",
        api_key="dd-api",
        application_key="dd-app",
        series=series,
        scope=kwargs.pop("scope", {"host": "box-a"}),
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def _one_series(value):
    """The shape Datadog's /api/v1/query returns for a single scoped series."""
    return {
        "status": "ok",
        "series": [
            {
                "metric": "whatever",
                "scope": "host:box-a",
                "pointlist": [[1_700_000_000_000, value]],
            }
        ],
    }


# ---------------------------------------------------------------------------
# 19.1 — the unit difference
# ---------------------------------------------------------------------------


class TestNanosecondsAreConvertedNotPassedThrough:
    def test_a_declared_nanosecond_timer_is_returned_in_seconds(self):
        """The collector's contract is seconds, for every backend, so that the
        one seconds-to-milliseconds conversion in `collector.py` stays the only
        unit arithmetic in the product."""

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["query"]
            if query.endswith("count{host:box-a}"):
                return httpx.Response(200, json=_one_series(3186))
            if "sum" in query:
                return httpx.Response(200, json=_one_series(3_499_070_000_000))
            return httpx.Response(200, json=_one_series(2_405_600_000))

        raw = _provider(handler).fetch("hikaricp.connections.acquire")

        assert raw is not None
        assert raw["COUNT"] == pytest.approx(3186)
        assert raw["TOTAL_TIME"] == pytest.approx(3499.07)
        assert raw["MAX"] == pytest.approx(2.4056)

    def test_the_k3_numbers_survive_the_nanosecond_adapter(self):
        """The assertion that matters most in this file, and the direct
        analogue of 15.2 for PromQL. Feeding this adapter's output through the
        collector must produce the K3 numbers — 1098 ms mean and a 2406 ms
        recent max — not 2.4, and not 2.4 billion."""

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["query"]
            if query.endswith("count{host:box-a}"):
                return httpx.Response(200, json=_one_series(3186))
            if "sum" in query:
                return httpx.Response(200, json=_one_series(3_499_070_000_000))
            return httpx.Response(200, json=_one_series(2_405_600_000))

        raw = _provider(handler).fetch("hikaricp.connections.acquire")
        derived = derive_timer_ms(raw, "acquire")

        assert derived["acquire_mean_ms"] == pytest.approx(1098, rel=0.01)
        assert derived["acquire_max_recent_ms"] == pytest.approx(2406, rel=0.01)

    def test_a_count_is_never_unit_converted(self):
        """A tally has no unit to convert. Dividing 3186 acquisitions by a
        billion would turn evidence that the pool was hammered into evidence
        that it was idle.

        The conversion helper itself converts whatever it is handed — that is
        its job — so the rule being asserted is that `fetch` does not call it
        for COUNT."""
        assert DatadogMetricsProvider.to_seconds(3186, "nanoseconds") == pytest.approx(3.186e-6)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params["query"].endswith("count{host:box-a}"):
                return httpx.Response(200, json=_one_series(3186))
            return httpx.Response(200, json={"status": "ok", "series": []})

        raw = _provider(handler).fetch("hikaricp.connections.acquire")

        assert raw == {"COUNT": 3186.0}

    def test_seconds_declared_are_left_alone(self):
        """A target that submits seconds is not double-converted just because
        the backend is Datadog. The unit is a property of the metric, declared
        per series, not of the provider."""
        assert DatadogMetricsProvider.to_seconds(2.4056, "seconds") == pytest.approx(2.4056)


class TestAnUndeclaredUnitIsRefusedNotGuessed:
    def test_a_timer_with_no_declared_unit_is_refused_and_recorded(self):
        """DESIGN.md 4.8. Converting on a hunch is the K3 failure; dropping
        silently leaves the agent reasoning from a picture whose edges it
        cannot see. So: excluded from derived values AND surfaced."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(1.0))

        provider = _provider(
            handler,
            series={
                "mystery.timer": DatadogSeriesMapping(
                    count="mystery.timer.count", total="mystery.timer.sum", unit=""
                )
            },
        )

        assert provider.fetch("mystery.timer") is None
        assert provider.unreadable["mystery.timer"]["unit"] == "unknown"

    def test_an_unrecognised_unit_is_refused_and_names_what_it_would_accept(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(1.0))

        provider = _provider(
            handler,
            series={
                "jiffy.timer": DatadogSeriesMapping(
                    count="jiffy.timer.count", total="jiffy.timer.sum", unit="jiffies"
                )
            },
        )

        assert provider.fetch("jiffy.timer") is None
        reason = provider.unreadable["jiffy.timer"]["reason"]
        assert "jiffies" in reason
        assert "nanoseconds" in reason


# ---------------------------------------------------------------------------
# 19.2 — the API shape
# ---------------------------------------------------------------------------


class TestTheQueryItSends:
    def test_credentials_and_a_time_window_are_sent_on_every_query(self):
        """Datadog's v1 query API has no instant form: a query without from/to
        is a 400, and a query without both keys is a 403 that reads exactly
        like an empty metric."""
        captured: dict[str, str] = {}
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            paths.append(request.url.path)
            return httpx.Response(200, json=_one_series(1.0))

        _provider(handler).fetch("hikaricp.connections.acquire")

        assert paths and set(paths) == {"/api/v1/query"}
        assert captured["api_key"] == "dd-api"
        assert captured["application_key"] == "dd-app"
        assert int(captured["to"]) - int(captured["from"]) == 300

    def test_the_scope_pins_the_query_to_one_host(self):
        """The free tier has one host, but a query without a scope starts
        averaging silently the moment a second one appears."""
        queries: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            queries.append(request.url.params["query"])
            return httpx.Response(200, json=_one_series(1.0))

        _provider(handler).fetch("hikaricp.connections.acquire")

        assert all("{host:box-a}" in q for q in queries)

    def test_the_aggregator_is_chosen_per_statistic(self):
        """Same rule as PromQL's 15.5: counts sum, maxima do not. The slowest
        request in the service is the largest per-tag maximum, not the total of
        them, and summing would report a latency nothing ever experienced."""
        queries: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            queries.append(request.url.params["query"])
            return httpx.Response(200, json=_one_series(1.0))

        _provider(handler).fetch("hikaricp.connections.acquire")

        assert any(q.startswith("sum:") and ".count" in q for q in queries)
        assert any(q.startswith("max:") and ".max" in q for q in queries)


class TestMoreThanOneSeriesIsAnErrorNotAChoice:
    def test_two_series_return_none_rather_than_the_first(self):
        """Assertion 15.4 carried across. More than one series means the scope
        did not pin a single host, and picking the first would quietly report
        one machine's numbers as the service's — DESIGN.md §5 forbids exactly
        this one level up, for percentiles."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {"scope": "host:a", "pointlist": [[1, 10.0]]},
                        {"scope": "host:b", "pointlist": [[1, 20.0]]},
                    ],
                },
            )

        assert _provider(handler).fetch("hikaricp.connections.acquire") is None


class TestAnUnreachableBackendDegradesToUnknown:
    def test_an_http_error_is_none_not_an_exception(self):
        """A campaign must not abort mid-run over a transient 502. `None`
        becomes `null` in the snapshot, which the agent reads as 'never
        measured' — true, and safe."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        assert _provider(handler).fetch("hikaricp.connections.acquire") is None

    def test_malformed_json_is_none_not_an_exception(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        assert _provider(handler).fetch("hikaricp.connections.acquire") is None

    def test_a_null_datapoint_is_skipped_for_the_last_real_one(self):
        """Datadog pads sparse series with nulls. Reading the final point
        blindly would report 'never measured' for a metric that has data a few
        seconds earlier."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {"scope": "host:box-a", "pointlist": [[1, 5_000_000_000], [2, None]]}
                    ],
                },
            )

        raw = _provider(handler).fetch("hikaricp.connections.acquire")

        assert raw is not None
        assert raw["TOTAL_TIME"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 19.3 — gauges, construction, and the free tier
# ---------------------------------------------------------------------------


class TestGauges:
    def test_a_gauge_returns_the_actuator_value_shape(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(43))

        provider = _provider(
            handler, series={"hikaricp.connections.pending": DatadogSeriesMapping(value="hikaricp.connections.pending")}
        )

        assert provider.fetch("hikaricp.connections.pending") == {"VALUE": 43.0}

    def test_an_undeclared_metric_is_none(self):
        """Not an error: the profile simply does not publish it here, and the
        collector renders that as null — 'we never looked' — rather than 0."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(1.0))

        assert _provider(handler).fetch("nothing.declared") is None


class TestConstruction:
    def test_both_keys_are_required(self):
        """A request carrying only the API key gets a 403, which is easy to
        mistake for an empty metric — so it fails at construction instead."""
        with pytest.raises(DatadogError, match="APPLICATION key"):
            DatadogMetricsProvider(
                base_url="https://api.datadoghq.com", api_key="dd-api", application_key=""
            )

    def test_from_profile_reads_the_declared_section(self):
        """The shipped spring-boot profile, not a fabricated one — a test
        against a hand-built mapping would pass while the file a campaign
        actually loads was broken."""
        profile = TargetProfile.named("spring-boot")

        provider = DatadogMetricsProvider.from_profile(
            "https://api.datadoghq.com",
            profile,
            api_key="dd-api",
            application_key="dd-app",
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_one_series(1.0)))),
        )

        acquire = provider.series["hikaricp.connections.acquire"]
        assert acquire.unit == "nanoseconds"
        assert acquire.is_timer
        assert provider.series["hikaricp.connections.pending"].is_gauge

    def test_a_profile_with_no_datadog_section_refuses_at_construction(self):
        """Falling back to derived names would be the guess this adapter
        exists to avoid, and failing here is far cheaper than failing eight
        minutes into a load run."""
        profile = TargetProfile.named("fastapi")

        with pytest.raises(DatadogError, match="declares no `datadog:` section"):
            DatadogMetricsProvider.from_profile(
                "https://api.datadoghq.com", profile, api_key="k", application_key="a"
            )


class TestTheFreeTierIsDeclared:
    def test_the_limits_are_carried_for_the_manifest(self):
        """A campaign that ran against one day of retention is a different
        claim from one that could compare against last month, and a reader
        should not have to know Datadog's pricing page to tell them apart."""
        assert FREE_TIER_HOSTS == 1
        assert FREE_TIER_RETENTION_DAYS == 1

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(1.0))

        limits = _provider(handler).free_tier_limits()

        assert limits["hosts"] == 1
        assert limits["retention_days"] == 1
        assert "retention" in limits["note"]

    def test_datadog_supports_service_wide_percentiles_where_actuator_does_not(self):
        """DESIGN.md §5: Datadog holds the distribution, so a percentile can be
        computed across instances rather than averaged from pre-aggregated
        per-instance ones."""
        from crucible.perf.providers.actuator import (
            SUPPORTS_SERVICE_WIDE_PERCENTILES as ACTUATOR_SUPPORTS,
        )

        assert SUPPORTS_SERVICE_WIDE_PERCENTILES is True
        assert ACTUATOR_SUPPORTS is False


class TestEndpointBreakdown:
    def test_a_breakdown_is_keyed_by_the_declared_tag(self):
        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["query"]
            value = 100.0 if ".count" in query else 5.0
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {"tag_set": ["resource_name:/api/db"], "pointlist": [[1, value]]}
                    ],
                },
            )

        breakdown = _provider(handler).endpoint_breakdown("http.server.requests")

        assert breakdown is not None
        assert breakdown["/api/db"]["request_count"] == pytest.approx(100.0)
        assert breakdown["/api/db"]["mean_ms"] == pytest.approx(50.0)

    def test_no_series_means_none_not_an_empty_dict(self):
        """`None` sets `available_evidence.endpoint_breakdown: false`, so the
        agent knows it cannot attribute latency to one endpoint. An empty dict
        would read as 'looked, found nothing'."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "series": []})

        assert _provider(handler).endpoint_breakdown("http.server.requests") is None

    def test_no_series_name_means_none(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_one_series(1.0))

        assert _provider(handler).endpoint_breakdown("") is None


class TestTheSeamHolds:
    def test_fetch_returns_the_same_shape_as_the_other_providers(self):
        """The point of the adapter seam: switching a campaign from Actuator
        or PromQL to Datadog changes the provider and nothing else."""

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["query"]
            if ".count" in query:
                return httpx.Response(200, json=_one_series(10))
            return httpx.Response(200, json=_one_series(1_000_000_000))

        raw = _provider(handler).fetch("hikaricp.connections.acquire")

        assert raw is not None
        assert set(raw) <= {"COUNT", "TOTAL_TIME", "MAX"}
        assert json.dumps(raw)  # plain floats, serialisable into a snapshot
