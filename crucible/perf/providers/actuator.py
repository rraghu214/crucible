"""Spring Boot Actuator as a metrics provider.

Reads ``/actuator/metrics/{name}`` and returns the raw statistic mapping unchanged
-- ``{"COUNT": .., "TOTAL_TIME": .., "MAX": ..}`` for a timer, ``{"VALUE": ..}``
for a gauge. Conversion belongs to the collector, not here: an adapter that
converted units would have to be re-audited for every backend, and the K3 failure
would then have four places to hide instead of one.

**Known limitation, declared rather than worked around.** Actuator exposes
pre-aggregated percentiles for one instance. p99 of instance A and p99 of instance
B do not combine into a service p99 -- that needs the underlying histogram. So an
Actuator-backed campaign pins load to a single instance and bypasses the load
balancer. Prometheus, Datadog and Dynatrace hold histogram buckets and do not have
this restriction (``DESIGN.md`` section 5).
"""

from __future__ import annotations

from typing import Any

import httpx

#: True percentiles across instances need histogram buckets, which Actuator does
#: not expose. Campaigns read this to decide whether multi-instance load is valid.
SUPPORTS_SERVICE_WIDE_PERCENTILES = False


class ActuatorMetricsProvider:
    """One Spring Boot instance's Actuator endpoint."""

    name = "actuator"

    def __init__(self, base_url: str, timeout_s: float = 5.0, client: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)

    def fetch(self, name: str) -> dict[str, float] | None:
        """The raw statistic mapping for ``name``, or ``None`` if it does not exist.

        ``None`` is returned for a missing metric and for a failed read. Both mean
        "we do not know", which the collector renders as ``null`` rather than
        ``0`` -- the distinction the agent needs to avoid ruling a cause out on
        evidence it never gathered.
        """
        try:
            response = self._client.get(f"{self.base_url}/actuator/metrics/{name}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        measurements = payload.get("measurements") or []
        return {m["statistic"]: float(m["value"]) for m in measurements if "statistic" in m}

    def fetch_env(self, name: str) -> Any | None:
        """One resolved configuration property from ``/actuator/env``.

        Callers must pass the result through
        :func:`crucible.perf.collector.redact_runtime_config` before it reaches a
        model or a journal: ``/actuator/env`` returns datasource passwords and API
        keys alongside pool sizes.
        """
        try:
            response = self._client.get(f"{self.base_url}/actuator/env/{name}")
            response.raise_for_status()
            prop = response.json().get("property")
        except (httpx.HTTPError, ValueError):
            return None
        return prop.get("value") if prop else None

    def endpoint_breakdown(self) -> dict[str, Any] | None:
        """Per-URI request statistics, or ``None`` when the target does not tag by URI.

        Returning ``None`` matters: the snapshot's ``available_evidence`` then says
        ``endpoint_breakdown: false``, and the agent knows it cannot attribute
        latency to one endpoint rather than assuming it is spread evenly.
        """
        try:
            response = self._client.get(f"{self.base_url}/actuator/metrics/http.server.requests")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        uris = next(
            (tag.get("values") for tag in payload.get("availableTags", []) if tag.get("tag") == "uri"),
            None,
        )
        if not uris:
            return None
        breakdown: dict[str, Any] = {}
        for uri in uris:
            try:
                response = self._client.get(
                    f"{self.base_url}/actuator/metrics/http.server.requests",
                    params={"tag": f"uri:{uri}"},
                )
                response.raise_for_status()
                measurements = response.json().get("measurements") or []
            except (httpx.HTTPError, ValueError):
                continue
            raw = {m["statistic"]: float(m["value"]) for m in measurements if "statistic" in m}
            count = raw.get("COUNT", 0.0)
            total_ms = raw.get("TOTAL_TIME", 0.0) * 1000.0
            breakdown[uri] = {
                "request_count": count,
                "mean_ms": (total_ms / count) if count else None,
                "max_ms": raw.get("MAX", 0.0) * 1000.0,
            }
        return breakdown or None

    def close(self) -> None:
        self._client.close()
