"""Metrics provider adapters.

One interface, several backends (``DESIGN.md`` section 5). Actuator lands first
because it needs nothing but the target itself; PromQL follows in week 2 because
one adapter covers Prometheus, Mimir, Thanos, VictoriaMetrics, Chronosphere and
the three managed Prometheus services.
"""

from .actuator import ActuatorMetricsProvider

__all__ = ["ActuatorMetricsProvider"]
