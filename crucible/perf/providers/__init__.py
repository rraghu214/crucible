"""Metrics provider adapters.

One interface, several backends (``DESIGN.md`` section 5). Actuator lands first
because it needs nothing but the target itself; PromQL follows because one
adapter covers Prometheus, Mimir, Thanos, VictoriaMetrics, Chronosphere and the
three managed Prometheus services -- roughly half the observability market.

They are not interchangeable in what they can *prove*. Actuator exposes one
instance's pre-aggregated percentiles, so a campaign using it pins load to a
single instance and declares that as a limitation. Prometheus holds histogram
buckets, so true service-wide percentiles are available. Each adapter states
which it is through ``SUPPORTS_SERVICE_WIDE_PERCENTILES`` rather than leaving a
caller to assume.
"""

from .actuator import ActuatorMetricsProvider
from .promql import PromQLError, PromQLMetricsProvider, SeriesMapping

__all__ = [
    "ActuatorMetricsProvider",
    "PromQLError",
    "PromQLMetricsProvider",
    "SeriesMapping",
]
