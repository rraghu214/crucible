"""Datadog as a metrics provider.

The third adapter, and the first with a proprietary query language (``DESIGN.md``
section 5). Actuator needs nothing but the target; PromQL covers roughly half the
observability market in one implementation; Datadog, Dynatrace, New Relic and
Elastic each need their own because none of them speaks PromQL. This is the one
that proves the seam holds against a backend whose API shape, naming convention
*and units* all differ.

**The unit difference is the whole reason this file is dangerous.** Micrometer
reports timers in seconds, and both existing adapters hand the collector seconds
because that is what their backends natively publish. Datadog does not: a timer
submitted through Micrometer's ``DatadogMeterRegistry`` (or through DogStatsD)
lands with a base unit of **nanoseconds**. An adapter that passed those through
unchanged would have the collector multiply nanoseconds by 1000 as though they
were seconds, and ``acquire`` would read 2.4 billion milliseconds instead of
2406 -- the K3 failure again, a million times louder but in exactly the same
place.

So this adapter normalises to the collector's contract: ``fetch`` returns
``{"COUNT": .., "TOTAL_TIME": .., "MAX": ..}`` **in seconds**, like every other
adapter, and the collector still performs exactly one seconds-to-milliseconds
conversion for every backend. That is not the same thing as an adapter inventing
a conversion. The source unit is **declared** per series in ``profile.yaml``
under ``datadog:``, exactly as ``promql:`` declares ``unit: seconds``; the factor
for a *declared* nanosecond is exact and known, where the factor for an
undeclared unit is a guess. An undeclared or unrecognised unit is still refused
and recorded in :attr:`DatadogMetricsProvider.unreadable` (``DESIGN.md``
section 4.8) rather than converted hopefully.

**Free tier, declared rather than discovered at 3am.** ``DESIGN.md`` section 16
puts Datadog in scope on the free plan: **one host** and **one day** of metric
retention. Both matter to a campaign. One host means a query that returns more
than one series is a configuration error rather than something to average --
the same refusal PromQL makes, for the same reason (section 5: percentiles do
not average across instances). One day means a campaign cannot compare against
a baseline captured last week, and the report must not imply it can.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

#: Datadog stores distributions, so percentiles can be computed across instances
#: rather than averaged. Same capability as PromQL, and the one Actuator lacks
#: (``DESIGN.md`` section 5).
SUPPORTS_SERVICE_WIDE_PERCENTILES = True

#: Source units this adapter will convert into the collector's seconds contract.
#: ``nanoseconds`` is here because it is what Datadog's Micrometer registry and
#: DogStatsD actually publish timers in; the conversion is exact for a DECLARED
#: unit. Anything not on this list is refused, not guessed (``DESIGN.md`` 4.8).
KNOWN_TIMER_UNITS = ("seconds", "nanoseconds")

#: Exact, and therefore safe once the unit has been declared rather than assumed.
_NANOSECONDS_TO_SECONDS = 1e-9

#: ``DESIGN.md`` section 16: Datadog free is one host and one day of retention.
#: Carried as constants so a campaign can state the limits it ran under instead
#: of a reader inferring them.
FREE_TIER_HOSTS = 1
FREE_TIER_RETENTION_DAYS = 1


class DatadogError(RuntimeError):
    """The Datadog API could not be reached, or was configured unusably."""


@dataclass
class DatadogSeriesMapping:
    """How one logical metric is published by this runtime under Datadog.

    Declared in ``profile.yaml``, never derived. Datadog appends an aggregation
    suffix to a submitted timer -- ``.count``, ``.sum``, ``.max``, ``.avg`` --
    and which suffixes exist depends on how the metric was submitted (a
    distribution exposes ``.sum``; a plain gauge exposes nothing but itself).
    Deriving those names from the Micrometer name would produce plausible
    queries that return no data, which the collector would honestly record as
    "never measured" while Datadog held the answer all along.
    """

    count: str = ""
    total: str = ""
    maximum: str = ""
    value: str = ""
    #: ``seconds`` or ``nanoseconds``. Timers submitted through Datadog's
    #: Micrometer registry are nanoseconds; a metric the app computed itself may
    #: be anything, which is precisely why this is declared and not assumed.
    unit: str = ""
    #: The Datadog query aggregator, e.g. ``avg``/``sum``/``max``. Applied per
    #: statistic: a MAX is aggregated with ``max`` whatever this says, because
    #: summing per-tag maxima reports a latency nothing ever experienced.
    aggregator: str = "avg"
    #: Extra tag matchers beyond the provider's own scope, e.g.
    #: ``{"memory_area": "heap"}``.
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | str) -> DatadogSeriesMapping:
        # A bare string is the common case for a gauge.
        if isinstance(data, str):
            return cls(value=data)
        return cls(
            count=str(data.get("count", "")),
            total=str(data.get("total", "")),
            maximum=str(data.get("max", "")),
            value=str(data.get("value", "")),
            unit=str(data.get("unit", "")),
            aggregator=str(data.get("aggregator", "avg")),
            tags={str(k): str(v) for k, v in (data.get("tags") or {}).items()},
        )

    @property
    def is_timer(self) -> bool:
        return bool(self.count or self.total or self.maximum)

    @property
    def is_gauge(self) -> bool:
        return bool(self.value)


@dataclass
class DatadogMetricsProvider:
    """A Datadog metrics query endpoint, shaped like the other two providers.

    ``fetch`` returns the same mappings the collector already consumes, so
    switching a campaign from Actuator or PromQL to Datadog changes the provider
    and nothing else -- the collector's arithmetic, and every integrity rule
    resting on it, stays provider-independent.
    """

    #: ``https://api.datadoghq.com`` for the US site, ``https://api.datadoghq.eu``
    #: for the EU one. Not derived from the key: the same key is invalid against
    #: the wrong site, and a 403 is a much clearer failure than an empty result
    #: that reads as "never measured".
    base_url: str
    api_key: str
    application_key: str
    #: Logical metric name -> how it is published. From the profile's
    #: ``datadog:`` section; this class never fills it in.
    series: dict[str, DatadogSeriesMapping] = field(default_factory=dict)
    #: Tag scope pinning the query to one host, e.g. ``{"host": "box-a"}``. On
    #: the free tier there is only one host anyway, but a query without a scope
    #: silently starts averaging the moment a second one appears.
    scope: dict[str, str] = field(default_factory=dict)
    #: How far back the instant query looks. Datadog's query API is a time-range
    #: API with no instant form, so "now" has to be expressed as a short window.
    #: 300 s matches the default measured window: long enough that a sparse
    #: metric has a point in it, short enough that it is still "now".
    window_s: int = 300
    timeout_s: float = 10.0
    name: str = "datadog"
    _client: Any = None
    #: Metrics the profile declared with a unit this adapter will not convert.
    #: Surfaced rather than dropped -- ``DESIGN.md`` 4.8.
    unreadable: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.api_key or not self.application_key:
            raise DatadogError(
                "Datadog needs both an API key and an APPLICATION key. The query "
                "API rejects a request carrying only the first, and it rejects it "
                "with a 403 that is easy to mistake for an empty metric."
            )
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_profile(
        cls,
        base_url: str,
        profile: Any,
        *,
        api_key: str,
        application_key: str,
        scope: dict[str, str] | None = None,
        client: Any = None,
        timeout_s: float = 10.0,
    ) -> DatadogMetricsProvider:
        """Build from a ``TargetProfile``'s ``datadog:`` section.

        Raises when the profile has no such section, for the same reason the
        PromQL adapter does: falling back to derived names would be the guess
        this adapter exists to avoid, and failing at construction is far cheaper
        than failing eight minutes into a load run.
        """
        raw = getattr(profile, "datadog", None) or {}
        if not raw:
            raise DatadogError(
                f"profile {getattr(profile, 'name', '?')!r} declares no `datadog:` "
                "section, so this adapter has no metric names. Datadog's suffixes "
                "depend on how each metric was submitted, and deriving them here "
                "would produce plausible queries that silently return no data."
            )
        return cls(
            base_url=base_url,
            api_key=api_key,
            application_key=application_key,
            series={str(k): DatadogSeriesMapping.from_mapping(v) for k, v in raw.items()},
            scope=dict(scope or {}),
            timeout_s=timeout_s,
            _client=client,
        )

    # -- querying ---------------------------------------------------------

    def _scope_text(self, extra: dict[str, str] | None = None) -> str:
        tags = {**self.scope, **(extra or {})}
        if not tags:
            return "{*}"
        inner = ",".join(f"{key}:{value}" for key, value in sorted(tags.items()))
        return "{" + inner + "}"

    def expression(self, mapping: DatadogSeriesMapping, series_name: str, stat: str) -> str:
        """The Datadog query for one statistic of one declared series.

        The aggregator is chosen PER STATISTIC for the same reason it is in the
        PromQL adapter: 200 requests across four endpoints really is 200
        requests, so a COUNT sums -- but the slowest request in the service is
        the largest per-tag maximum, not the sum of them, and summing would
        report a latency nothing ever experienced.
        """
        if stat == "MAX":
            aggregator = "max"
        elif stat == "COUNT":
            aggregator = "sum"
        else:
            aggregator = mapping.aggregator or "avg"
        return f"{aggregator}:{series_name}{self._scope_text(mapping.tags)}"

    def query(self, expression: str, *, now: float | None = None) -> list[dict[str, Any]]:
        """Run one query and return its series list.

        Errors return an empty list rather than raising. A metrics backend being
        briefly unreachable must degrade to "we do not know", which the collector
        renders as ``null`` -- raising here would abort a campaign mid-run over a
        transient 502, and retrying silently would hide a backend that is down.
        """
        import time as _time

        end = int(now if now is not None else _time.time())
        try:
            response = self._client.get(
                f"{self.base_url}/api/v1/query",
                params={
                    "query": expression,
                    "from": end - self.window_s,
                    "to": end,
                    # Datadog accepts credentials either as these query
                    # parameters or as DD-API-KEY / DD-APPLICATION-KEY headers.
                    # The parameter form is used because it is the one the v1
                    # query API documents by these exact names.
                    "api_key": self.api_key,
                    "application_key": self.application_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if payload.get("status") not in (None, "ok"):
            return []
        return list(payload.get("series") or [])

    def scalar(self, expression: str, *, now: float | None = None) -> float | None:
        """The most recent value the query returned, or ``None``.

        ``None`` for no series, ``None`` for an empty point list, and ``None``
        for more than one series. That last case is assertion 15.4's rule
        carried across: more than one series means the scope did not pin a
        single host, and picking the first would quietly report one machine's
        numbers as the service's. On the free tier there is only one host, so a
        second series means the scope is wrong rather than that the estate grew.
        """
        series = self.query(expression, now=now)
        if len(series) != 1:
            return None
        points = series[0].get("pointlist") or []
        for timestamp_and_value in reversed(points):
            try:
                value = timestamp_and_value[1]
            except (IndexError, TypeError):
                continue
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    # -- the MetricsProvider interface ------------------------------------

    def fetch(self, name: str) -> dict[str, float] | None:
        """The raw statistic mapping for ``name``, in Actuator's shape and units.

        Timer values come back **in seconds**, converted from the declared
        source unit, because that is the contract every other adapter meets and
        the collector performs exactly one conversion for all of them.

        ``None`` when the profile does not declare the metric, when Datadog has
        no data for it, or when its declared unit is one this adapter will not
        convert. All three are honestly "we do not know", which the collector
        renders as ``null`` rather than ``0``.
        """
        mapping = self.series.get(name)
        if mapping is None:
            return None

        if mapping.is_timer:
            if mapping.unit not in KNOWN_TIMER_UNITS:
                # Declared, never guessed, never dropped. An undeclared unit is
                # the K3 failure waiting to happen in a new adapter, and a
                # silently dropped metric leaves the agent reasoning from a
                # picture whose edges it cannot see (DESIGN.md 4.8).
                self.unreadable[name] = {
                    "unit": "unknown",
                    "declared_unit": mapping.unit or "(none declared)",
                    "reason": (
                        f"profile declares unit {mapping.unit or '(none)'!r}; this adapter "
                        f"converts only {', '.join(KNOWN_TIMER_UNITS)}. Excluded from every "
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
                if value is None:
                    continue
                # COUNT is a tally and has no unit to convert; converting it
                # would turn 3186 acquisitions into 0.000003186 of them.
                if stat != "COUNT":
                    value = self.to_seconds(value, mapping.unit)
                raw[stat] = value
            return raw or None

        if mapping.is_gauge:
            value = self.scalar(self.expression(mapping, mapping.value, "VALUE"))
            return {"VALUE": value} if value is not None else None

        return None

    @staticmethod
    def to_seconds(value: float, declared_unit: str) -> float:
        """Convert a DECLARED source unit into the collector's seconds contract.

        Kept separate and static so the conversion can be asserted on its own,
        without a client: this one multiplication is the difference between a
        2406 ms connection wait and a 2.4-billion-millisecond one.
        """
        if declared_unit == "nanoseconds":
            return value * _NANOSECONDS_TO_SECONDS
        return value

    def endpoint_breakdown(self, requests_series: str = "", *, tag: str = "resource_name") -> dict[str, Any] | None:
        """Per-endpoint request statistics, or ``None`` when they cannot be had.

        ``None`` is meaningful: the snapshot's ``available_evidence`` then
        reports ``endpoint_breakdown: false``, and the agent knows it cannot
        attribute latency to one endpoint rather than assuming it is spread
        evenly.

        The tag is a parameter because Datadog has no single convention for it:
        APM traces tag by ``resource_name``, a custom Micrometer meter usually
        tags by ``uri``, and an agent check may use neither. Guessing one would
        produce an empty breakdown that looks exactly like a target which does
        not tag at all.
        """
        if not requests_series:
            return None
        scope = self._scope_text()
        counts = self.query(f"sum:{requests_series}.count{scope} by {{{tag}}}")
        totals = self.query(f"sum:{requests_series}.sum{scope} by {{{tag}}}")
        if not counts:
            return None

        def by_tag(series: list[dict[str, Any]]) -> dict[str, float]:
            out: dict[str, float] = {}
            for entry in series:
                label = _tag_value(entry, tag)
                if label is None:
                    continue
                points = entry.get("pointlist") or []
                for point in reversed(points):
                    try:
                        value = point[1]
                    except (IndexError, TypeError):
                        continue
                    if value is None:
                        continue
                    try:
                        out[label] = float(value)
                    except (TypeError, ValueError):
                        continue
                    break
            return out

        count_by_tag = by_tag(counts)
        total_by_tag = by_tag(totals)
        breakdown: dict[str, Any] = {}
        for label, count in count_by_tag.items():
            total = total_by_tag.get(label)
            # Totals share the timers' declared unit problem, and this method has
            # no per-series mapping to read it from -- so the mean is reported
            # only when a total was actually returned, and in the same seconds
            # contract the rest of the adapter honours.
            breakdown[label] = {
                "request_count": count,
                "mean_ms": (total / count * 1000.0) if total is not None and count else None,
            }
        return breakdown or None

    def free_tier_limits(self) -> dict[str, Any]:
        """What the free plan constrains, for the manifest to record.

        A campaign that ran against one day of retention is a different claim
        from one that could compare against last month, and a reader should not
        have to know Datadog's pricing page to tell them apart.
        """
        return {
            "hosts": FREE_TIER_HOSTS,
            "retention_days": FREE_TIER_RETENTION_DAYS,
            "note": (
                "Datadog free tier: one host and one day of metric retention "
                "(DESIGN.md 16). A baseline older than the retention window "
                "cannot be re-read, and a second host would make every query "
                "an average across machines."
            ),
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def _tag_value(entry: dict[str, Any], tag: str) -> str | None:
    """Pull one tag's value out of a Datadog series.

    Datadog reports the grouping either as a ``scope`` string
    (``resource_name:get_/api/db``) or as a ``tag_set`` list, depending on the
    query and the API version. Both are read, because an adapter that handled
    only one would return an empty breakdown against half of Datadog's own
    responses -- and an empty breakdown is indistinguishable from a target that
    does not tag by endpoint at all.
    """
    for candidate in entry.get("tag_set") or []:
        key, sep, value = str(candidate).partition(":")
        if sep and key == tag:
            return value
    scope = str(entry.get("scope") or "")
    for candidate in scope.split(","):
        key, sep, value = candidate.strip().partition(":")
        if sep and key == tag:
            return value
    return None
