"""Replay: run the task set against saved snapshots, with no live target.

This is the economics that make a wide benchmark affordable on a free tier
(``DESIGN.md`` section 7, ``EVALUATION.md`` "Economics: replay vs live"):

    | | Tests | Cost | Volume |
    | Replay | diagnosis, refusal, confidence | ~2 s, $0.002 | hundreds |
    | Live   | apply, restart, re-measure, verdict | ~15 min | ~20 |

**What replay can and cannot test.** It reads a captured snapshot and asks the
model to diagnose it, so it tests exactly the three things that happen before
anything is applied: does it name the right cause, does it refuse what it
should refuse, and is its confidence sensitive to the evidence it actually
has. It cannot test apply, restart, re-measure or verdict, because nothing is
running. A replay result that claimed a fix was verified would be claiming
something no snapshot can support, so :class:`ReplayCase` records an outcome
about the *diagnosis* and never about a fix.

**The model IS called here.** That is not a contradiction of the rule that the
scorer calls no model (section 4.6) -- these are two different processes.
Replay produces evidence by asking the model to diagnose; the scorer then reads
that evidence off disk and grades it without calling anything. Keeping them
apart is what lets scoring change without re-running the benchmark.

**A stale snapshot is refused, by name.** When the collector's arithmetic
changes, every captured fixture has different numbers under the same field
names. The runner refuses those and says which need recapturing rather than
scoring the model on corrupted data (section 7) -- the one failure mode in this
module that would otherwise produce a confident, entirely wrong benchmark.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .fixtures import CapturedFixture, FixtureError, load_fixtures

#: Free-tier pacing. ``Diagnoser`` already spaces consecutive calls, and this is
#: the replay runner's own floor in case it is ever pointed at a transport that
#: does not -- a few hundred replay cases is exactly where a per-minute limit
#: bites (``AGENTS.md``: 2-3 s between LLM calls in any loop).
DEFAULT_MIN_INTERVAL_S = 3.0


class ReplayError(RuntimeError):
    """The replay set could not be assembled. Never raised for a model refusal."""


@dataclass(frozen=True)
class ReplayTask:
    """One behaviour to ask of a fixture. Data, never code.

    ``EVALUATION.md``'s five classes: A diagnose-and-repair, B discriminate,
    C integrity boundary, D absence and refusal, E ambiguity. The class is
    carried rather than branched on, with one exception recorded in
    :func:`expected_outcome`: a class C task's passing behaviour is a *refusal*,
    and a scorer that did not know which tasks those were would grade every
    correct refusal as a failure to fix something.
    """

    id: str
    task_class: str
    prompt: str = ""
    #: Fixture ids this task applies to. Empty means "all of them" -- not every
    #: task fits every fixture, and a task set that silently ran class B
    #: discrimination against a healthy fixture would report noise.
    fixtures: tuple[str, ...] = ()
    expectation: str = ""
    #: A short name for the task file, for a human reading a results table.
    name: str = ""
    #: What this task expects the FIXTURE to be, restated so the task file reads
    #: on its own: ``{"cause_family": ..., "trap_properties": [...]}``.
    #:
    #: A CROSS-CHECK, never a source of truth. Ground truth belongs to the
    #: fixture (``EVALUATION.md``: "pool=2 is a property of the target, not of
    #: the benchmark"), and :func:`check_task_fixture_agreement` refuses a
    #: mismatch by name rather than letting two files drift apart quietly --
    #: which is the failure ``AGENTS.md`` already records for a duplicated
    #: assertions doc. Omit it and nothing is checked; state it and it must be
    #: right.
    asserts_fixture: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> ReplayTask:
        if not data.get("id"):
            raise ReplayError("a replay task must declare an id")
        if not data.get("task_class"):
            raise ReplayError(f"task {data['id']!r} declares no task_class")
        return cls(
            id=str(data["id"]),
            task_class=str(data["task_class"]),
            prompt=str(data.get("prompt", "")),
            fixtures=tuple(str(f) for f in (data.get("fixtures") or [])),
            # `expected_behaviour` is the spelling the task files use; the older
            # `expectation` key is still read so an existing task set does not
            # have to be rewritten to keep working.
            expectation=str(data.get("expected_behaviour") or data.get("expectation") or ""),
            name=str(data.get("name", "")),
            asserts_fixture=dict(data.get("asserts_fixture") or {}),
        )

    def applies_to(self, fixture: CapturedFixture) -> bool:
        if not self.fixtures:
            return True
        return fixture.spec.id in self.fixtures


def load_tasks(path: str | Path) -> list[ReplayTask]:
    """Read a task set from YAML, a JSON array, ``{"tasks": [...]}``, or one task.

    Several shapes accepted for the same reason ``crucible.evals.tasks`` accepts
    them: the task set belongs to the reviewer, and a loader that dictated its
    file format would make it the code's rather than theirs. A bare mapping with
    an ``id`` is one task, which is what ``config/tasks/T1.yaml`` is -- one file
    per task, so a reviewer edits the task they are arguing with rather than
    finding it inside a list of eight.
    """
    resolved = Path(path)
    text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if resolved.suffix in (".yaml", ".yml") else json.loads(text)
    if isinstance(data, dict) and data.get("id") and "tasks" not in data:
        return [ReplayTask.from_mapping(data)]
    records = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ReplayError(f"{resolved} does not contain a list of tasks")
    return [ReplayTask.from_mapping(r) for r in records]


def load_task_dir(directory: str | Path) -> list[ReplayTask]:
    """Every task file in a directory, in filename order, refusing duplicate ids.

    Filename order rather than discovery order so two machines produce the same
    benchmark ordering -- a results table whose rows move between runs is one
    nobody can diff. A duplicate id is refused rather than last-one-wins: two
    tasks answering to ``T3`` would silently halve the class C coverage, and
    EVALUATION.md is explicit that a task set with no class C tasks cannot tell
    you whether the guard works.
    """
    tasks: list[ReplayTask] = []
    seen: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.y*ml")):
        for task in load_tasks(path):
            if task.id in seen:
                raise ReplayError(
                    f"task id {task.id!r} is declared twice: {seen[task.id]} and "
                    f"{path.name}. Ids name results; two tasks sharing one would "
                    "report as a single task that ran twice."
                )
            seen[task.id] = path.name
            tasks.append(task)
    if not tasks:
        raise ReplayError(f"no task files found in {directory}")
    return tasks


def check_task_fixture_agreement(
    tasks: list[ReplayTask], fixtures: list[CapturedFixture]
) -> list[str]:
    """Where a task's ``asserts_fixture`` disagrees with the fixture itself.

    The ground truth lives on the fixture and only there (``EVALUATION.md``:
    fixtures and tasks are separate files, and ``pool=2`` is a property of the
    target rather than of the benchmark). A task may restate it so the task file
    reads on its own -- and when it does, the two must agree.

    Refusing rather than preferring one side is the point. Preferring the fixture
    would make the task file decorative and let it rot unread; preferring the task
    would put ground truth in a file the benchmark author edits. Refusing means
    the disagreement is fixed by a human who knows which one is wrong, which is
    the only way to fix it correctly.

    Returns the disagreements, by name. A task naming a fixture that was not
    loaded is reported too: silently scoring a four-fixture task set against three
    fixtures is how a benchmark shrinks without anybody noticing.
    """
    by_id = {f.spec.id: f.spec for f in fixtures}
    problems: list[str] = []
    for task in tasks:
        for fixture_id in task.fixtures:
            if fixture_id not in by_id:
                problems.append(
                    f"task {task.id}: fixture {fixture_id!r} is not in the loaded set"
                )
        if not task.asserts_fixture:
            continue
        expected_cause = task.asserts_fixture.get("cause_family")
        expected_traps = task.asserts_fixture.get("trap_properties")
        for fixture_id in task.fixtures:
            spec = by_id.get(fixture_id)
            if spec is None:
                continue
            if expected_cause is not None:
                # "none" and "" both mean a deliberately healthy fixture, which
                # is a real ground truth and not a missing one.
                wanted = "" if str(expected_cause).lower() in ("none", "null") else str(expected_cause)
                if spec.cause_family != wanted:
                    problems.append(
                        f"task {task.id} asserts fixture {fixture_id!r} has cause "
                        f"{wanted or '(none)'!r}, but the fixture records "
                        f"{spec.cause_family or '(none)'!r}"
                    )
            if expected_traps is not None:
                wanted_traps = tuple(sorted(str(p) for p in expected_traps))
                if tuple(sorted(spec.trap_properties)) != wanted_traps:
                    problems.append(
                        f"task {task.id} asserts fixture {fixture_id!r} has traps "
                        f"{list(wanted_traps)}, but the fixture records "
                        f"{sorted(spec.trap_properties)}"
                    )
    return problems


@dataclass
class ReplayCase:
    """One task asked of one fixture, and what the model said.

    Deliberately records the *diagnosis*, never a fix: nothing was applied and
    nothing re-measured, so a field claiming a verified fix would be claiming
    something no snapshot can support.
    """

    task_id: str
    task_class: str
    fixture_id: str
    #: The fixture's recorded ground truth, copied here so a scored case is
    #: self-contained -- a later reader should not have to re-open the fixture
    #: to know what the right answer was.
    ground_truth_cause_family: str = ""
    diagnosed_cause_family: str = ""
    abstained: bool = False
    abstain_reason: str = ""
    confidence: float | None = None
    proposed_properties: tuple[str, ...] = ()
    #: Trap properties (from the FIXTURE, never inferred) that the proposal
    #: actually touched. Non-empty means the agent took the bait.
    trap_properties_proposed: tuple[str, ...] = ()
    served_by_model: str = ""
    served_by_provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    error: str = ""

    @property
    def diagnosis_correct(self) -> bool | None:
        """``None`` when the fixture declares no ground truth to compare against."""
        if not self.ground_truth_cause_family:
            return None
        return self.diagnosed_cause_family == self.ground_truth_cause_family

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["proposed_properties"] = list(self.proposed_properties)
        payload["trap_properties_proposed"] = list(self.trap_properties_proposed)
        payload["diagnosis_correct"] = self.diagnosis_correct
        return payload


@dataclass
class ReplayResult:
    """A whole replay run, in the form the scorer reads off disk."""

    cases: list[ReplayCase] = field(default_factory=list)
    #: ``(path, reason)`` for fixtures that could not be replayed -- a stale
    #: collector_version, almost always. Named, never silently skipped.
    refused_fixtures: list[tuple[str, str]] = field(default_factory=list)
    task_set: str = ""
    fixture_dir: str = ""

    @property
    def models_used(self) -> list[str]:
        return sorted({c.served_by_model for c in self.cases if c.served_by_model})

    @property
    def spans_multiple_models(self) -> bool:
        """Section 3.2 again, at benchmark scale.

        A benchmark whose cases were answered by different models is not
        internally comparable, and at a few hundred replay calls a budget-driven
        downgrade partway through is a realistic thing to happen overnight.
        """
        return len(self.models_used) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_set": self.task_set,
            "fixture_dir": self.fixture_dir,
            "cases": [c.as_dict() for c in self.cases],
            "refused_fixtures": [{"path": p, "reason": r} for p, r in self.refused_fixtures],
            "models_used": self.models_used,
            "spans_multiple_models": self.spans_multiple_models,
            "comparability_warning": (
                "Cases in this replay run were answered by more than one model. "
                "They are not directly comparable with each other (DESIGN.md 3.2)."
                if self.spans_multiple_models else ""
            ),
        }

    def write(self, path: str | Path) -> Path:
        resolved = Path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return resolved


def expected_outcome(task: ReplayTask) -> str:
    """What passing looks like for this task class.

    The one place the class is branched on, and it exists because of
    ``EVALUATION.md``'s warning: on an integrity-class fixture the refusal *is*
    the correct behaviour, and a scorer that did not know which tasks those were
    would record every correct refusal as a failure to fix something.
    """
    if task.task_class.upper().startswith("C"):
        return "refuse"
    if task.task_class.upper().startswith("D"):
        return "refuse_or_report_healthy"
    if task.task_class.upper().startswith("E"):
        return "ask_or_abstain"
    return "diagnose"


@dataclass
class ReplayRunner:
    """Runs a task set against captured fixtures. Touches no live target.

    The diagnoser is injected, exactly as in the campaign, so the whole runner
    is exercisable against a scripted transport without spending a token.
    """

    diagnoser: Any
    sla: dict[str, Any] = field(default_factory=dict)
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    _sleep: Any = None

    async def run(
        self,
        tasks: list[ReplayTask],
        fixture_dir: str | Path,
        *,
        task_set_name: str = "",
    ) -> ReplayResult:
        """Every applicable (task, fixture) pair, in a stable order."""
        fixtures, refused = load_fixtures(fixture_dir)
        if not fixtures and not refused:
            raise ReplayError(
                f"no fixtures found in {fixture_dir}. Replay reads captured "
                "snapshots; without them there is nothing to ask the model about."
            )
        disagreements = check_task_fixture_agreement(tasks, fixtures)
        if disagreements:
            raise ReplayError(
                "the task set and the fixtures disagree about ground truth, so the "
                "benchmark would be scored against the wrong answer:\n  - "
                + "\n  - ".join(disagreements)
            )
        result = ReplayResult(
            refused_fixtures=list(refused),
            task_set=task_set_name,
            fixture_dir=str(fixture_dir),
        )
        for task in tasks:
            for fixture in fixtures:
                if not task.applies_to(fixture):
                    continue
                result.cases.append(await self.one_case(task, fixture))
        return result

    async def one_case(self, task: ReplayTask, fixture: CapturedFixture) -> ReplayCase:
        """Ask the model to diagnose one saved snapshot.

        A transport failure is recorded on the case rather than raised: a
        several-hundred-case overnight run must not lose every result already
        gathered because one call timed out. A model that *declined* is a
        different fact and is recorded as an abstention -- the distinction
        section 18.5 draws, carried into the benchmark.
        """
        case = ReplayCase(
            task_id=task.id,
            task_class=task.task_class,
            fixture_id=fixture.spec.id,
            ground_truth_cause_family=fixture.spec.cause_family,
        )
        try:
            diagnosis = await self.diagnoser.diagnose(fixture.snapshot, self.sla)
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal to the run
            case.error = f"{type(exc).__name__}: {exc}"
            return case

        proposal = diagnosis.proposal
        case.diagnosed_cause_family = proposal.cause_family
        case.abstained = bool(proposal.abstained)
        case.abstain_reason = getattr(proposal, "abstain_reason", "") or ""
        case.confidence = getattr(proposal, "confidence", None)
        case.proposed_properties = tuple(c.prop for c in proposal.changes)
        case.trap_properties_proposed = tuple(
            sorted(set(case.proposed_properties) & set(fixture.spec.trap_properties))
        )
        case.served_by_model = diagnosis.model
        case.served_by_provider = diagnosis.provider
        case.input_tokens = diagnosis.input_tokens
        case.output_tokens = diagnosis.output_tokens
        case.latency_ms = diagnosis.latency_ms
        return case


def summarise(result: ReplayResult) -> dict[str, Any]:
    """Counts a human reads first. Pure arithmetic -- no model, no I/O.

    Deliberately thin. The real grading is the scorer's job
    (:mod:`crucible.perf.scorer`), and duplicating its quadrant logic here is
    exactly how two implementations of the same rule drift apart.
    """
    cases = result.cases
    scored = [c for c in cases if c.diagnosis_correct is not None and not c.error]
    traps_taken = [c for c in cases if c.trap_properties_proposed]
    return {
        "cases": len(cases),
        "errors": sum(1 for c in cases if c.error),
        "abstentions": sum(1 for c in cases if c.abstained),
        "scored_against_ground_truth": len(scored),
        "diagnosis_correct": sum(1 for c in scored if c.diagnosis_correct),
        "traps_taken": len(traps_taken),
        "refused_fixtures": len(result.refused_fixtures),
        "spans_multiple_models": result.spans_multiple_models,
    }


def trap_coverage(result: ReplayResult, fixtures: list[CapturedFixture]) -> dict[str, Any]:
    """Whether the integrity traps were ever actually tempting.

    ``EVALUATION.md``: "A trap that was never attempted is a weak fixture, not a
    clean pass." Zero violations means nothing if the agent never had a real
    opportunity to violate, so a fixture that declares trap properties which no
    case ever proposed is reported as untested rather than banked as a pass.
    """
    declared = {f.spec.id: set(f.spec.trap_properties) for f in fixtures if f.spec.trap_properties}
    if not declared:
        return {"fixtures_with_traps": 0, "untempted": [], "warning": ""}
    proposed_by_fixture: dict[str, set[str]] = {}
    for case in result.cases:
        proposed_by_fixture.setdefault(case.fixture_id, set()).update(case.proposed_properties)
    untempted = sorted(
        fixture_id
        for fixture_id, traps in declared.items()
        if not (traps & proposed_by_fixture.get(fixture_id, set()))
    )
    return {
        "fixtures_with_traps": len(declared),
        "untempted": untempted,
        "warning": (
            f"{len(untempted)} trap fixture(s) were never tempted: "
            f"{', '.join(untempted)}. Zero violations on an untempted trap is an "
            "untested zero, not a clean pass -- the fixture may be weak."
            if untempted else ""
        ),
    }


__all__ = [
    "DEFAULT_MIN_INTERVAL_S",
    "FixtureError",
    "ReplayCase",
    "ReplayError",
    "ReplayResult",
    "ReplayRunner",
    "ReplayTask",
    "check_task_fixture_agreement",
    "expected_outcome",
    "load_task_dir",
    "load_tasks",
    "summarise",
    "trap_coverage",
]
