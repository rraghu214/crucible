"""Jaeger as a trace provider, and the honest absence of one.

Traces are **optional** in Crucible (``DESIGN.md`` section 5's adapter table lists
``TraceProvider`` as optional, with ``none`` as a legitimate implementation), and
that optionality is the whole reason this module is shaped the way it is. An
agent that cannot tell "I looked at the spans and found nothing slow" from "there
were no spans to look at" will eliminate a live hypothesis on evidence it never
gathered -- which is exactly what happened in K3 attempt 1 (section 4.3).

So there are two classes here and they are equally important:

:class:`JaegerTraceProvider` reads a real Jaeger, and :class:`NoTraceProvider` is
what a campaign uses when tracing is not configured. The second is not a stub. It
is the declaration that makes the absence visible: it reports ``traces=False``, a
``trace_reason`` saying why, and a sampling rate of ``None``. Making "no tracing"
an object rather than a skipped branch is what stops those three fields from
being quietly omitted at one of the call sites -- an omitted field reads as
absent, and absent reads as "not applicable" rather than "not measured".

**The sampling rate is evidence about the evidence.** Most production tracing
runs at 1-10% head sampling, and a p99 outlier is by definition rare, so "no slow
spans were found" can be false on a fully instrumented deployment (section 4.3).
A provider that reported traces as simply present or absent would let the agent
treat a 1% sample as proof. So the rate travels with the finding, and when it
cannot be determined it is ``None`` -- never assumed to be 100%.

**Jaeger reports span durations in MICROSECONDS.** Converted here, with every
field named for its unit, for the same reason the collector converts Micrometer's
seconds: no raw duration reaches the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

#: Jaeger's query API expresses times in microseconds, and span ``duration`` with
#: them. One conversion, in one place, named for what it does.
_MICROSECONDS_TO_MS = 1e-3


class TraceProviderError(RuntimeError):
    """The trace backend was configured unusably. Not raised for an outage."""


class TraceProvider(Protocol):
    """One tracing backend, or the declared absence of one.

    Every implementation answers :meth:`evidence` -- that is the contract the
    collector depends on, because ``available_evidence`` must carry the same keys
    whether tracing was configured or not.
    """

    name: str

    def evidence(self) -> dict[str, Any]: ...


@dataclass
class TraceFinding:
    """What the spans actually said, in milliseconds, with its own limits attached.

    ``sampling_rate_pct`` is carried on the finding rather than alongside it
    because the two are only meaningful together: "no slow spans" at 100% is a
    strong negative and at 1% is almost none, and a caller that could pick up one
    without the other would eventually report the first as though it were the
    second.
    """

    slow_span_count: int = 0
    slowest_span_ms: float | None = None
    slowest_operation: str = ""
    #: Operation name -> its slowest observed duration, so the agent can see
    #: WHERE the time went rather than only that it went somewhere.
    by_operation_ms: dict[str, float] = field(default_factory=dict)
    traces_examined: int = 0
    sampling_rate_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slow_span_count": self.slow_span_count,
            "slowest_span_ms": self.slowest_span_ms,
            "slowest_operation": self.slowest_operation,
            "by_operation_ms": dict(self.by_operation_ms),
            "traces_examined": self.traces_examined,
            "sampling_rate_pct": self.sampling_rate_pct,
        }


@dataclass
class NoTraceProvider:
    """The declared absence of tracing. Used when Jaeger is not configured.

    This exists so that "we have no traces" is a statement the snapshot makes,
    rather than a silence the reader has to interpret. ``traces`` is ``False``
    and ``trace_sampling_rate_pct`` is ``None`` -- **both keys present**, because
    a missing key and a null value do not read the same way to a model, and the
    difference is precisely the one section 4.3 exists to protect.
    """

    name: str = "none"
    reason: str = "no trace provider configured for this campaign"

    def evidence(self) -> dict[str, Any]:
        return {
            "traces": False,
            "trace_reason": self.reason,
            # Not 100, and not omitted. There is no sampling rate because there
            # is no sampling: "unknown" is the honest value, and it is what stops
            # a caller from rendering a default of 100% and implying full coverage.
            "trace_sampling_rate_pct": None,
        }

    def finding(self, **_kwargs: Any) -> TraceFinding:
        """No spans, and a finding that says so rather than an empty success."""
        return TraceFinding(sampling_rate_pct=None)

    def close(self) -> None:
        return None


@dataclass
class JaegerTraceProvider:
    """A Jaeger query endpoint (``/api/traces``, ``/api/services``).

    Free and self-hosted in a container, which is why it is the trace backend in
    scope for the capstone (``DESIGN.md`` section 16) ahead of Tempo or Datadog APM.
    """

    base_url: str
    #: The service name as Jaeger knows it. Declared, never derived from the
    #: target URL: Jaeger's service name comes from the application's own
    #: resource attributes, and guessing it returns an empty result that is
    #: indistinguishable from a service with no slow spans.
    service: str
    #: Head sampling rate, as a percentage, declared by whoever configured the
    #: SDK. ``None`` means nobody told us -- which is reported as unknown rather
    #: than assumed to be 100%, because assuming 100% turns a 1% sample into a
    #: clean bill of health.
    sampling_rate_pct: float | None = None
    #: A span slower than this is worth the agent's attention. Milliseconds,
    #: like every other duration Crucible carries.
    slow_span_threshold_ms: float = 100.0
    lookback_s: int = 300
    limit: int = 50
    timeout_s: float = 10.0
    name: str = "jaeger"
    _client: Any = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.service:
            raise TraceProviderError(
                "JaegerTraceProvider needs the service name Jaeger knows the "
                "target by. It comes from the application's own resource "
                "attributes and cannot be derived from the target URL; a wrong "
                "name returns an empty result that looks exactly like a service "
                "with no slow spans."
            )
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)

    # -- reachability -----------------------------------------------------

    def reachable(self) -> bool:
        """Whether Jaeger answered and knows this service.

        Both halves matter. A reachable Jaeger that has never heard of the
        service yields no spans, and reporting that as "traces available, none
        slow" would be the strongest possible version of the K3 mistake.
        """
        try:
            response = self._client.get(f"{self.base_url}/api/services")
            response.raise_for_status()
            services = (response.json() or {}).get("data") or []
        except (httpx.HTTPError, ValueError):
            return False
        return self.service in services

    # -- the TraceProvider interface --------------------------------------

    def evidence(self) -> dict[str, Any]:
        """The three ``available_evidence`` keys, always all three.

        When Jaeger is unreachable this reports ``traces: False`` with the
        reason, rather than raising: a tracing backend being down is a gap in
        the evidence, not a reason to abandon a campaign whose metrics are fine.
        """
        if not self.reachable():
            return {
                "traces": False,
                "trace_reason": (
                    f"Jaeger at {self.base_url} did not answer, or does not know "
                    f"service {self.service!r}. Spans may exist and simply not be "
                    "reachable from here -- this is 'not measured', not 'none found'."
                ),
                "trace_sampling_rate_pct": None,
            }
        return {
            "traces": True,
            "trace_reason": "",
            # Carried even when traces ARE available, because at 1-10% head
            # sampling "no slow spans" is weak evidence and the agent is told so
            # explicitly (see diagnosis.summarise_evidence_gaps).
            "trace_sampling_rate_pct": self.sampling_rate_pct,
        }

    def finding(self, *, now_us: int | None = None) -> TraceFinding:
        """The slowest spans in the lookback window, in milliseconds.

        Returns an empty finding when Jaeger has nothing -- and the caller must
        read that alongside :meth:`evidence`, because an empty finding with
        ``traces: False`` means "we never looked" while the same finding with
        ``traces: True`` at 100% sampling means "we looked and it was clean".
        """
        import time as _time

        end = int(now_us if now_us is not None else _time.time() * 1_000_000)
        try:
            response = self._client.get(
                f"{self.base_url}/api/traces",
                params={
                    "service": self.service,
                    "start": end - self.lookback_s * 1_000_000,
                    "end": end,
                    "limit": self.limit,
                },
            )
            response.raise_for_status()
            traces = (response.json() or {}).get("data") or []
        except (httpx.HTTPError, ValueError):
            return TraceFinding(sampling_rate_pct=self.sampling_rate_pct)

        finding = TraceFinding(
            traces_examined=len(traces), sampling_rate_pct=self.sampling_rate_pct
        )
        for trace in traces:
            for span in trace.get("spans") or []:
                try:
                    duration_ms = float(span["duration"]) * _MICROSECONDS_TO_MS
                except (KeyError, TypeError, ValueError):
                    continue
                operation = str(span.get("operationName") or "(unnamed)")
                if duration_ms > finding.by_operation_ms.get(operation, -1.0):
                    finding.by_operation_ms[operation] = duration_ms
                if duration_ms >= self.slow_span_threshold_ms:
                    finding.slow_span_count += 1
                if finding.slowest_span_ms is None or duration_ms > finding.slowest_span_ms:
                    finding.slowest_span_ms = duration_ms
                    finding.slowest_operation = operation
        return finding

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def trace_evidence(provider: TraceProvider | None) -> dict[str, Any]:
    """The trace half of ``available_evidence``, for any provider or for none.

    ``None`` is accepted and produces :class:`NoTraceProvider`'s answer. That is
    deliberate: a caller that has not been given a trace provider should not have
    to remember to write three fields by hand, because the one it forgets will be
    the sampling rate, and a missing sampling rate reads as full coverage.
    """
    if provider is None:
        return NoTraceProvider().evidence()
    return provider.evidence()
