"""Turn raw provider metrics into the snapshot the model is allowed to see.

Everything the agent concludes rests on this module being right, and the K3
feasibility spike already proved what happens when it is not (``docs/K3_RESULT.md``).
Four rules come straight out of that failure and are not negotiable:

1. **Units are converted here, never by the model.** Micrometer reports timers in
   *seconds*. Handed the raw tuple, the model read ``acquire MAX: 2.4`` as 2.4 ms --
   a healthy wait -- and ruled the connection pool out. The true value was 2406 ms.
   No raw ``COUNT``/``TOTAL_TIME``/``MAX`` tuple ever reaches the model, and every
   derived field name ends in its unit.
2. **Gauges carry their peak during load, not their value after.** ``pending`` reads
   0 the moment load drains; during the run it was 43.
3. **An unsampled gauge is ``None``, never ``0``.** A clean zero and an untested zero
   must not look identical.
4. **``available_evidence`` travels with every snapshot**, so the agent can tell "I
   looked and found nothing" from "I never looked".

Plus one rule from ``DESIGN.md`` section 7: every snapshot carries
:data:`COLLECTOR_VERSION`, because snapshots are replayed hundreds of times and a
collector change bakes wrong numbers into every old one.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

#: Bumped whenever a field is added, renamed, or its arithmetic changes.
#: The eval runner refuses snapshots that do not match (``DESIGN.md`` section 7).
COLLECTOR_VERSION = "1.1.0"

#: Micrometer timers are reported in seconds. This is the whole of the K3 fix.
_SECONDS_TO_MS = 1000.0

#: Every derived field must end in one of these. Enforced by test, not by code,
#: so a new unit is a deliberate decision rather than a silent addition.
#:
#: ``_count`` is reserved for genuine tallies -- "3186 acquisitions happened".
#: A gauge is a LEVEL, not a tally, so it carries the noun of the thing being
#: counted instead: ``pending_peak_connections``, ``threads_live_peak_threads``.
#: Naming a level ``_count`` invites exactly the misreading the unit rule exists
#: to prevent -- that 43 is a running total rather than 43 waiters at one instant.
UNIT_SUFFIXES = ("_ms", "_pct", "_count", "_bytes", "_rps", "_connections", "_threads")


class MetricsProvider(Protocol):
    """One metrics backend. Actuator and PromQL both satisfy this.

    ``fetch`` returns the raw statistic mapping for a metric -- for Actuator that
    is ``{"COUNT": .., "TOTAL_TIME": .., "MAX": ..}`` -- or ``None`` when the
    metric does not exist. ``None`` and an all-zero mapping mean different things
    and must stay distinguishable all the way to the snapshot.
    """

    def fetch(self, name: str) -> dict[str, float] | None: ...


# ---------------------------------------------------------------------------
# Pure arithmetic. No I/O here, so every rule above is testable without a target.
# ---------------------------------------------------------------------------


def seconds_to_ms(value: float | None) -> float | None:
    """Convert a Micrometer timer value to milliseconds. ``None`` stays ``None``."""
    if value is None:
        return None
    return float(value) * _SECONDS_TO_MS


def peak_during_load(samples: list[float] | None) -> float | None:
    """The highest value a gauge reached while load ran.

    Returns ``None`` for an empty or absent sample list. That is rule 3: the
    sampler never ran, so we do not know the value -- which is not the same claim
    as "the value was zero".
    """
    if not samples:
        return None
    return max(samples)


def trough_during_load(samples: list[float] | None) -> float | None:
    """The lowest value a gauge reached while load ran (``idle`` bottoming out)."""
    if not samples:
        return None
    return min(samples)


def compute_window(at_warmup_end: dict[str, float], at_measure_end: dict[str, float]) -> dict[str, Any]:
    """Metrics for the measurement window only, as a delta across it.

    A JVM runs slowly for its first few thousand requests while it compiles hot
    code. Counting from process start makes every fixture look worse, and worse by
    an inconsistent amount, so two fixtures stop being comparable.

    ``MAX`` is the exception and gets its own name. Micrometer's ``MAX`` is a
    *rolling* maximum over roughly the last two minutes, and it forgets: a 1800 ms
    acquire at t=200s in a 120-420s window has aged out by the time the window
    ends, so the field would read 250 ms and the agent would conclude there is no
    tail-latency problem. Subtracting is worse still -- end minus start goes
    negative. So it is carried as ``max_recent_ms``, never ``max_ms``: the name
    says it is the recent maximum, not the window maximum. Sustained causes are
    unaffected (recent max approximates window max when the problem never stops);
    transient ones -- GC pauses especially -- are where this under-reports.
    """
    count = float(at_measure_end.get("COUNT", 0.0)) - float(at_warmup_end.get("COUNT", 0.0))
    total_s = float(at_measure_end.get("TOTAL_TIME", 0.0)) - float(at_warmup_end.get("TOTAL_TIME", 0.0))
    total_ms = total_s * _SECONDS_TO_MS
    window: dict[str, Any] = {
        "count": count,
        "total_ms": total_ms,
        "mean_ms": (total_ms / count) if count > 0 else None,
        "max_recent_ms": seconds_to_ms(at_measure_end.get("MAX")),
        "max_recent_ms_note": (
            "rolling ~2-minute maximum read at end of window, NOT the maximum over "
            "the measured window. Micrometer's MAX decays, so a spike early in the "
            "window has already aged out. Under-reports transient causes such as GC "
            "pauses; approximates the true window max only for sustained ones."
        ),
    }
    return window


def derive_timer_ms(raw: dict[str, float] | None, prefix: str) -> dict[str, Any]:
    """Convert one Micrometer timer tuple into named, unit-suffixed milliseconds.

    The division is done here so the model never performs it. ``mean`` is
    ``TOTAL_TIME / COUNT``, which is the number the K3 spike needed and did not
    have: 3499.07 s over 3186 acquisitions is 1098 ms, not the 2.4 the raw tuple
    appeared to say.

    ``MAX`` becomes ``{prefix}_max_recent_ms`` rather than ``{prefix}_max_ms``
    because it is a rolling maximum that decays -- see :func:`compute_window`.
    """
    if not raw:
        return {
            f"{prefix}_count": None,
            f"{prefix}_mean_ms": None,
            f"{prefix}_max_recent_ms": None,
            f"{prefix}_total_ms": None,
        }
    count = float(raw.get("COUNT", 0.0))
    total_ms = seconds_to_ms(raw.get("TOTAL_TIME", 0.0))
    return {
        f"{prefix}_count": count,
        f"{prefix}_mean_ms": (total_ms / count) if count > 0 and total_ms is not None else None,
        f"{prefix}_max_recent_ms": seconds_to_ms(raw.get("MAX")),
        f"{prefix}_total_ms": total_ms,
    }


def build_derived_hikari(
    acquire_raw: dict[str, float] | None,
    creation_raw: dict[str, float] | None = None,
    gauge_samples: dict[str, list[float]] | None = None,
    pool_max: float | None = None,
) -> dict[str, Any]:
    """The HikariCP view the model reads: converted timers plus sampled gauge peaks.

    ``gauge_samples`` comes from the load runner's background sampler. Anything it
    did not sample is ``None`` here, never ``0`` -- see rule 3.
    """
    samples = gauge_samples or {}
    derived: dict[str, Any] = {}
    derived.update(derive_timer_ms(acquire_raw, "acquire"))
    derived.update(derive_timer_ms(creation_raw, "creation"))
    derived["pending_peak_connections"] = peak_during_load(samples.get("pending"))
    derived["active_peak_connections"] = peak_during_load(samples.get("active"))
    derived["idle_trough_connections"] = trough_during_load(samples.get("idle"))
    derived["pool_max_connections"] = float(pool_max) if pool_max is not None else None

    active_peak = derived["active_peak_connections"]
    if active_peak is not None and pool_max:
        derived["utilisation_peak_pct"] = 100.0 * active_peak / float(pool_max)
    else:
        derived["utilisation_peak_pct"] = None

    unsampled = [name for name in ("pending", "active", "idle") if not samples.get(name)]
    if unsampled:
        derived["note"] = (
            f"not sampled during load: {', '.join(unsampled)}. "
            "null means the sampler never ran, not that the value was zero."
        )
    return derived


# ---------------------------------------------------------------------------
# Evidence and redaction
# ---------------------------------------------------------------------------


@dataclass
class AvailableEvidence:
    """What the collector actually managed to gather, declared explicitly.

    Without this the agent eliminates live hypotheses on evidence it never
    gathered -- exactly K3 attempt 1 (``DESIGN.md`` section 4.3).

    ``trace_sampling_rate_pct`` matters on its own: most production tracing runs at
    1-10% head sampling and a p99 outlier is by definition rare, so "no slow spans"
    can be false even on a fully instrumented deployment.
    """

    metrics: bool = False
    traces: bool = False
    trace_reason: str = "no trace provider configured"
    trace_sampling_rate_pct: float | None = None
    endpoint_breakdown: bool = False
    gauge_sampling: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_runtime_config(
    properties: dict[str, Any] | None,
    allowlist: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Keep only allowlisted properties. Allowlist, never blocklist.

    ``/actuator/env`` returns every property on the target, including datasource
    passwords and API keys, and snapshots go to a model and into journals that get
    replayed hundreds of times (``DESIGN.md`` section 12). A blocklist fails open on
    the one key nobody thought of; an allowlist fails closed. This runs before the
    journal write, not after.
    """
    allowed = set(allowlist)
    source = properties or {}
    kept = {key: value for key, value in source.items() if key in allowed}
    dropped = len(source) - len(kept)
    if dropped:
        kept["_redacted_count"] = dropped
    return kept


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def build_snapshot(
    metrics: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    profile_name: str = "",
    target: str = "",
    gauge_samples: dict[str, list[float]] | None = None,
    load_summary: dict[str, Any] | None = None,
    runtime_config: dict[str, Any] | None = None,
    redaction_allowlist: tuple[str, ...] | list[str] = (),
    endpoint_breakdown: dict[str, Any] | None = None,
    baseline_reference: dict[str, Any] | None = None,
    evidence: AvailableEvidence | None = None,
    window: dict[str, Any] | None = None,
    captured_at_epoch_s: float | None = None,
) -> dict[str, Any]:
    """Assemble the snapshot handed to the model.

    ``metrics`` holds raw provider tuples keyed by metric name (Actuator naming).
    Nothing raw survives into the result: every timer is converted here, and every
    gauge comes from ``gauge_samples`` rather than from a post-run reading.
    """
    raw = metrics or {}
    samples = gauge_samples or {}
    pool_max_raw = raw.get("hikaricp.connections.max") or {}
    pool_max = pool_max_raw.get("VALUE")

    evidence = evidence or AvailableEvidence(
        metrics=bool(raw),
        endpoint_breakdown=bool(endpoint_breakdown),
        gauge_sampling=any(samples.values()),
    )
    if not any(samples.values()):
        evidence.notes.append(
            "gauges were not sampled during load; every gauge peak is null, not zero"
        )

    return {
        "collector_version": COLLECTOR_VERSION,
        "captured_at_epoch_s": captured_at_epoch_s if captured_at_epoch_s is not None else time.time(),
        "run_id": run_id,
        "profile": profile_name,
        "target": target,
        "window": window or {},
        "load_summary": load_summary or {},
        "hikaricp": build_derived_hikari(
            raw.get("hikaricp.connections.acquire"),
            raw.get("hikaricp.connections.creation"),
            samples,
            pool_max,
        ),
        "jvm": {
            **derive_timer_ms(raw.get("jvm.gc.pause"), "gc_pause"),
            "heap_used_peak_bytes": peak_during_load(samples.get("heap_used")),
            "threads_live_peak_threads": peak_during_load(samples.get("threads_live")),
        },
        "http": derive_timer_ms(raw.get("http.server.requests"), "request"),
        "endpoint_breakdown": endpoint_breakdown,
        "runtime_config": redact_runtime_config(runtime_config, redaction_allowlist),
        "baseline_reference": baseline_reference or {},
        "available_evidence": evidence.as_dict(),
    }
