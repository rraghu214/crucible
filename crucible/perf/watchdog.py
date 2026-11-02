"""The watchdog: seven tripwires, arithmetic, every five minutes.

A long scenario runs unattended for hours. Something has to notice when the thing
being measured stops being the thing that was asked about -- the service falling
over rather than merely being slow, the load generator dying, a neighbouring
tenant taking the CPU -- and stop, because every further minute of that run
produces numbers that look ordinary and mean nothing.

**It is arithmetic, not a model call** (``DESIGN.md`` section 6). A 24-hour
scenario is 288 checks; at $0.002 each that is $0.58 to answer questions
subtraction already answers, against a whole-campaign budget of about the same.
Section 6 does leave one model call in: "a model call fires only when a tripwire
trips *ambiguously* -- roughly twice per long scenario". That seam is
:attr:`Watchdog.adjudicator`, and it is ``None`` by default. With no adjudicator
an ambiguous reading is recorded as :data:`WARN` and never aborts anything, so a
default watchdog calls nothing at all.

**The four kinds of answer a tripwire can give, and why there are four.**

:data:`OK` and :data:`TRIPPED` are obvious. :data:`UNKNOWN` exists because
principle 2 says the agent knows what it cannot see: a ``cpu_steal_pct`` of
``None`` means nothing measured steal, which is not the same claim as "steal was
fine". That is section 4.2's null-is-not-zero rule applied to the watchdog rather
than to the collector, and it matters most for exactly the tripwire least likely
to be readable -- host contention, on a box whose ``/proc/stat`` we may not be
able to read at all. :data:`SUSPENDED` is the ceiling probe below: a tripwire
deliberately switched off must not be recorded as one that passed.

**On a ceiling probe the subject tripwires are suspended, not the instrument
ones.** A scenario declaring ``push_beyond: true`` was sent to find where things
break, so aborting on a high error rate discards the answer it went to get
(section 6). What stays armed is everything that says the *measurement* is
invalid rather than that the service is unhealthy: reachability, the load
generator's own health, and host contention. The split is worth stating because
it is not quite "the error tripwires": ``throughput_collapse`` is suspended too,
since a collapse under deliberate overload is the finding, while
``load_generator_alive`` stays armed, since a dead load generator reports the
same zero throughput and would read as a service that comfortably survived the
step that just killed it.

Note the asymmetry section 20.5 draws: this is the ``push_beyond`` probe, which
suspends. Ceiling *discovery* does the opposite and stops at the first SLA miss,
because there the failure is the answer. Both are legitimate; a campaign has to
say which question it is asking, and neither is inferred here.

**The latency ceiling is an absolute wall, never derived from the SLA.** Missing
the SLA is the thing the campaign exists to measure; a latency tripwire set from
the SLA would abort every run that is doing its job. The ceiling is set where
requests have stopped being requests -- tens of seconds -- and it is
configuration, like every other threshold in this module.

**Thresholds are per environment, not constants.** Host contention especially:
5% by default, 10% on the Oracle free tier, because steal there is a steady 4-6%
under load and a fixed 5% would have aborted a K1 run that passed every stability
criterion it was judged against (section 6, ``docs/K1_CLOUD_RESULT.md`` 4.4).
Observed steal is recorded on every run whether or not it tripped, together with
the threshold it was judged against: 4% under a 5% limit and 4% under a 10% limit
are different levels of confidence in the same number.
"""

from __future__ import annotations

import csv
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .campaign import ABORTED, ExperimentManifest, request_abort

#: The seven, in the order the watchdog screen lists them
#: (``docs/crucible-screens-v2.html``). The order is part of the contract: an
#: operator comparing two runs side by side should not have to re-find the row.
TRIPWIRES = (
    "target_reachable",
    "error_rate",
    "error_rate_trend",
    "throughput_collapse",
    "latency_ceiling",
    "load_generator_alive",
    "host_contention",
)

#: Statuses. Strings rather than an enum so a manifest read years later needs no
#: import to be legible -- the same choice the campaign's verdicts make.
OK = "ok"
WARN = "warn"
TRIPPED = "tripped"
SUSPENDED = "suspended"
UNKNOWN = "unknown"

#: Suspended on a ceiling probe. Everything NOT in here is an instrument
#: tripwire: it says the measurement is invalid rather than that the service is
#: unhealthy, and no declaration of intent makes an invalid measurement valid.
SUSPENDED_ON_CEILING_PROBE = (
    "error_rate",
    "error_rate_trend",
    "throughput_collapse",
    "latency_ceiling",
)

#: Every five minutes (section 6). Not every thirty seconds: the arithmetic is
#: free but the readings are not -- each check hits the target and the load
#: generator, on boxes whose CPU is the thing being measured.
DEFAULT_CHECK_INTERVAL_S = 300.0

#: A reading inside this fraction of its limit is ambiguous rather than clean.
#: It never aborts on its own; it is what the adjudicator seam is offered.
DEFAULT_WARN_FRACTION = 0.8


class WatchdogError(RuntimeError):
    """The watchdog could not be configured. Never raised for a tripped wire."""


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogThresholds:
    """What each tripwire is judged against, and where the number came from.

    Every field is configuration. The defaults are the watchdog screen's
    published readings, which makes them a documented starting point rather than
    a measured truth -- and :meth:`from_sla` is how the two that ARE measured on
    a specific box (the error budget, host contention) come from the SLA instead.
    """

    #: Consecutive failed probes before the target is declared unreachable. Not
    #: one: a single dropped connection on a shared box is not an outage, and a
    #: watchdog that ended a six-hour run on one refused handshake would be worse
    #: than no watchdog.
    unreachable_checks: int = 3
    #: Percent. The scenario's error budget; defaults to the SLA's.
    error_rate_pct: float = 1.0
    #: Percentage points per check. The screen's example trips at +0.19/check
    #: against this.
    error_rate_trend_pct_per_check: float = 0.15
    #: How many checks the trend is fitted over. Two points are a difference, not
    #: a trend, and one noisy check would swing it.
    error_rate_trend_window: int = 4
    #: Percent below the reference throughput. The screen's 594 against 900 tpm
    #: is a 34% fall.
    throughput_collapse_pct: float = 25.0
    #: Absolute milliseconds. NOT derived from the SLA -- see the module
    #: docstring. This is "requests have stopped being requests", never "the SLA
    #: was missed".
    latency_ceiling_ms: float = 70_000.0
    #: Seconds since the load generator last reported. Locust writes its history
    #: CSV once a second, so a minute of silence is not a slow write.
    load_generator_beat_max_age_s: float = 60.0
    #: Percent CPU steal. Per environment, never a constant (section 6): 5% by
    #: default, 10% on Oracle free tier where steal runs 4-6% under load.
    cpu_steal_abort_pct: float = 5.0
    #: How close to a limit counts as ambiguous rather than clean.
    warn_fraction: float = DEFAULT_WARN_FRACTION
    #: Where the numbers above came from, recorded with the run. A threshold
    #: whose provenance is lost cannot be argued with later.
    source: str = "watchdog defaults"

    @classmethod
    def from_sla(cls, sla: Any, **overrides: Any) -> WatchdogThresholds:
        """Take the two measured thresholds from the SLA, keep the rest as defaults.

        The SLA owns the error budget and this environment's steal ceiling, and it
        is Policy memory the agent cannot write (section 4.4). Reading it here is
        what stops the watchdog carrying a second, independently-drifting copy of a
        number the campaign is already judged against.
        """
        return cls(
            error_rate_pct=float(getattr(sla, "error_rate_pct", 1.0)),
            cpu_steal_abort_pct=float(getattr(sla, "cpu_steal_abort_pct", 5.0)),
            source=f"config/slo.yaml ({getattr(sla, 'name', 'unnamed SLA')})",
            **overrides,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# One poll
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogReading:
    """What one check managed to observe. Every field may be ``None``.

    ``None`` is load-bearing everywhere in here. A reading nobody took produces
    :data:`UNKNOWN`, never :data:`OK`, for the same reason an unsampled gauge is
    ``null`` and never ``0`` (section 4.2): a clean pass and an untested pass must
    not look identical in the record a human reads at 3am.
    """

    elapsed_s: float = 0.0
    #: ``None`` when nothing probed the target; ``False`` when a probe failed.
    reachable: bool | None = None
    http_status: int | None = None
    error_rate_pct: float | None = None
    throughput_rps: float | None = None
    #: The load generator's current p99, in milliseconds.
    p99_ms: float | None = None
    load_generator_users: int | None = None
    load_generator_beat_age_s: float | None = None
    cpu_steal_pct: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Tripwire:
    """One tripwire's answer at one check."""

    name: str
    status: str
    #: What the screen prints in its "reading" column -- the observed value and
    #: its limit, together. A status without its number cannot be argued with.
    reading: str = ""
    detail: str = ""

    @property
    def tripped(self) -> bool:
        return self.status == TRIPPED

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The tripwires. Pure arithmetic: no I/O, no clock, no model.
# ---------------------------------------------------------------------------


def _band(value: float, limit: float, warn_fraction: float) -> str:
    """:data:`TRIPPED` above the limit, :data:`WARN` inside the band below it, else OK."""
    if value > limit:
        return TRIPPED
    if limit > 0 and value >= limit * warn_fraction:
        return WARN
    return OK


def check_target_reachable(
    consecutive_failures: int | None, thresholds: WatchdogThresholds
) -> Tripwire:
    """Is the service answering at all.

    Armed on a ceiling probe as well. That looks strict -- a probe sent to break
    things may well break them -- but by the time the target stops answering, the
    error and throughput tripwires have already recorded where the knee was, so
    stopping then discards nothing and leaves the box recoverable.
    """
    if consecutive_failures is None:
        return Tripwire(
            "target_reachable",
            UNKNOWN,
            "not probed",
            "nothing probed the target this check; unknown is not the same as reachable",
        )
    limit = thresholds.unreachable_checks
    status = TRIPPED if consecutive_failures >= limit else (WARN if consecutive_failures else OK)
    return Tripwire(
        "target_reachable",
        status,
        f"{consecutive_failures} consecutive failures | aborts at {limit}",
        "the target stopped answering" if status == TRIPPED else "",
    )


def check_error_rate(error_rate_pct: float | None, thresholds: WatchdogThresholds) -> Tripwire:
    """Errors against the scenario's budget."""
    if error_rate_pct is None:
        return Tripwire("error_rate", UNKNOWN, "not measured", "no error rate was read")
    status = _band(error_rate_pct, thresholds.error_rate_pct, thresholds.warn_fraction)
    return Tripwire(
        "error_rate",
        status,
        f"{error_rate_pct:.2f}% | max {thresholds.error_rate_pct:.2f}%",
        "errors are over the scenario's budget" if status == TRIPPED else "",
    )


def error_trend_pct_per_check(values: list[float] | tuple[float, ...]) -> float | None:
    """Least-squares slope of the error rate, in percentage points per check.

    Least squares rather than last-minus-first because one spiky check should not
    decide whether a six-hour run continues; the fit uses every point in the
    window. ``None`` below three points -- two points are a difference, and
    calling a difference a trend is how a watchdog aborts on noise.
    """
    ys = [float(v) for v in values if v is not None]
    if len(ys) < 3:
        return None
    n = len(ys)
    mean_x = (n - 1) / 2.0
    mean_y = sum(ys) / n
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if not denominator:
        return None
    numerator = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    return numerator / denominator


def check_error_rate_trend(
    recent_error_rates: list[float] | tuple[float, ...], thresholds: WatchdogThresholds
) -> Tripwire:
    """Are errors rising, even while still inside the budget.

    Its own tripwire because a rate under the limit and climbing steadily is a run
    that will be over the limit later, and noticing at check 23 is worth more than
    noticing at check 40 -- the checks in between are spent wall clock, which
    section 7 says is the real budget.
    """
    slope = error_trend_pct_per_check(recent_error_rates)
    if slope is None:
        return Tripwire(
            "error_rate_trend",
            UNKNOWN,
            f"{len(recent_error_rates)} of 3 checks needed",
            "a trend needs at least three checks; two points are a difference",
        )
    limit = thresholds.error_rate_trend_pct_per_check
    status = (
        TRIPPED if slope > limit else (WARN if slope >= limit * thresholds.warn_fraction else OK)
    )
    return Tripwire(
        "error_rate_trend",
        status,
        f"{slope:+.2f}%/check | trips above {limit:+.2f}%/check",
        "errors are climbing" if status == TRIPPED else "",
    )


def check_throughput_collapse(
    throughput_rps: float | None,
    reference_rps: float | None,
    thresholds: WatchdogThresholds,
) -> Tripwire:
    """Has throughput fallen away from what this scenario was achieving.

    Its own tripwire, and that is section 6's point: errors alone are ambiguous,
    but errors *and* a 34% throughput fall together mean the service is falling
    over rather than merely being slow -- and a service falling over is no longer
    a latency measurement. Each fires independently; when several fire at once the
    abort reason says so, because the conjunction is what tells the two apart.
    """
    if throughput_rps is None:
        return Tripwire(
            "throughput_collapse", UNKNOWN, "not measured", "no throughput was read"
        )
    if not reference_rps:
        return Tripwire(
            "throughput_collapse",
            UNKNOWN,
            f"{throughput_rps:.1f} rps | no reference yet",
            "nothing to compare against until a reference throughput is established",
        )
    fall_pct = 100.0 * (reference_rps - throughput_rps) / reference_rps
    status = _band(fall_pct, thresholds.throughput_collapse_pct, thresholds.warn_fraction)
    return Tripwire(
        "throughput_collapse",
        status,
        f"{throughput_rps:.1f} rps vs {reference_rps:.1f} reference ({-fall_pct:+.1f}%) "
        f"| aborts below -{thresholds.throughput_collapse_pct:.0f}%",
        "throughput has collapsed" if status == TRIPPED else "",
    )


def check_latency_ceiling(p99_ms: float | None, thresholds: WatchdogThresholds) -> Tripwire:
    """Have requests stopped being requests.

    Deliberately far above any SLA. Missing the SLA is what the campaign is for;
    aborting on that would abort every run that is doing its job.
    """
    if p99_ms is None:
        return Tripwire("latency_ceiling", UNKNOWN, "not measured", "no latency was read")
    status = _band(p99_ms, thresholds.latency_ceiling_ms, thresholds.warn_fraction)
    return Tripwire(
        "latency_ceiling",
        status,
        f"{p99_ms / 1000.0:.1f} s | aborts above {thresholds.latency_ceiling_ms / 1000.0:.0f} s",
        "requests are hanging rather than completing slowly" if status == TRIPPED else "",
    )


def check_load_generator_alive(
    users: int | None, beat_age_s: float | None, thresholds: WatchdogThresholds
) -> Tripwire:
    """Is the instrument still running.

    Armed on a ceiling probe too, which is the distinction the module docstring
    draws: a dead load generator reports zero throughput and zero errors, and on a
    ceiling probe that would read as a service comfortably surviving the step it
    was just killed by.
    """
    if beat_age_s is None and users is None:
        return Tripwire(
            "load_generator_alive",
            UNKNOWN,
            "no heartbeat read",
            "nothing observed the load generator this check",
        )
    if users is not None and users <= 0:
        return Tripwire(
            "load_generator_alive",
            TRIPPED,
            "0 users",
            "the load generator has no users running; nothing is being measured",
        )
    if beat_age_s is None:
        return Tripwire(
            "load_generator_alive", OK, f"{users} users | no heartbeat age available", ""
        )
    limit = thresholds.load_generator_beat_max_age_s
    status = _band(beat_age_s, limit, thresholds.warn_fraction)
    return Tripwire(
        "load_generator_alive",
        status,
        f"{users if users is not None else '?'} users | last beat {beat_age_s:.0f} s ago "
        f"| aborts above {limit:.0f} s",
        "the load generator has stopped reporting" if status == TRIPPED else "",
    )


def check_host_contention(
    cpu_steal_pct_value: float | None, thresholds: WatchdogThresholds
) -> Tripwire:
    """Is a neighbouring tenant taking the CPU this measurement is about.

    Aborts even when the application looks perfectly healthy. Better to declare
    the measurement invalid than to report a p99 that was really about somebody
    else's workload -- a direct consequence of running on free-tier shared vCPU
    (section 6).
    """
    if cpu_steal_pct_value is None:
        return Tripwire(
            "host_contention",
            UNKNOWN,
            f"not measured | would abort above {thresholds.cpu_steal_abort_pct:.1f}%",
            "nothing read CPU steal; a run where nobody looked is not a run that was clean",
        )
    status = _band(cpu_steal_pct_value, thresholds.cpu_steal_abort_pct, thresholds.warn_fraction)
    return Tripwire(
        "host_contention",
        status,
        f"cpu steal {cpu_steal_pct_value:.1f}% | trips above {thresholds.cpu_steal_abort_pct:.1f}%",
        "a co-tenant is affecting these numbers" if status == TRIPPED else "",
    )


# ---------------------------------------------------------------------------
# One check, and the run
# ---------------------------------------------------------------------------


@dataclass
class WatchdogCheck:
    """All seven tripwires at one moment, plus what was done about them."""

    number: int
    elapsed_s: float
    reading: WatchdogReading
    tripwires: list[Tripwire] = field(default_factory=list)
    #: Tripwires the adjudicator was asked about, and what it said. Empty on
    #: every check of a default watchdog, which has no adjudicator.
    adjudications: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tripped(self) -> list[str]:
        return [t.name for t in self.tripwires if t.status == TRIPPED]

    @property
    def warned(self) -> list[str]:
        return [t.name for t in self.tripwires if t.status == WARN]

    @property
    def aborting(self) -> bool:
        return bool(self.tripped)

    @property
    def healthy(self) -> bool:
        """No tripwire tripped or warned. :data:`UNKNOWN` does not disqualify a check.

        Deliberate: on a box where steal cannot be read, every check would
        otherwise be unhealthy and "the last healthy reading" -- the thing an
        operator is handed after an abort -- would never exist. What was unknown
        is carried on the check itself, so a reader can still see it.
        """
        return not self.tripped and not self.warned

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "elapsed_s": self.elapsed_s,
            "reading": self.reading.as_dict(),
            "tripwires": [t.as_dict() for t in self.tripwires],
            "adjudications": self.adjudications,
            "tripped": self.tripped,
            "warned": self.warned,
        }


@dataclass
class AbortRecord:
    """Why the run stopped, and what state it stopped in."""

    run_id: str
    tripwires: list[str] = field(default_factory=list)
    check_number: int = 0
    elapsed_s: float = 0.0
    reason: str = ""
    #: The screen's "you are told, with the last healthy reading attached". An
    #: abort reason without the last good reading tells an operator that
    #: something broke but not what the run looked like before it did.
    last_healthy_check: dict[str, Any] | None = None
    #: What the abort actually did, in order. Recorded rather than assumed: an
    #: abort that failed to stop the load generator is a different situation from
    #: one that succeeded, and the manifest has to be able to say which.
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WatchdogRun:
    """Every check taken during one scenario, and how it ended."""

    run_id: str
    scenario: str = ""
    thresholds: dict[str, Any] = field(default_factory=dict)
    ceiling_probe: bool = False
    checks: list[WatchdogCheck] = field(default_factory=list)
    abort: AbortRecord | None = None

    @property
    def aborted(self) -> bool:
        return self.abort is not None

    @property
    def observed_cpu_steal_pct(self) -> float | None:
        """Peak steal over the run, or ``None`` if nothing ever read it."""
        values = [
            c.reading.cpu_steal_pct for c in self.checks if c.reading.cpu_steal_pct is not None
        ]
        return max(values) if values else None

    def last_healthy(self) -> WatchdogCheck | None:
        for check in reversed(self.checks):
            if check.healthy:
                return check
        return None

    def as_dict(self) -> dict[str, Any]:
        """The shape that travels on the manifest.

        ``observed_cpu_steal_pct`` and its threshold are both here whether or not
        contention tripped. Section 6: a run that stayed under the threshold is
        not the same claim as a run where nobody looked, and 4% under a 5% limit
        is not the same confidence as 4% under a 10% one.
        """
        steal = self.observed_cpu_steal_pct
        limit = self.thresholds.get("cpu_steal_abort_pct")
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "thresholds": self.thresholds,
            "ceiling_probe": self.ceiling_probe,
            "suspended_tripwires": list(SUSPENDED_ON_CEILING_PROBE) if self.ceiling_probe else [],
            "checks": [c.as_dict() for c in self.checks],
            "check_count": len(self.checks),
            "aborted": self.aborted,
            "abort": self.abort.as_dict() if self.abort else None,
            "observed_cpu_steal_pct": steal,
            "cpu_steal_abort_pct": limit,
            "cpu_steal_note": (
                "CPU steal was never read during this run; it is unknown, not clean"
                if steal is None
                else f"peak CPU steal {steal:.1f}% against a {limit}% threshold"
            ),
        }


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------


class ReadingSource(Protocol):
    """Whatever can say what is happening right now. Injected, always."""

    def __call__(self, elapsed_s: float) -> WatchdogReading: ...


@dataclass
class Watchdog:
    """Evaluate the seven tripwires on a schedule, and abort when one trips.

    Collaborators are injected for the same reason the campaign's are: the real
    ones talk to a load generator and a cloud box for six hours, and every rule in
    here has to be assertable on numbers in milliseconds.
    """

    run_id: str
    thresholds: WatchdogThresholds = field(default_factory=WatchdogThresholds)
    scenario_name: str = ""
    #: Set from ``Scenario.push_beyond``. See :func:`for_scenario` -- it is never
    #: inferred from ``expect_possible_failure``, which only says a non-zero exit
    #: from the load generator is tolerable.
    ceiling_probe: bool = False
    interval_s: float = DEFAULT_CHECK_INTERVAL_S
    #: Throughput this scenario was achieving before anything went wrong --
    #: normally the campaign's measured baseline rps. Without it, the first
    #: :attr:`reference_checks` checks establish one and the tripwire reports
    #: :data:`UNKNOWN` until then rather than guessing.
    reference_rps: float | None = None
    reference_checks: int = 3
    #: Section 6's "a model call fires only when a tripwire trips ambiguously".
    #: ``None`` by default, so a default watchdog calls nothing. Signature:
    #: ``(check, tripwire) -> (abort, reason)``.
    adjudicator: Callable[[WatchdogCheck, Tripwire], tuple[bool, str]] | None = None
    #: "Roughly twice per long scenario" is the budget section 6 assumes. A
    #: flapping signal would otherwise turn the cheap watchdog into the expensive
    #: one it exists to avoid.
    max_adjudications: int = 4
    #: Where the abort marker is written, so the campaign's own boundary check
    #: sees it. ``None`` skips that step.
    state_dir: Path | None = None

    _checks: list[WatchdogCheck] = field(default_factory=list, init=False)
    _consecutive_unreachable: int = field(default=0, init=False)
    _adjudications: int = field(default=0, init=False)

    # -- evaluation -------------------------------------------------------

    def _reference(self) -> float | None:
        """The throughput a collapse is judged against.

        An explicit reference wins. Otherwise the median of the first few checks:
        the median rather than the first value, because one cold check would
        otherwise set the bar for the whole run -- a bar set too low never trips,
        and one set too high trips immediately.
        """
        if self.reference_rps:
            return self.reference_rps
        seen = [
            c.reading.throughput_rps
            for c in self._checks[: self.reference_checks]
            if c.reading.throughput_rps is not None
        ]
        if len(seen) < self.reference_checks:
            return None
        ordered = sorted(seen)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    def _recent_error_rates(self) -> list[float]:
        window = self.thresholds.error_rate_trend_window
        return [
            c.reading.error_rate_pct
            for c in self._checks[-window:]
            if c.reading.error_rate_pct is not None
        ]

    def check(self, reading: WatchdogReading) -> WatchdogCheck:
        """Evaluate all seven against one reading, and record it.

        Suspension is applied here rather than by skipping the evaluation, so a
        suspended tripwire still carries its observed reading. That is the whole
        point of a ceiling probe: the numbers it was sent to find are exactly the
        ones being suspended, and recording them is the deliverable.
        """
        if reading.reachable is None:
            failures: int | None = None
        elif reading.reachable:
            self._consecutive_unreachable = 0
            failures = 0
        else:
            self._consecutive_unreachable += 1
            failures = self._consecutive_unreachable

        check = WatchdogCheck(
            number=len(self._checks) + 1,
            elapsed_s=reading.elapsed_s,
            reading=reading,
        )
        # Appended before the trend and the reference are computed, so this
        # check's own reading counts toward both -- a trend that excluded the
        # newest point would be a trend about the past.
        self._checks.append(check)

        raw = [
            check_target_reachable(failures, self.thresholds),
            check_error_rate(reading.error_rate_pct, self.thresholds),
            check_error_rate_trend(self._recent_error_rates(), self.thresholds),
            check_throughput_collapse(reading.throughput_rps, self._reference(), self.thresholds),
            check_latency_ceiling(reading.p99_ms, self.thresholds),
            check_load_generator_alive(
                reading.load_generator_users, reading.load_generator_beat_age_s, self.thresholds
            ),
            check_host_contention(reading.cpu_steal_pct, self.thresholds),
        ]
        check.tripwires = [self._apply_suspension(t) for t in raw]
        self._adjudicate(check)
        return check

    def _apply_suspension(self, tripwire: Tripwire) -> Tripwire:
        if not self.ceiling_probe or tripwire.name not in SUSPENDED_ON_CEILING_PROBE:
            return tripwire
        return Tripwire(
            name=tripwire.name,
            status=SUSPENDED,
            reading=tripwire.reading,
            detail=(
                "suspended: this scenario declares push_beyond, so where it breaks is the "
                "answer it was sent to find, not a reason to stop "
                f"(would otherwise be {tripwire.status})"
            ),
        )

    def _previous_status(self, name: str) -> str | None:
        if len(self._checks) < 2:
            return None
        for tripwire in self._checks[-2].tripwires:
            if tripwire.name == name:
                return tripwire.status
        return None

    def _adjudicate(self, check: WatchdogCheck) -> None:
        """Offer newly-ambiguous tripwires to the adjudicator, if there is one.

        Only on the transition INTO :data:`WARN`, and only a handful of times. A
        model call on every ambiguous check of a 288-check scenario is the cost
        section 6 exists to avoid; a call on the transition is the "roughly twice
        per long scenario" it budgets for.
        """
        if self.adjudicator is None:
            return
        for tripwire in check.tripwires:
            if tripwire.status != WARN:
                continue
            if self._previous_status(tripwire.name) == WARN:
                continue
            if self._adjudications >= self.max_adjudications:
                check.adjudications.append(
                    {
                        "tripwire": tripwire.name,
                        "called": False,
                        "reason": (
                            f"adjudication budget of {self.max_adjudications} calls is spent; "
                            "the watchdog stays arithmetic for the rest of this run"
                        ),
                    }
                )
                continue
            self._adjudications += 1
            abort, reason = self.adjudicator(check, tripwire)
            check.adjudications.append(
                {
                    "tripwire": tripwire.name,
                    "called": True,
                    "abort": bool(abort),
                    "reason": reason,
                }
            )
            if abort:
                check.tripwires = [
                    Tripwire(t.name, TRIPPED, t.reading, f"adjudicated: {reason}")
                    if t.name == tripwire.name
                    else t
                    for t in check.tripwires
                ]

    # -- the run ----------------------------------------------------------

    def supervise(
        self,
        read: ReadingSource,
        duration_s: float,
        *,
        stop_load: Callable[[], None] | None = None,
        revert: Callable[[], str] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> WatchdogRun:
        """Poll every :attr:`interval_s` until the scenario ends or a wire trips.

        Returns the whole run either way. A run that finished cleanly is still
        evidence -- the observed steal margin on it is what a later reader needs
        in order to know how much to trust the p99 it accompanies.
        """
        run = WatchdogRun(
            run_id=self.run_id,
            scenario=self.scenario_name,
            thresholds=self.thresholds.as_dict(),
            ceiling_probe=self.ceiling_probe,
            checks=self._checks,
        )
        started = now()
        while True:
            remaining = duration_s - (now() - started)
            if remaining <= 0:
                return run
            sleep(min(self.interval_s, remaining))
            check = self.check(read(now() - started))
            if check.aborting:
                run.abort = self.abort(check, run, stop_load=stop_load, revert=revert)
                return run

    def abort(
        self,
        check: WatchdogCheck,
        run: WatchdogRun,
        *,
        stop_load: Callable[[], None] | None = None,
        revert: Callable[[], str] | None = None,
    ) -> AbortRecord:
        """Stop the load, undo the uncommitted change, and tell the campaign.

        The four actions the watchdog screen lists, in that order and for a
        reason: the load generator is stopped first because every second it keeps
        running is load against a target nobody is measuring any more, and the
        abort marker is written last because it is what lets the campaign's own
        boundary check take over -- including section 19.9's redeploy of the last
        good commit, which belongs to the campaign and is deliberately not
        duplicated here.

        Every step is recorded, including its failure. An abort that could not
        stop the load generator leaves the box under load, and a manifest that
        implied otherwise would be worse than one that said nothing.
        """
        names = check.tripped
        detail = "; ".join(f"{t.name}: {t.reading}" for t in check.tripwires if t.tripped)
        reason = (
            f"watchdog aborted at check {check.number} "
            f"({check.elapsed_s / 60.0:.0f} min): {', '.join(names)}. {detail}"
        )
        healthy = run.last_healthy()
        record = AbortRecord(
            run_id=self.run_id,
            tripwires=names,
            check_number=check.number,
            elapsed_s=check.elapsed_s,
            reason=reason,
            last_healthy_check=healthy.as_dict() if healthy else None,
        )

        if stop_load is not None:
            try:
                stop_load()
                record.actions.append("load generator stopped")
            except Exception as exc:  # noqa: BLE001 - recorded, never masked
                record.actions.append(f"FAILED to stop the load generator: {exc}")
        else:
            record.actions.append("no load generator to stop")

        if revert is not None:
            try:
                record.actions.append(f"workspace reverted: {revert()}")
            except Exception as exc:  # noqa: BLE001 - recorded, never masked
                record.actions.append(f"FAILED to revert the workspace: {exc}")
        else:
            record.actions.append("no workspace revert configured")

        if self.state_dir is not None:
            request_abort(self.state_dir, self.run_id, reason)
            record.actions.append(
                "abort requested of the campaign; it stops at the next experiment boundary "
                "and redeploys the last good commit (DESIGN.md 19.9)"
            )
        return record


def for_scenario(
    scenario: Any,
    sla: Any,
    run_id: str,
    *,
    reference_rps: float | None = None,
    state_dir: Path | None = None,
    **overrides: Any,
) -> Watchdog:
    """Build a watchdog for one scenario, taking its thresholds from the SLA.

    ``push_beyond`` alone declares a ceiling probe. ``expect_possible_failure``
    does not, although section 6 names the two in the same breath: that flag only
    says a non-zero exit from the load generator is tolerable, and letting it
    suspend four tripwires would mean a scenario switched off half the watchdog by
    way of an error-handling convenience. Section 20.1 is explicit that the intent
    is declared, never inferred.
    """
    return Watchdog(
        run_id=run_id,
        thresholds=WatchdogThresholds.from_sla(sla, **overrides),
        scenario_name=getattr(scenario, "name", ""),
        ceiling_probe=bool(getattr(scenario, "push_beyond", False)),
        reference_rps=reference_rps,
        state_dir=state_dir,
    )


def abort_manifest(
    record: AbortRecord,
    run: WatchdogRun,
    *,
    experiment: int,
    before: dict[str, Any] | None = None,
) -> ExperimentManifest:
    """The manifest an aborted scenario leaves behind.

    Verdict :data:`~crucible.perf.campaign.ABORTED`, with the tripwire that fired
    named in the reason -- the watchdog screen's third on-abort line. The manifest
    shape is the campaign's, imported rather than re-declared: a second definition
    of the thing the scorer reads is how two readers of "the same" manifest end up
    disagreeing about what a field means.
    """
    manifest = ExperimentManifest(
        run_id=record.run_id,
        experiment=experiment,
        started_at_epoch_s=time.time(),
        verdict=ABORTED,
        verdict_reason=record.reason,
        before=before or {},
        watchdog=run.as_dict(),
    )
    manifest.notes.extend(record.actions)
    manifest.notes.append(
        "aborted by the watchdog; nothing was measured after the tripwire fired, and no "
        "verdict about a change may be read from this experiment"
    )
    return manifest


# ---------------------------------------------------------------------------
# Readings from the real world. Thin on purpose: the arithmetic above is where
# the rules live, and it stays testable without any of this.
# ---------------------------------------------------------------------------


def probe_http(url: str, timeout_s: float = 5.0) -> tuple[bool | None, int | None]:
    """``(reachable, status)``. ``(None, None)`` when there is no URL to probe.

    A non-2xx answer still counts as reachable: the service is up and saying no,
    which is the error-rate tripwire's business. This tripwire is only about
    whether anything is listening at all.
    """
    if not url:
        return None, None
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - config URL
            return True, int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as http_error:
        return True, int(http_error.code)
    except (urllib.error.URLError, OSError, ValueError):
        return False, None


def cpu_steal_pct(previous: dict[str, float], current: dict[str, float]) -> float | None:
    """Steal as a percentage of the CPU time elapsed between two ``/proc/stat`` reads.

    A delta, never the cumulative figure: ``/proc/stat`` counts since boot, so a
    box up for three weeks would report the average steal of three weeks rather
    than of this scenario. Pure arithmetic, so it is testable on a machine with no
    ``/proc`` at all -- which is every Windows dev box in this project.
    """
    if not previous or not current:
        return None
    total = sum(current.values()) - sum(previous.values())
    if total <= 0:
        return None
    steal = float(current.get("steal", 0.0)) - float(previous.get("steal", 0.0))
    return 100.0 * steal / total


def read_proc_stat(path: str | Path = "/proc/stat") -> dict[str, float] | None:
    """The aggregate ``cpu`` line as named fields, or ``None`` where there is none.

    ``None`` off Linux rather than a zero. Host contention is then reported as
    :data:`UNKNOWN`, which is the honest answer: nobody looked.
    """
    fields = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            values = [float(v) for v in line.split()[1:]]
            return {name: values[i] for i, name in enumerate(fields) if i < len(values)}
    return None


def read_locust_history(stats_history_csv: str | Path) -> dict[str, Any] | None:
    """The last aggregate row of Locust's ``_stats_history.csv``, written once a second.

    Locust writes this file throughout a headless run whenever ``--csv`` is given,
    so it is the live view of a run whose summary does not exist yet. ``None`` when
    the file is missing or holds no aggregate rows -- the load-generator tripwire
    then has no heartbeat, and says so rather than assuming silence means idle.
    """
    path = Path(stats_history_csv)
    if not path.exists():
        return None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle) if r.get("Name") in ("Aggregated", "")]
    except OSError:
        return None
    if not rows:
        return None
    row = rows[-1]

    def number(*names: str) -> float | None:
        for name in names:
            text = (row.get(name) or "").strip()
            if text and text.upper() != "N/A":
                try:
                    return float(text)
                except ValueError:
                    continue
        return None

    requests = number("Total Request Count") or 0.0
    failures = number("Total Failure Count") or 0.0
    return {
        "timestamp": number("Timestamp"),
        "users": int(number("User Count") or 0),
        "throughput_rps": number("Requests/s"),
        "p99_ms": number("99%", "99%ile"),
        "error_rate_pct": (100.0 * failures / requests) if requests else None,
    }


@dataclass
class LiveReadings:
    """Assemble one :class:`WatchdogReading` from the box and the load generator.

    Deliberately the only class in this module that touches the outside world.
    Everything that decides anything is arithmetic above it, so a rule can be
    argued with in a test rather than reproduced on a cloud box at 3am.
    """

    health_url: str = ""
    stats_history_csv: str | Path = ""
    proc_stat_path: str | Path = "/proc/stat"
    _previous_stat: dict[str, float] | None = field(default=None, init=False)

    def __call__(self, elapsed_s: float) -> WatchdogReading:
        reachable, status = probe_http(self.health_url)
        history = read_locust_history(self.stats_history_csv) if self.stats_history_csv else None
        current = read_proc_stat(self.proc_stat_path)
        steal = cpu_steal_pct(self._previous_stat or {}, current or {})
        self._previous_stat = current
        beat_age = None
        if history and history.get("timestamp"):
            beat_age = max(0.0, time.time() - float(history["timestamp"]))
        return WatchdogReading(
            elapsed_s=elapsed_s,
            reachable=reachable,
            http_status=status,
            error_rate_pct=(history or {}).get("error_rate_pct"),
            throughput_rps=(history or {}).get("throughput_rps"),
            p99_ms=(history or {}).get("p99_ms"),
            load_generator_users=(history or {}).get("users"),
            load_generator_beat_age_s=beat_age,
            cpu_steal_pct=steal,
            note="" if current else "no /proc/stat on this host; CPU steal is unknown",
        )


def stop_locust(process: Any) -> None:
    """Stop a running load generator, politely and then not.

    ``terminate`` first: Locust writes its statistics on shutdown, and killing it
    outright would throw away the evidence of the run that just went wrong --
    which is the evidence the abort exists to preserve.
    """
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
    except (OSError, AttributeError):
        return


__all__ = [
    "DEFAULT_CHECK_INTERVAL_S",
    "OK",
    "SUSPENDED",
    "SUSPENDED_ON_CEILING_PROBE",
    "TRIPPED",
    "TRIPWIRES",
    "UNKNOWN",
    "WARN",
    "AbortRecord",
    "LiveReadings",
    "ReadingSource",
    "Tripwire",
    "Watchdog",
    "WatchdogCheck",
    "WatchdogError",
    "WatchdogReading",
    "WatchdogRun",
    "WatchdogThresholds",
    "abort_manifest",
    "check_error_rate",
    "check_error_rate_trend",
    "check_host_contention",
    "check_latency_ceiling",
    "check_load_generator_alive",
    "check_target_reachable",
    "check_throughput_collapse",
    "cpu_steal_pct",
    "error_trend_pct_per_check",
    "for_scenario",
    "probe_http",
    "read_locust_history",
    "read_proc_stat",
    "stop_locust",
]
