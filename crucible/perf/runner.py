"""The load generator, and the mid-run gauge sampler that goes with it.

Two things happen here that the collector cannot do for itself, because both need
to happen *while load is running*:

**Gauges are sampled during load.** ``hikaricp.connections.pending`` reads 0 the
moment load drains. In the K3 spike the model saw that zero, concluded nothing was
waiting for a connection, and crossed the pool off -- during the run the value had
been 43. :class:`GaugeSampler` runs in a background thread for the measured window
and hands the collector a list of readings per gauge. An unsampled gauge yields an
empty list, which the collector turns into ``None`` rather than ``0``.

**Warmup is discarded, not averaged in.** A JVM runs slowly for its first few
thousand requests while it compiles hot code. The runner therefore runs load in two
phases -- a warmup phase whose statistics are thrown away entirely, then the
measured phase -- so warmup requests are absent from the percentiles rather than
merely outnumbered by later ones. Scenarios own duration and repeats; a plan cannot
shorten a scenario without changing what is measured (``DESIGN.md`` section 7).

This module builds its command from configuration only -- never from model output --
so it does not route through ``crucible.coding.exec``, which exists to bound
commands the *agent* wrote and caps them at ten minutes. A 300 s measured window
plus warmup would not fit under that cap, and the two risks are not the same one.
The no-shell discipline is kept: the command is an argument list and is never
handed to a shell.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .collector import MetricsProvider

# NO METRIC NAMES LIVE IN THIS FILE. Which gauges to sample and which timers to
# bracket are declared by the TargetProfile (`gauges:` and `window_timers:` in
# profile.yaml), for the same reason cause families are: a metric name is a
# property of the runtime, not of Crucible. `jvm.memory.used` does not exist on
# CPython, and a sampler that asked a FastAPI target for JVM meters would get
# nothing back and record every gauge as "not measured" -- leaving the agent to
# report, correctly but for a buggy reason, that it never looked.
#
# GaugeSampler therefore takes its gauge map as a required argument rather than
# defaulting to one. A default is how a runtime constant creeps back in.


class LoadRunnerError(RuntimeError):
    """The load generator could not be run, or did not produce usable statistics."""


@dataclass(frozen=True)
class Scenario:
    """One load profile. Owned by the collection, never writable by the agent.

    Duration and repeats live here rather than on the plan: a plan that could
    shorten a scenario would change what was measured while claiming to be the
    same experiment. ``push_beyond`` marks a ceiling probe -- a scenario sent to
    find where things break -- on which the watchdog's error tripwires are
    suspended, because aborting on a high error rate discards the answer
    (``DESIGN.md`` section 6).
    """

    name: str
    host: str
    users: int = 50
    spawn_rate: float = 10.0
    warmup_s: float = 120.0
    measure_s: float = 300.0
    repeats: int = 1
    locustfile: str = "locust/locustfile.py"
    tags: tuple[str, ...] = ()
    push_beyond: bool = False
    expect_possible_failure: bool = False
    gauge_sample_interval_s: float = 1.0


@dataclass
class LoadResult:
    """What one measured run produced. Latencies in milliseconds, throughout."""

    run_id: str
    scenario: str
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    mean_ms: float | None = None
    # A true maximum over the measured run: the load generator keeps every sample
    # and does not decay. Deliberately NOT named max_recent_ms -- the collector's
    # Micrometer-derived field is a rolling max that forgets, and the two must not
    # look like the same quantity under different names.
    max_ms: float | None = None
    rps: float | None = None
    request_count: int = 0
    failure_count: int = 0
    error_rate_pct: float | None = None
    warmup_s: float = 0.0
    measure_s: float = 0.0
    ramp_s: float = 0.0
    gauge_samples: dict[str, list[float]] = field(default_factory=dict)
    metrics_at_warmup_end: dict[str, Any] = field(default_factory=dict)
    metrics_at_measure_end: dict[str, Any] = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str = ""
    #: The watchdog's record of the seven tripwires across this window, when one
    #: supervised it (:mod:`crucible.perf.watchdog`). Present on a clean run too:
    #: the observed CPU steal margin is what tells a later reader how much to
    #: trust the p99 beside it, and section 6 requires it either way.
    watchdog: dict[str, Any] | None = None

    def as_load_summary(self) -> dict[str, Any]:
        """The subset the snapshot's ``load_summary`` carries."""
        return {
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "mean_ms": self.mean_ms,
            "max_ms": self.max_ms,
            "rps": self.rps,
            "request_count": self.request_count,
            "failure_count": self.failure_count,
            "error_rate_pct": self.error_rate_pct,
            "warmup_s": self.warmup_s,
            "measure_s": self.measure_s,
            "ramp_s": self.ramp_s,
        }


class GaugeSampler:
    """Poll gauges on a background thread for the duration of the measured window.

    Deliberately tolerant: a failed read is skipped rather than raised. A sampler
    that dies mid-run would leave a partially-sampled gauge looking like a fully
    sampled one, which is the failure this whole class exists to prevent. Read
    failures are counted and reported instead.
    """

    def __init__(
        self,
        provider: MetricsProvider,
        gauges: dict[str, str],
        interval_s: float = 1.0,
    ) -> None:
        if not gauges:
            raise ValueError(
                "GaugeSampler needs a gauge map from the TargetProfile "
                "(profile.yaml `gauges:`). Sampling nothing would silently "
                "report every gauge as never-measured."
            )
        self._provider = provider
        self._gauges = dict(gauges)
        self._interval_s = max(0.05, float(interval_s))
        self._samples: dict[str, list[float]] = {name: [] for name in self._gauges}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.read_failures = 0

    def _read_once(self) -> None:
        for short_name, metric_name in self._gauges.items():
            try:
                raw = self._provider.fetch(metric_name)
            except Exception:
                self.read_failures += 1
                continue
            if not raw:
                continue
            value = raw.get("VALUE")
            if value is None:
                continue
            self._samples[short_name].append(float(value))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._read_once()
            self._stop.wait(self._interval_s)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sampler already started")
        self._thread = threading.Thread(target=self._loop, name="crucible-gauge-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, list[float]]:
        """Stop sampling and return the readings, dropping gauges that never read.

        A gauge with no samples is omitted rather than returned as ``[]``, so the
        collector's "never looked" branch is reached and the field becomes
        ``None``.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s * 4 + 5.0)
            self._thread = None
        return {name: values for name, values in self._samples.items() if values}

    def __enter__(self) -> GaugeSampler:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class LoadRunner(Protocol):
    """Locust, JMeter, k6 and Gatling all satisfy this."""

    def run(self, scenario: Scenario, run_id: str) -> LoadResult: ...


def _read_locust_aggregated(stats_csv: Path) -> dict[str, str]:
    """Pull the ``Aggregated`` row out of a locust ``_stats.csv``."""
    if not stats_csv.exists():
        raise LoadRunnerError(f"locust wrote no statistics at {stats_csv}")
    with stats_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == "Aggregated":
            return row
    raise LoadRunnerError(f"no Aggregated row in {stats_csv}")


def _as_float(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        text = (row.get(name) or "").strip()
        if text and text.upper() != "N/A":
            try:
                return float(text)
            except ValueError:
                continue
    return None


class LocustRunner:
    """Drive Locust headless, in two phases, sampling gauges through the second.

    ``metrics_provider`` is optional. Without one, the run still produces
    latency statistics but ``gauge_samples`` comes back empty -- and the collector
    will then report every gauge as ``None``, which is the honest answer.
    """

    def __init__(
        self,
        results_dir: str | Path = "results",
        metrics_provider: MetricsProvider | None = None,
        locust_binary: str = "locust",
        gauges: dict[str, str] | None = None,
        window_timers: tuple[str, ...] = (),
        profile: Any = None,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.metrics_provider = metrics_provider
        self.locust_binary = locust_binary
        # Taken from the profile unless passed explicitly. Never a module constant.
        self.gauges = dict(gauges) if gauges else (dict(profile.gauges) if profile else {})
        self.window_timers = tuple(window_timers) if window_timers else (
            tuple(profile.window_timers) if profile else ())
        if metrics_provider is not None and not self.gauges:
            raise ValueError(
                "LocustRunner has a metrics provider but no gauges to sample. Pass "
                "profile=<TargetProfile> or gauges=..., or the run would measure "
                "latency while reporting every gauge as never-measured."
            )

    # -- internals --------------------------------------------------------

    def _command(self, scenario: Scenario, run_time_s: float, csv_prefix: Path) -> list[str]:
        command = [
            self.locust_binary,
            "-f", str(scenario.locustfile),
            "--headless",
            "--host", scenario.host,
            "-u", str(int(scenario.users)),
            "-r", str(scenario.spawn_rate),
            "--run-time", f"{int(round(run_time_s))}s",
            "--csv", str(csv_prefix),
            "--only-summary",
        ]
        if scenario.tags:
            # Tags select which endpoint's tasks run. One cause family per tag, so
            # a scenario measures one signal rather than a blend of eight.
            command += ["--tags", *scenario.tags]
        return command

    def _launch(self, command: list[str], budget_s: float) -> subprocess.CompletedProcess[str]:
        if shutil.which(self.locust_binary) is None:
            raise LoadRunnerError(
                f"{self.locust_binary!r} is not on PATH. Install the load extra: "
                "uv sync --group load"
            )
        try:
            return subprocess.run(  # noqa: S603 - argv list, no shell, config-built
                command,
                capture_output=True,
                text=True,
                timeout=budget_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LoadRunnerError(
                f"load generator overran its wall-clock budget of {budget_s:.0f}s"
            ) from exc

    def _read_timers(self) -> dict[str, Any]:
        if self.metrics_provider is None:
            return {}
        readings: dict[str, Any] = {}
        for name in self.window_timers:
            try:
                raw = self.metrics_provider.fetch(name)
            except Exception:
                raw = None
            if raw:
                readings[name] = raw
        return readings

    # -- the run ----------------------------------------------------------

    def run(self, scenario: Scenario, run_id: str) -> LoadResult:
        """Warm up, discard, then measure with gauges sampled throughout."""
        self.results_dir.mkdir(parents=True, exist_ok=True)
        result = LoadResult(
            run_id=run_id,
            scenario=scenario.name,
            warmup_s=scenario.warmup_s,
            measure_s=scenario.measure_s,
            ramp_s=(scenario.users / scenario.spawn_rate) if scenario.spawn_rate else 0.0,
        )

        # Phase 1 - warmup. Statistics are written to a separate prefix and never
        # read, so JIT-era requests are absent from the percentiles rather than
        # merely diluted by them.
        if scenario.warmup_s > 0:
            warmup_prefix = self.results_dir / f"{run_id}-warmup"
            self._launch(
                self._command(scenario, scenario.warmup_s, warmup_prefix),
                budget_s=scenario.warmup_s + 120.0,
            )
        result.metrics_at_warmup_end = self._read_timers()

        # Phase 2 - the measured window, with the gauge sampler running.
        sampler: GaugeSampler | None = None
        if self.metrics_provider is not None:
            sampler = GaugeSampler(
                self.metrics_provider,
                self.gauges,
                interval_s=scenario.gauge_sample_interval_s,
            )
            sampler.start()

        measure_prefix = self.results_dir / run_id
        try:
            completed = self._launch(
                self._command(scenario, scenario.measure_s, measure_prefix),
                budget_s=scenario.measure_s + 120.0,
            )
        finally:
            if sampler is not None:
                result.gauge_samples = sampler.stop()

        # The gauges must be read before the pool drains, so timers are read only
        # after the sampler has stopped -- they are cumulative and do not drain.
        result.metrics_at_measure_end = self._read_timers()

        if completed.returncode != 0 and not scenario.expect_possible_failure:
            raise LoadRunnerError(
                f"locust exited {completed.returncode}: {completed.stderr.strip()[:500]}"
            )

        row = _read_locust_aggregated(Path(f"{measure_prefix}_stats.csv"))
        requests = int(_as_float(row, "Request Count") or 0)
        failures = int(_as_float(row, "Failure Count") or 0)
        result.request_count = requests
        result.failure_count = failures
        result.error_rate_pct = (100.0 * failures / requests) if requests else None
        result.p50_ms = _as_float(row, "50%", "Median Response Time")
        result.p95_ms = _as_float(row, "95%")
        result.p99_ms = _as_float(row, "99%")
        result.mean_ms = _as_float(row, "Average Response Time")
        result.max_ms = _as_float(row, "Max Response Time")
        result.rps = _as_float(row, "Requests/s")
        return result


def measurement_window(result: LoadResult) -> dict[str, Any]:
    """The measured-window delta for the request timer, if both readings exist."""
    from .collector import compute_window

    start = result.metrics_at_warmup_end.get("http.server.requests")
    end = result.metrics_at_measure_end.get("http.server.requests")
    if not start or not end:
        return {
            "warmup_s": result.warmup_s,
            "measure_s": result.measure_s,
            "note": "no metrics provider; window is the load generator's own statistics only",
        }
    window = compute_window(start, end)
    window["warmup_s"] = result.warmup_s
    window["measure_s"] = result.measure_s
    return window


def elapsed_s(started_at: float) -> float:
    """Wall clock since ``started_at``. Wall clock is the real budget, not money."""
    return time.time() - started_at
