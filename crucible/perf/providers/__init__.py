"""Metrics provider adapters.

One interface, several backends (``DESIGN.md`` section 5). Actuator lands first
because it needs nothing but the target itself; PromQL follows because one
adapter covers Prometheus, Mimir, Thanos, VictoriaMetrics, Chronosphere and the
three managed Prometheus services -- roughly half the observability market.

Datadog lands third: the first backend with a proprietary query language, which
is what proves the seam survives a provider that shares neither PromQL's API nor
Micrometer's units.

They are not interchangeable in what they can *prove*. Actuator exposes one
instance's pre-aggregated percentiles, so a campaign using it pins load to a
single instance and declares that as a limitation. Prometheus and Datadog hold
the underlying distributions, so true service-wide percentiles are available.
Each adapter states which it is through ``SUPPORTS_SERVICE_WIDE_PERCENTILES``
rather than leaving a caller to assume.

Nor are they interchangeable in what they *cost to read wrong*. Actuator and
PromQL both publish timers in seconds; Datadog publishes them in nanoseconds,
and normalises to seconds inside its own adapter so the collector keeps doing
exactly one conversion for every backend. A per-series ``unit:`` declaration in
``profile.yaml`` is what makes that conversion a fact rather than a guess.

Tracing is a separate, OPTIONAL interface (``DESIGN.md`` section 5 lists ``none``
as a legitimate implementation). ``jaeger.py`` holds both the real provider and
:class:`~crucible.perf.providers.jaeger.NoTraceProvider`, which is what a campaign
without tracing uses -- an object that declares the absence rather than a branch
that skips the fields, because a missing key and a null value do not read the same
way to a model.
"""

from .actuator import ActuatorMetricsProvider
from .datadog import DatadogError, DatadogMetricsProvider, DatadogSeriesMapping
from .jaeger import (
    JaegerTraceProvider,
    NoTraceProvider,
    TraceFinding,
    TraceProvider,
    TraceProviderError,
    trace_evidence,
)
from .promql import PromQLError, PromQLMetricsProvider, SeriesMapping

__all__ = [
    "ActuatorMetricsProvider",
    "DatadogError",
    "DatadogMetricsProvider",
    "DatadogSeriesMapping",
    "JaegerTraceProvider",
    "NoTraceProvider",
    "PromQLError",
    "PromQLMetricsProvider",
    "SeriesMapping",
    "TraceFinding",
    "TraceProvider",
    "TraceProviderError",
    "trace_evidence",
]
