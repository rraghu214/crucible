"""PromQL as a metrics provider: Prometheus and everything that speaks its API.

One adapter covers Prometheus, Mimir, Thanos, VictoriaMetrics, Chronosphere and
the AWS/Azure/Google managed Prometheus services -- roughly half the
observability market for one implementation, which is why it lands second after
Actuator and before the proprietary query languages (``DESIGN.md`` section 5).

**It fixes Actuator's real limitation.** Actuator exposes pre-aggregated
percentiles for one instance, and p99 of instance A does not combine with p99 of
instance B into a service p99 -- that needs the underlying histogram. Prometheus
holds the buckets, so :meth:`PromQLMetricsProvider.service_quantile` computes a
true service-wide percentile and :data:`SUPPORTS_SERVICE_WIDE_PERCENTILES` is
``True`` here where it is ``False`` for Actuator.

**No metric name is invented in this file.** Micrometer's Prometheus registry
renames meters -- dots become underscores, and the base unit is appended, so
``hikaricp.connections.acquire`` is published as
``hikaricp_connections_acquire_seconds_{count,sum,max}``. Those transformations
look mechanical, and deriving them here would work for the meters we happen to
have tested. It would also silently produce a wrong series name for the first
meter whose base unit we guessed wrong, and a wrong series name returns no data,
which the collector faithfully records as "never measured". The agent would then
be told it had no evidence about a pool that Prometheus was scraping perfectly
well. So the series names are declared in ``profile.yaml`` under ``promql:``,
exactly as the Actuator names are declared under ``snapshot_metrics:``.

**A declared series carries its unit, and an undeclared one is not guessed.** The
``unit`` key on each mapping says what the series is in. ``seconds`` is converted
to the ``TOTAL_TIME``/``MAX`` shape the collector already divides; anything else
is returned unconverted and flagged, so it is excluded from derived values rather
than being read as the wrong magnitude. That is the K3 failure -- seconds read as
milliseconds -- and it does not get a second chance through a new adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

#: Prometheus stores histogram buckets, so percentiles can be computed across
#: instances. This is the capability Actuator lacks (``DESIGN.md`` section 5).
SUPPORTS_SERVICE_WIDE_PERCENTILES = True

#: Units this adapter knows how to hand to the collector. Micrometer publishes
#: timers in seconds under Prometheus, same as under Actuator, so the collector's
#: existing conversion applies unchanged. Anything not on this list is carried
#: through as-is and marked unreadable rather than assumed.
KNOWN_TIMER_UNITS = ("seconds",)


class PromQLError(RuntimeError):
    """The Prometheus API could not be reached or answered unusably."""


@dataclass
class SeriesMapping:
    """How one logical metric is published by this runtime under Prometheus.

    Declared in ``profile.yaml``, never derived. ``count``/``total``/``max``
    describe a timer; ``value`` describes a gauge. A mapping with neither is a
    configuration error rather than an empty result, because an empty result is
    indistinguishable from a metric that genuinely was not being scraped.
    """

    count: str = ""
    total: str = ""
    maximum: str = ""
    value: str = ""
    unit: str = ""
    #: Extra label matchers, e.g. ``{"area": "heap"}``. Micrometer splits one
    #: logical meter across many label combinations -- ``jvm_memory_used_bytes``
    #: has a series per memory pool -- and without a matcher the query returns
    #: all of them.
    labels: dict[str, str] = field(default_factory=dict)
    #: How to collapse several series into one. ``sum`` is the usual answer.
    #: Empty means "expect exactly one series", which is correct for a meter that
    #: genuinely has one and is the safer default: a metric that unexpectedly
    #: splits should refuse rather than silently report one label's value as the
    #: whole.
    aggregate: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | str) -> SeriesMapping:
        # A bare string is the common case for a gauge: `pending: hikaricp_connections_pending`
        if isinstance(data, str):
            return cls(value=data)
        return cls(
            count=str(data.get("count", "")),
            total=str(data.get("total", "")),
            maximum=str(data.get("max", "")),
            value=str(data.get("value", "")),
            unit=str(data.get("unit", "")),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            aggregate=str(data.get("aggregate", "")),
        )

    @property
    def is_timer(self) -> bool:
        return bool(self.count or self.total or self.maximum)

    @property
    def is_gauge(self) -> bool:
        return bool(self.value)


@dataclass
class PromQLMetricsProvider:
    """A Prometheus-compatible query endpoint, shaped like the Actuator provider.

    ``fetch`` returns the same ``{"COUNT": .., "TOTAL_TIME": .., "MAX": ..}`` /
    ``{"VALUE": ..}`` mappings the collector already consumes, so switching a
    campaign from Actuator to PromQL changes the provider and nothing else. That
    is the whole point of the adapter seam: the collector's arithmetic, and every
    integrity rule that depends on it, is provider-independent.
    """

    base_url: str
    #: Logical metric name -> how it is published. Comes from the profile's
    #: ``promql:`` section; this class never fills it in.
    series: dict[str, SeriesMapping] = field(default_factory=dict)
    #: Label selector pinning the query to one target, e.g.
    #: ``{"instance": "10.0.0.79:8080"}``. Without it a multi-instance scrape
    #: would sum meters across instances, and a summed acquire-time mean is not
    #: a quantity that means anything.
    selector: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 10.0
    name: str = "promql"
    _client: Any = None
    #: Metrics the profile declared with a unit this adapter cannot convert.
    #: Surfaced rather than dropped -- see the module docstring.
    unreadable: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_profile(
        cls,
        base_url: str,
        profile: Any,
        *,
        selector: dict[str, str] | None = None,
        client: Any = None,
        timeout_s: float = 10.0,
    ) -> PromQLMetricsProvider:
        """Build from a ``TargetProfile``'s ``promql:`` section.

        Raises when the profile has no such section. Falling back to derived
        names would be the guess this adapter exists to avoid, and failing at
        construction is far cheaper than failing eight minutes into a load run.
        """
        raw = getattr(profile, "promql", None) or {}
        if not raw:
            raise PromQLError(
                f"profile {getattr(profile, 'name', '?')!r} declares no `promql:` "
                "section, so this adapter has no series names. Prometheus renames "
                "Micrometer meters and the renaming depends on each meter's base "
                "unit; deriving it here would produce plausible names that silently "
                "return no data."
            )
        return cls(
            base_url=base_url,
            series={str(k): SeriesMapping.from_mapping(v) for k, v in raw.items()},
            selector=dict(selector or {}),
            timeout_s=timeout_s,
            _client=client,
        )

    # -- querying ---------------------------------------------------------

    def _selector_text(self, extra: dict[str, str] | None = None) -> str:
        labels = {**self.selector, **(extra or {})}
        if not labels:
            return ""
        inner = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        return "{" + inner + "}"

    def query(self, expression: str) -> list[dict[str, Any]]:
        """Run an instant query and return its result vector.

        Errors return an empty list rather than raising. A metrics backend being
        briefly unreachable must degrade to "we do not know", which the collector
        renders as ``null`` -- raising here would abort a campaign mid-run over a
        scrape hiccup, and retrying silently would hide a backend that is down.
        """
        try:
            response = self._client.get(
                f"{self.base_url}/api/v1/query", params={"query": expression}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if payload.get("status") != "success":
            return []
        return list((payload.get("data") or {}).get("result") or [])

    def scalar(self, expression: str) -> float | None:
        """The single value an instant query returned, or ``None``.

        ``None`` for an empty vector and ``None`` for a vector with several
        entries. The second case matters: more than one series means the selector
        did not pin a single instance, and picking the first would silently report
        one machine's numbers as the service's.
        """
        vector = self.query(expression)
        if len(vector) != 1:
            return None
        try:
            return float(vector[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    # -- the MetricsProvider interface ------------------------------------

    def fetch(self, name: str) -> dict[str, float] | None:
        """The raw statistic mapping for ``name``, in Actuator's shape.

        ``None`` when the profile does not declare the metric, when Prometheus
        has no data for it, or when its unit is one this adapter will not
        convert. All three are honestly "we do not know", and the collector turns
        that into ``null`` rather than ``0``.
        """
        mapping = self.series.get(name)
        if mapping is None:
            return None

        if mapping.is_timer:
            if mapping.unit and mapping.unit not in KNOWN_TIMER_UNITS:
                # Declared, never guessed, never dropped. Reading an unknown unit
                # as seconds is exactly the K3 failure, and dropping it silently
                # leaves the agent reasoning from a picture whose edges it cannot
                # see (DESIGN.md 4.8).
                self.unreadable[name] = {
                    "unit": "unknown",
                    "declared_unit": mapping.unit,
                    "reason": (
                        f"profile declares unit {mapping.unit!r}; this adapter converts "
                        f"only {', '.join(KNOWN_TIMER_UNITS)}. Excluded from every "
                        "derived value rather than guessed."
                    ),
                }
                return None
            raw: dict[str, float] = {}
            for stat, series_name in (
                ("COUNT", mapping.count),
                ("TOTAL_TIME", mapping.total),
                ("MAX", mapping.maximum),
            ):
                if not series_name:
                    continue
                value = self.scalar(self.expression(mapping, series_name, stat))
                if value is not None:
                    raw[stat] = value
            return raw or None

        if mapping.is_gauge:
            value = self.scalar(self.expression(mapping, mapping.value, "VALUE"))
            return {"VALUE": value} if value is not None else None

        return None

    def expression(self, mapping: SeriesMapping, series_name: str, stat: str) -> str:
        """The PromQL for one statistic of one declared series.

        The aggregation is chosen PER STATISTIC, because the arithmetic differs
        and getting it wrong is the kind of error that produces a plausible
        number. Summing a count and summing a total are both correct -- 200
        requests across four URIs really is 200 requests. Summing a MAX is not:
        the longest request in the service is the largest of the per-URI maxima,
        not their total, and adding them would report a latency nothing ever
        experienced.

        Found by running against a real Prometheus. Micrometer splits
        ``http_server_requests_seconds_count`` across uri/status/method/outcome
        and ``jvm_memory_used_bytes`` across memory pools, so a bare series name
        returns many series and :meth:`scalar` -- correctly -- refuses to pick
        one. Every fake-client test passed, because a fake answers whatever it
        was asked for.
        """
        inner = f"{series_name}{self._selector_text(mapping.labels)}"
        if not mapping.aggregate:
            return inner
        operator = "max" if stat == "MAX" else mapping.aggregate
        return f"{operator}({inner})"

    # -- what Prometheus can do that Actuator cannot ----------------------

    def service_quantile(
        self, histogram_series: str, quantile: float, *, window: str = "5m"
    ) -> float | None:
        """A true service-wide percentile, computed from histogram buckets.

        This is the capability that makes PromQL worth a second adapter. Actuator
        hands back one instance's pre-aggregated p99, and averaging two instances'
        p99s produces a number that is not a percentile of anything. Here the
        buckets are summed first and the quantile taken afterwards, which is the
        only order that is arithmetically valid.

        Returned in **milliseconds**, because every duration Crucible carries ends
        in its unit and is already converted -- the collector's rule applies to
        adapters that bypass it too.
        """
        selector = self._selector_text()
        expression = (
            f"histogram_quantile({quantile}, "
            f"sum by (le) (rate({histogram_series}{selector}[{window}])))"
        )
        seconds = self.scalar(expression)
        return None if seconds is None else seconds * 1000.0

    def endpoint_breakdown(
        self, requests_series: str = "", *, window: str = "5m"
    ) -> dict[str, Any] | None:
        """Per-URI request statistics, or ``None`` when they cannot be had.

        ``None`` is meaningful: the snapshot's ``available_evidence`` then reports
        ``endpoint_breakdown: false``, and the agent knows it cannot attribute
        latency to one endpoint instead of assuming it is spread evenly.
        """
        if not requests_series:
            return None
        selector = self._selector_text()
        counts = self.query(
            f"sum by (uri) (rate({requests_series}_count{selector}[{window}]))"
        )
        sums = self.query(
            f"sum by (uri) (rate({requests_series}_sum{selector}[{window}]))"
        )
        if not counts:
            return None

        def by_uri(vector: list[dict[str, Any]]) -> dict[str, float]:
            out: dict[str, float] = {}
            for entry in vector:
                uri = (entry.get("metric") or {}).get("uri")
                if uri is None:
                    continue
                try:
                    out[str(uri)] = float(entry["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            return out

        count_by_uri = by_uri(counts)
        sum_by_uri = by_uri(sums)
        breakdown: dict[str, Any] = {}
        for uri, rate in count_by_uri.items():
            total_s = sum_by_uri.get(uri)
            breakdown[uri] = {
                "request_rate_rps": rate,
                "mean_ms": (total_s / rate * 1000.0) if total_s is not None and rate else None,
            }
        return breakdown or None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
