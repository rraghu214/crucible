"""Fixtures: one target state, captured once, replayed hundreds of times.

``EVALUATION.md`` draws the line this module implements. A **fixture** is one
target application in one known broken state, with the true cause recorded
*before* any run -- ``perflab_pool_starved`` is a fixture. A **task** is a
behaviour the harness must exhibit. They live in separate files so the same task
set runs against a second target by swapping fixtures rather than rewriting
tasks, and because the ground truth belongs to the fixture: an agent's own
manifest can never certify whether its diagnosis was right.

**Capture is the expensive half and happens once.** 120 s warmup discarded,
300 s measured, ~120 s restart and settle -- roughly nine minutes per fixture,
so fifty fixtures is one overnight run (``EVALUATION.md``, "Capture cost").
Everything after that is replay at ~2 s and $0.002 a case, which is the
arithmetic that makes a wide benchmark affordable on free tiers (``DESIGN.md``
section 7).

**Because it happens once, it has to be captured properly.** The warmup is not
optional: a cold JVM measured p99 150 ms where a warm one measured 98 on the
same box, a ~50% gap against a 2.08% noise floor. A fixture captured without it
bakes that gap into every replay that ever reads it.

**And every snapshot carries ``collector_version``.** When the collector's
arithmetic changes, every previously captured fixture has wrong numbers baked
into the same field names, and replaying against one scores the model on
corrupted data with nothing to flag it. :func:`load_fixture` refuses a mismatch
and names what needs recapturing rather than running quietly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .collector import COLLECTOR_VERSION

#: ``EVALUATION.md``: 120 s discarded, 300 s measured. Defaults rather than
#: constants -- a scenario may legitimately want longer -- but a capture that
#: silently used zero warmup would poison every replay built on it, so the
#: defaults are here and :func:`check_capture_settings` refuses a short one.
DEFAULT_WARMUP_S = 120.0
DEFAULT_MEASURE_S = 300.0

#: The K1 re-validation gate for week 3 (see :func:`p99_spread_pct`). Looser than
#: the per-environment noise floor in ``config/slo.yaml`` on purpose: this asks
#: "is this box stable enough to capture fixtures on at all", not "is this
#: particular improvement real".
K1_REVALIDATION_MAX_SPREAD_PCT = 20.0


class FixtureError(RuntimeError):
    """A fixture could not be captured, loaded, or trusted."""


@dataclass
class FixtureSpec:
    """One target state, and the truth about it recorded BEFORE any run.

    ``cause_family`` is the ground truth. It is written here, by a human setting
    the fixture up, and never by anything that later reads the fixture -- which
    is the whole basis on which :func:`crucible.perf.scorer.score_diagnosis` can
    tell ``CORRECT`` from ``LUCKY``.
    """

    id: str
    #: The ground-truth cause family, in the vocabulary of the fixture's own
    #: profile. Empty for a deliberately healthy fixture, which is a real case:
    #: an agent that always finds something will eventually tune a healthy
    #: service (`EVALUATION.md` task class D).
    cause_family: str = ""
    profile: str = "spring-boot"
    #: How the target was put into this state -- the property changed, the
    #: endpoint hit, the data seeded. Prose, for a human reproducing it.
    setup: str = ""
    #: Which task classes this fixture can answer. Not every task fits every
    #: fixture (`EVALUATION.md`, "Test case").
    task_classes: tuple[str, ...] = ()
    #: Properties that would improve a headline number without fixing the cause
    #: ON THIS FIXTURE -- the connection-timeout shortcut being the canonical
    #: one. Fixture metadata, because whether a property is a shortcut depends
    #: on what is actually wrong; the scorer takes this as `trap_properties`
    #: and never infers it.
    trap_properties: tuple[str, ...] = ()
    #: The exact configuration that puts the target into this state, as
    #: property -> value. Machine-readable alongside the prose in `setup`,
    #: because "pool=2" is the part a later reader has to reproduce EXACTLY and
    #: prose is where a digit goes missing. Never applied by Crucible: setting
    #: the target up is the human's job (see `capture_fixture`).
    bottleneck_config: dict[str, Any] = field(default_factory=dict)
    #: `mild` | `moderate` | `severe`, or `none` for a healthy fixture.
    #: EVALUATION.md's grid is 10 families x 3 severities, and severity is what
    #: separates "it spots the unmissable one" from "it spots the subtle one".
    severity: str = ""
    #: The date a K1 re-validation confirmed this fixture's signal is actually
    #: present and stable on the box. EMPTY MEANS NOT YET CONFIRMED -- a fixture
    #: whose bottleneck does not reproduce captures a snapshot of nothing, and
    #: every replay case built on it scores the model against an answer that was
    #: never in the data.
    validated_at: str = ""
    #: The load-generator tag that selects THIS fixture's endpoint.
    #:
    #: Declared, never guessed, and never empty. ``locust/locustfile.py`` has one
    #: tagged task per cause family and says in its own docstring to "run one
    #: family at a time, selected by tag, so a scenario measures one signal". A
    #: capture that omitted the tag ran all nine tasks and measured a BLEND:
    #: observed on Box A on 26 September 2026, where an untagged run reported an
    #: aggregate p99 of 420 ms that belonged almost entirely to `/api/downstream`
    #: (410 ms p50, httpbin behaving exactly as designed) while `/api/db`, the
    #: endpoint the SLA is about, sat at 56/210 and contributed a ninth of the
    #: traffic. A pool-starvation signal measured that way is diluted to
    #: invisibility, and the resulting fixture would look plausible and be
    #: worthless.
    scenario_tag: str = ""
    #: Which metrics providers to capture this state through. A fixture is a
    #: target state seen THROUGH a provider, so the same state under Actuator and
    #: under PromQL is two snapshots (`EVALUATION.md`, "Open: fixtures vs
    #: snapshots"). Multi-provider capture is what makes provider independence a
    #: measured claim rather than an architectural one.
    providers: tuple[str, ...] = ("actuator",)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> FixtureSpec:
        """Read one fixture spec.

        Two spellings are accepted for the two fields whose names carry weight:
        ``ground_truth_cause_family`` and ``target_profile`` are the explicit
        forms the fixture files use, and the shorter ``cause_family`` /
        ``profile`` stay readable for an already-captured fixture's embedded
        spec. Preferring the explicit spelling is not decoration -- "ground
        truth" is precisely the property that makes this field different from the
        `cause_family` the agent writes on a manifest.
        """
        if not data.get("id"):
            raise FixtureError("a fixture must declare an id")
        cause = data.get("ground_truth_cause_family", data.get("cause_family", ""))
        # `none` is how a healthy fixture states its ground truth out loud. It
        # means the same as empty, and saying it explicitly is better than an
        # omission nobody can tell from an oversight.
        if str(cause).strip().lower() in ("none", "null"):
            cause = ""
        return cls(
            id=str(data["id"]),
            cause_family=str(cause),
            profile=str(data.get("target_profile") or data.get("profile") or "spring-boot"),
            setup=str(data.get("setup", "")),
            task_classes=tuple(str(c) for c in (data.get("task_classes") or [])),
            trap_properties=tuple(str(p) for p in (data.get("trap_properties") or [])),
            bottleneck_config=dict(data.get("bottleneck_config") or {}),
            severity=str(data.get("severity", "")),
            scenario_tag=str(data.get("scenario_tag", "")),
            validated_at=str(data.get("validated_at") or ""),
            # `or` would be wrong here. An EMPTY providers list is a deliberate
            # statement -- "declared but not captured", which is how a fixture
            # blocked on missing instrumentation stays written down without
            # contributing a snapshot nobody can trust. `or` is falsy on `[]` and
            # would quietly restore the default, capturing exactly the fixture
            # somebody had excluded.
            providers=(
                ("actuator",)
                if data.get("providers") is None
                else tuple(str(p) for p in data["providers"])
            ),
            notes=str(data.get("notes", "")),
        )


@dataclass
class CapturedFixture:
    """A fixture spec plus the snapshot captured from it, as written to disk."""

    spec: FixtureSpec
    snapshot: dict[str, Any] = field(default_factory=dict)
    #: Which metrics provider produced the snapshot. A fixture is a target state
    #: seen THROUGH a provider, and the same state seen through Actuator and
    #: through PromQL is two snapshots, not one (`EVALUATION.md`, "Open:
    #: fixtures vs snapshots").
    provider: str = "actuator"
    captured_at_epoch_s: float = 0.0
    warmup_s: float = DEFAULT_WARMUP_S
    measure_s: float = DEFAULT_MEASURE_S

    @property
    def collector_version(self) -> str:
        return str(self.snapshot.get("collector_version", ""))

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.as_dict(),
            "provider": self.provider,
            "captured_at_epoch_s": self.captured_at_epoch_s,
            "warmup_s": self.warmup_s,
            "measure_s": self.measure_s,
            "snapshot": self.snapshot,
        }

    def write(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.spec.id}.{self.provider}.json"
        path.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return path


def load_specs(directory: str | Path) -> list[FixtureSpec]:
    """Every fixture spec in ``config/fixtures/``, in filename order.

    These are the capture PLAN -- target states somebody intends to capture --
    not captured fixtures. :func:`load_fixtures` reads the other thing: snapshots
    already on disk, with a ``collector_version`` to check. The two are separate
    functions because they answer different questions, and a loader that returned
    both would let "we mean to capture this" be mistaken for "we captured it".

    A duplicate id is refused. Ids name the snapshot files
    (``<id>.<provider>.json``), so two specs sharing one would have the second
    overwrite the first and leave a benchmark quietly one fixture short.
    """
    specs: list[FixtureSpec] = []
    seen: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.y*ml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise FixtureError(f"{path.name} must contain a single fixture mapping")
        spec = FixtureSpec.from_mapping(data)
        if spec.id in seen:
            raise FixtureError(
                f"fixture id {spec.id!r} is declared twice: {seen[spec.id]} and "
                f"{path.name}. Ids name the captured snapshot files, so one would "
                "overwrite the other."
            )
        seen[spec.id] = path.name
        specs.append(spec)
    if not specs:
        raise FixtureError(f"no fixture specs found in {directory}")
    return specs


def capture_plan(specs: list[FixtureSpec], *, per_fixture_minutes: float = 9.0) -> dict[str, Any]:
    """What an overnight capture run would do, before it is started.

    ``EVALUATION.md`` prices capture at ~9 minutes per fixture -- 120 s warmup
    discarded, 300 s measured, ~120 s restart and settle. The arithmetic is worth
    doing in advance rather than discovering at 4am that the run does not fit the
    night: every snapshot is a fixture x provider pair, so tagging three fixtures
    for three providers each adds six captures, not three.

    Unvalidated fixtures are NAMED rather than counted. A fixture whose signal
    nobody has confirmed captures a snapshot of nothing in particular, and every
    replay case built on it scores the model against an answer that was never in
    the data -- which looks exactly like a model that got it wrong.
    """
    snapshots = [(spec.id, provider) for spec in specs for provider in spec.providers]
    # A fixture declaring no providers is excluded ON PURPOSE -- normally because
    # the evidence its cause needs is not in the snapshot yet. It is reported
    # separately from the unvalidated ones: "nobody has confirmed this signal" and
    # "this one is deliberately not being captured" call for different responses,
    # and rolling them together would bury a decision inside a warning.
    excluded = [spec.id for spec in specs if not spec.providers]
    unvalidated = [spec.id for spec in specs if spec.providers and not spec.validated_at]
    return {
        "fixtures": len(specs),
        "capturing": len(specs) - len(excluded),
        "snapshots": len(snapshots),
        "pairs": snapshots,
        "estimated_minutes": len(snapshots) * per_fixture_minutes,
        "estimated_hours": round(len(snapshots) * per_fixture_minutes / 60.0, 2),
        "providers": sorted({provider for _id, provider in snapshots}),
        "excluded": excluded,
        "unvalidated": unvalidated,
        "warning": (
            f"{len(unvalidated)} fixture(s) have no validated_at date: "
            f"{', '.join(unvalidated)}. Their bottleneck signal has not been "
            "confirmed to reproduce on this box, and a fixture that does not "
            "reproduce captures a snapshot of nothing."
            if unvalidated
            else ""
        ),
        "excluded_note": (
            f"{len(excluded)} fixture(s) declare no providers and are not being "
            f"captured: {', '.join(excluded)}. See docs/ref/DEBT.md."
            if excluded
            else ""
        ),
    }


def check_capture_settings(warmup_s: float, measure_s: float) -> None:
    """Refuse a capture that would bake a cold-start gap into every replay.

    A zero or short warmup is the one capture mistake that cannot be corrected
    afterwards and cannot be seen in the result: the numbers look entirely
    ordinary, they are simply a measurement of a JVM compiling hot code. On the
    Oracle box that was a ~50% gap -- 150 ms cold against 98 ms warm -- and the
    noise floor it would be judged against is 2.08%.
    """
    if warmup_s < DEFAULT_WARMUP_S:
        raise FixtureError(
            f"warmup of {warmup_s:g}s is below the {DEFAULT_WARMUP_S:g}s "
            "EVALUATION.md requires for a captured fixture. A cold JVM measured "
            "150 ms where a warm one measured 98 on the same box; capturing that "
            "bakes the gap into every replay and nothing downstream can see it."
        )
    if measure_s < DEFAULT_MEASURE_S:
        raise FixtureError(
            f"measured window of {measure_s:g}s is below the {DEFAULT_MEASURE_S:g}s "
            "EVALUATION.md requires. A fixture is captured once and replayed "
            "hundreds of times; a short window is a permanent cost."
        )


def capture_fixture(
    spec: FixtureSpec,
    measure: Any,
    *,
    scenario: Any,
    provider_name: str = "actuator",
    warmup_s: float = DEFAULT_WARMUP_S,
    measure_s: float = DEFAULT_MEASURE_S,
) -> CapturedFixture:
    """Run one fixture's measured window and package the snapshot.

    ``measure`` is the same ``(scenario, run_id) -> (LoadResult, snapshot)``
    callable the campaign takes, injected for the same reason: the real one runs
    Locust for eight minutes and a test must not.

    This does not set the target up. Putting the application into the broken
    state is the human's job and is recorded in ``spec.setup`` -- automating it
    would mean Crucible writing the very configuration whose effect it is
    supposed to measure independently.
    """
    check_capture_settings(warmup_s, measure_s)
    _load, snapshot = measure(scenario, f"fixture-{spec.id}")
    if snapshot.get("collector_version") != COLLECTOR_VERSION:
        raise FixtureError(
            f"fixture {spec.id!r} was built by collector "
            f"{snapshot.get('collector_version')!r} but this process is "
            f"{COLLECTOR_VERSION!r}. Refusing to write a snapshot that is stale "
            "the moment it lands."
        )
    return CapturedFixture(
        spec=spec,
        snapshot=snapshot,
        provider=provider_name,
        captured_at_epoch_s=float(snapshot.get("captured_at_epoch_s") or 0.0),
        warmup_s=warmup_s,
        measure_s=measure_s,
    )


def load_fixture(path: str | Path) -> CapturedFixture:
    """Load one captured fixture, refusing a collector mismatch by name.

    ``DESIGN.md`` section 7: when the collector changes, old snapshots have
    wrong numbers baked in, and the eval runner must refuse mismatched snapshots
    and say which need recapture rather than silently scoring the model on
    corrupted data.
    """
    resolved = Path(path)
    data = json.loads(resolved.read_text(encoding="utf-8"))
    snapshot = data.get("snapshot") or {}
    version = snapshot.get("collector_version")
    if version != COLLECTOR_VERSION:
        raise FixtureError(
            f"{resolved.name}: snapshot was captured by collector {version!r}, but "
            f"this collector is {COLLECTOR_VERSION!r}. Recapture this fixture -- "
            "its numbers were computed by different arithmetic under the same "
            "field names, and replaying it would score the model on corrupted data."
        )
    return CapturedFixture(
        spec=FixtureSpec.from_mapping(data.get("spec") or {}),
        snapshot=snapshot,
        provider=str(data.get("provider", "actuator")),
        captured_at_epoch_s=float(data.get("captured_at_epoch_s") or 0.0),
        warmup_s=float(data.get("warmup_s") or DEFAULT_WARMUP_S),
        measure_s=float(data.get("measure_s") or DEFAULT_MEASURE_S),
    )


def load_fixtures(directory: str | Path) -> tuple[list[CapturedFixture], list[tuple[str, str]]]:
    """Every fixture in a directory, plus the ones that were refused.

    Returns ``(loaded, refused)`` where ``refused`` is ``(path, reason)``. One
    stale fixture must not hide the rest, and the stale ones must be *named* --
    "some fixtures were skipped" is not an actionable message at 3am.
    """
    loaded: list[CapturedFixture] = []
    refused: list[tuple[str, str]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            loaded.append(load_fixture(path))
        except (OSError, ValueError, FixtureError) as exc:
            refused.append((str(path), str(exc)))
    return loaded, refused


# ---------------------------------------------------------------------------
# K1 re-validation -- pure arithmetic over repeated identical runs
# ---------------------------------------------------------------------------


def p99_spread_pct(p99s: list[float] | tuple[float, ...]) -> float | None:
    """Spread across identical runs, as a percentage of the median.

    This is the number K1 produced: 14.3% on the local Windows machine with
    Locust co-located and H2 behind the target, 2.08% on the Oracle box
    (`docs/K1_RESULT.md`, `docs/K1_CLOUD_RESULT.md`). It is a property of the
    BOX, not of Crucible, which is why it is measured per environment rather
    than assumed.

    ``None`` for fewer than two runs: a spread across one measurement is not a
    small spread, it is no spread at all, and returning ``0.0`` would read as
    a perfectly stable box.
    """
    values = [float(p) for p in p99s if p is not None]
    if len(values) < 2:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    if not median:
        return None
    return 100.0 * (max(ordered) - min(ordered)) / median


def k1_revalidation(
    p99s: list[float] | tuple[float, ...],
    *,
    max_spread_pct: float = K1_REVALIDATION_MAX_SPREAD_PCT,
) -> dict[str, Any]:
    """Whether this box is stable enough to capture fixtures on.

    Run before the overnight capture, not after: fifty fixtures captured on a
    box whose identical runs disagree by 30% are fifty fixtures that have to be
    captured again, and the replay results built on them in the meantime are
    worth nothing.

    Reports the numbers alongside the verdict, because "passed" on its own
    hides the difference between 3% and 19%.
    """
    spread = p99_spread_pct(p99s)
    values = [float(p) for p in p99s if p is not None]
    return {
        "runs": len(values),
        "p99s_ms": values,
        "spread_pct": spread,
        "max_spread_pct": max_spread_pct,
        "passed": spread is not None and spread <= max_spread_pct,
        "reason": (
            "fewer than two runs; a spread needs at least two measurements"
            if spread is None
            else f"spread {spread:.2f}% against a {max_spread_pct:.2f}% ceiling"
        ),
    }
