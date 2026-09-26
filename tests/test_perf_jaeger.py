"""Jaeger trace provider assertions — week 3, new group 20. DESIGN.md §4.3, §5, §16.

DRAFTED by Claude Code. NOT YET REVIEWED by the operator.

This is GROUP 5 (evidence honesty) given a backend. The assertions that matter
are not about reading spans — they are about what the snapshot says when there
are no spans to read, because that is the case that produced the K3 attempt-1
failure: an agent that cannot tell "I looked at the spans and found nothing
slow" from "there were no spans to look at" eliminates a live hypothesis on
evidence it never gathered.

Three judgement calls for the operator:

1. **`NoTraceProvider` is an object, not a branch.** A campaign without tracing
   uses a provider that *declares* the absence, rather than call sites
   remembering to write `traces=False`. The reason is narrow and specific: the
   field a hand-written branch forgets is the sampling rate, and a missing
   sampling rate reads as full coverage.

2. **The field is `trace_sampling_rate_pct`, not `trace_sampling_rate`.** The
   week-3 brief for this deliverable names the latter. The existing
   `AvailableEvidence` dataclass already ships the former, and assertion 1.2
   (every field carries its unit) is why — `_pct` says what 10 means. Renaming
   it would break assertion 1.2 and require a COLLECTOR_VERSION bump that
   invalidates every captured snapshot, so the existing name is kept. **If you
   want the brief's name, that is a rename plus a version bump, not a one-line
   change.**

3. **An unknown sampling rate is `None`, never 100.** A provider that defaulted
   to 100% would turn "nobody told us the rate" into "we saw everything", which
   is the strongest possible version of the mistake §4.3 exists to prevent.
"""

from __future__ import annotations

import httpx
import pytest

from crucible.perf.collector import AvailableEvidence
from crucible.perf.diagnosis import summarise_evidence_gaps
from crucible.perf.providers.jaeger import (
    JaegerTraceProvider,
    NoTraceProvider,
    TraceProviderError,
    trace_evidence,
)


def _jaeger(handler, **kwargs) -> JaegerTraceProvider:
    return JaegerTraceProvider(
        base_url="http://jaeger:16686",
        service=kwargs.pop("service", "perf-lab"),
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def _trace(*spans):
    return {"data": [{"traceID": "abc", "spans": list(spans)}]}


def _span(operation: str, duration_us: int):
    return {"operationName": operation, "duration": duration_us, "startTime": 1}


# ---------------------------------------------------------------------------
# 20.1 — the absence is declared, never omitted
# ---------------------------------------------------------------------------


class TestAnAbsentTraceProviderIsDeclared:
    def test_all_three_keys_are_present_when_tracing_is_not_configured(self):
        """The requirement, stated exactly: traces False, sampling rate None,
        and NEITHER key absent. A missing key and a null value do not read the
        same way — absent suggests "not applicable", null says "not measured"."""
        evidence = trace_evidence(None)

        assert set(evidence) == {"traces", "trace_reason", "trace_sampling_rate_pct"}
        assert evidence["traces"] is False
        assert evidence["trace_sampling_rate_pct"] is None
        assert evidence["trace_reason"]

    def test_the_no_trace_provider_says_the_same_thing(self):
        assert NoTraceProvider().evidence() == trace_evidence(None)

    def test_an_unknown_rate_is_none_not_a_hundred(self):
        """A default of 100% would turn 'nobody told us' into 'we saw
        everything' — at 1% head sampling a p99 outlier is almost certainly not
        in the sample, and "no slow spans" would then be reported as proof."""
        assert NoTraceProvider().evidence()["trace_sampling_rate_pct"] is None
        assert NoTraceProvider().finding().sampling_rate_pct is None

    def test_the_evidence_dataclass_carries_both_keys_by_default(self):
        """Belt and braces: even a caller that bypasses `trace_evidence`
        entirely gets both fields, because the dataclass defaults them."""
        rendered = AvailableEvidence(metrics=True).as_dict()

        assert rendered["traces"] is False
        assert rendered["trace_sampling_rate_pct"] is None

    def test_the_model_is_told_in_words_that_it_has_no_spans(self):
        """The prose half of §4.3 — `diagnosis.summarise_evidence_gaps` turns
        the booleans into a sentence, and the sentence has to say that a cause
        cannot be ruled out on the absence of slow spans."""
        snapshot = {"available_evidence": AvailableEvidence(metrics=True).as_dict()}

        gaps = summarise_evidence_gaps(snapshot)

        assert any("No traces" in gap and "rule out" in gap for gap in gaps)


# ---------------------------------------------------------------------------
# 20.2 — a configured Jaeger
# ---------------------------------------------------------------------------


class TestAReachableJaeger:
    def test_evidence_reports_traces_available_with_its_rate(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab", "other"]})
            return httpx.Response(200, json={"data": []})

        evidence = _jaeger(handler, sampling_rate_pct=10.0).evidence()

        assert evidence["traces"] is True
        assert evidence["trace_sampling_rate_pct"] == 10.0

    def test_low_sampling_is_disclosed_to_the_model_as_a_limitation(self):
        """Assertion 5.3, end to end: at 1% the p99 outlier was probably never
        traced, so 'no slow spans' is weak evidence and the prompt says so."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            return httpx.Response(200, json={"data": []})

        evidence = _jaeger(handler, sampling_rate_pct=1.0).evidence()
        snapshot = {
            "available_evidence": AvailableEvidence(
                metrics=True,
                traces=evidence["traces"],
                trace_sampling_rate_pct=evidence["trace_sampling_rate_pct"],
            ).as_dict()
        }

        gaps = summarise_evidence_gaps(snapshot)

        assert any("sampl" in gap.lower() for gap in gaps)

    def test_span_durations_are_converted_from_microseconds(self):
        """Jaeger reports duration in MICROSECONDS. A 2406 ms span arrives as
        2_406_000, and an adapter that passed it through would hand the model a
        number three orders of magnitude wrong — the K3 unit failure in the one
        place the collector's conversion does not reach."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            return httpx.Response(200, json=_trace(_span("GET /api/db", 2_406_000)))

        finding = _jaeger(handler).finding(now_us=1_700_000_000_000_000)

        assert finding.slowest_span_ms == pytest.approx(2406.0)
        assert finding.slowest_operation == "GET /api/db"

    def test_slow_spans_are_counted_against_the_declared_threshold(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            return httpx.Response(
                200,
                json=_trace(
                    _span("fast", 5_000),          # 5 ms
                    _span("slow", 250_000),        # 250 ms
                    _span("slower", 900_000),      # 900 ms
                ),
            )

        finding = _jaeger(handler, slow_span_threshold_ms=100.0).finding()

        assert finding.slow_span_count == 2
        assert finding.by_operation_ms["slower"] == pytest.approx(900.0)
        assert finding.traces_examined == 1

    def test_the_query_is_scoped_to_the_service_and_the_window(self):
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"data": []})

        _jaeger(handler, lookback_s=300).finding(now_us=1_000_000_000)

        assert captured["service"] == "perf-lab"
        assert int(captured["end"]) - int(captured["start"]) == 300 * 1_000_000


# ---------------------------------------------------------------------------
# 20.3 — the failure modes that must not look like a clean result
# ---------------------------------------------------------------------------


class TestAnUnreachableOrUnknownServiceIsNotACleanBillOfHealth:
    def test_an_unreachable_jaeger_reports_traces_false_with_a_reason(self):
        """A tracing backend being down is a gap in the evidence, not a reason
        to abandon a campaign whose metrics are fine — and definitely not a
        reason to report 'no slow spans found'."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        evidence = _jaeger(handler, sampling_rate_pct=100.0).evidence()

        assert evidence["traces"] is False
        assert evidence["trace_sampling_rate_pct"] is None
        assert "not measured" in evidence["trace_reason"]

    def test_a_jaeger_that_never_heard_of_the_service_is_also_traces_false(self):
        """The strongest version of the K3 mistake available here: Jaeger is
        up, answers instantly, and returns nothing — because the service name
        is wrong. Reporting that as 'traces available, none slow' would let the
        agent rule out a downstream cause on a typo."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["some-other-service"]})
            return httpx.Response(200, json={"data": []})

        evidence = _jaeger(handler).evidence()

        assert evidence["traces"] is False
        assert "perf-lab" in evidence["trace_reason"]

    def test_a_query_failure_returns_an_empty_finding_not_an_exception(self):
        """Raising would abort a campaign over a trace backend hiccup,
        discarding metrics already measured."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            return httpx.Response(500, text="boom")

        finding = _jaeger(handler, sampling_rate_pct=10.0).finding()

        assert finding.slow_span_count == 0
        assert finding.slowest_span_ms is None
        # The rate survives the failure: it describes the INSTRUMENTATION, not
        # the query, so it is still true when the query failed.
        assert finding.sampling_rate_pct == 10.0

    def test_a_missing_service_name_fails_at_construction(self):
        """A wrong or empty service name returns an empty result that looks
        exactly like a service with no slow spans, so it is refused up front
        rather than producing a plausible nothing."""
        with pytest.raises(TraceProviderError, match="service name"):
            JaegerTraceProvider(base_url="http://jaeger:16686", service="")

    def test_a_malformed_span_is_skipped_not_fatal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/services":
                return httpx.Response(200, json={"data": ["perf-lab"]})
            return httpx.Response(
                200, json=_trace({"operationName": "broken"}, _span("ok", 150_000))
            )

        finding = _jaeger(handler).finding()

        assert finding.slow_span_count == 1
        assert finding.slowest_operation == "ok"
