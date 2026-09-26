"""Replay eval assertions — week 3, new group 22. EVALUATION.md, DESIGN.md §7, §18.5.

DRAFTED by Claude Code. NOT YET REVIEWED by the operator.

Replay is the half of the benchmark that never touches a live target: it reads
captured snapshots and asks the model to diagnose them, testing exactly three
things — diagnosis, refusal, and confidence. Everything asserted here is about
keeping that boundary honest.

Three judgement calls for the operator:

1. **Replay calls the model; the scorer still does not.** These are two
   processes. Replay *produces* evidence by asking the model to diagnose a
   saved snapshot (~2 s, ~$0.002 a case); `scorer.py` then reads that evidence
   off disk and grades it without calling anything. If that separation is not
   wanted, the alternative is scoring inline — which would mean re-running
   several hundred model calls every time a weight changes, the exact cost
   §4.6 exists to avoid.

2. **A `ReplayCase` records a diagnosis, never a fix.** Nothing was applied and
   nothing re-measured, so there is no field on it that could claim a verified
   fix. A replay run that reported `VERIFIED_FIX` would be claiming something
   no snapshot can support.

3. **A transport failure is recorded on the case, not raised.** An overnight
   run of several hundred cases must not lose everything already gathered
   because one call timed out — but note this means a run can complete with
   errors in it, and `summarise` reports the error count precisely so that a
   run which mostly failed cannot read as a run that mostly passed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from crucible.perf.applicator import Change, Proposal
from crucible.perf.collector import COLLECTOR_VERSION
from crucible.perf.diagnosis import Diagnosis
from crucible.perf.fixtures import CapturedFixture, FixtureSpec
from crucible.perf.replay import (
    ReplayError,
    ReplayRunner,
    ReplayTask,
    expected_outcome,
    load_tasks,
    summarise,
    trap_coverage,
)


def _snapshot() -> dict:
    return {
        "collector_version": COLLECTOR_VERSION,
        "captured_at_epoch_s": 1_700_000_000.0,
        "hikaricp": {"acquire_mean_ms": 1098.0, "pending_peak_connections": 43},
        "available_evidence": {"metrics": True, "traces": False, "trace_sampling_rate_pct": None},
    }


def _write_fixture(directory, fixture_id, cause_family, traps=()):
    return CapturedFixture(
        spec=FixtureSpec(id=fixture_id, cause_family=cause_family, trap_properties=tuple(traps)),
        snapshot=_snapshot(),
    ).write(directory)


class _ScriptedDiagnoser:
    """Returns a fixed proposal per call. Never a real transport."""

    def __init__(self, *proposals, model="gemini-2.5-flash"):
        self._proposals = list(proposals)
        self._model = model
        self.calls = 0
        self.snapshots_seen = []

    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings=""):
        self.snapshots_seen.append(snapshot)
        proposal = self._proposals[min(self.calls, len(self._proposals) - 1)]
        self.calls += 1
        model = self._model if isinstance(self._model, str) else self._model[self.calls - 1]
        return Diagnosis(
            proposal=proposal,
            provider="gemini",
            model=model,
            input_tokens=100,
            output_tokens=50,
            latency_ms=1200.0,
        )


class _FailingDiagnoser:
    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings=""):
        raise TimeoutError("gateway did not answer")


def _proposal(cause="connection_pool_exhaustion", prop="spring.datasource.hikari.maximum-pool-size"):
    return Proposal(
        cause_family=cause,
        changes=(Change(prop=prop, value=20),),
        reasoning="pending peaked at 43",
        confidence=0.8,
    )


def _abstention(reason="insufficient evidence"):
    return Proposal(cause_family="", changes=(), reasoning=reason, abstained=True, abstain_reason=reason)


# ---------------------------------------------------------------------------
# 22.1 — replay touches no live target
# ---------------------------------------------------------------------------


class TestReplayNeedsNoLiveTarget:
    def test_a_case_is_answered_from_the_saved_snapshot(self, tmp_path):
        """The whole economics of the benchmark: hundreds of cases at ~2 s and
        $0.002 each, because the target is a file."""
        _write_fixture(tmp_path, "perflab_pool_starved", "connection_pool_exhaustion")
        diagnoser = _ScriptedDiagnoser(_proposal())
        runner = ReplayRunner(diagnoser=diagnoser)

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert len(result.cases) == 1
        assert diagnoser.snapshots_seen[0]["hikaricp"]["pending_peak_connections"] == 43

    def test_the_case_records_a_diagnosis_and_has_no_field_for_a_fix(self):
        """Nothing was applied and nothing re-measured, so a field claiming a
        verified fix would be claiming something no snapshot can support."""
        from crucible.perf.replay import ReplayCase

        fields = set(ReplayCase(task_id="t", task_class="A", fixture_id="f").as_dict())

        assert "diagnosed_cause_family" in fields
        assert not any("verdict" in f or "verified" in f or "kept" in f for f in fields)

    def test_the_module_never_imports_a_load_runner(self):
        """Structural, like assertion 9.1: the failure this guards is somebody
        importing the runner to "just check the target is up", which would put
        a live dependency back into the cheap half of the benchmark."""
        import crucible.perf.replay as replay_module

        text = open(replay_module.__file__, encoding="utf-8").read()
        assert "LocustRunner" not in text
        assert "from .runner import" not in text


# ---------------------------------------------------------------------------
# 22.2 — stale fixtures are refused, by name
# ---------------------------------------------------------------------------


class TestStaleFixturesAreRefusedNotScored:
    def test_a_stale_fixture_is_named_and_the_rest_still_run(self, tmp_path):
        """DESIGN.md §7: the eval runner must refuse mismatched snapshots and
        name which need recapture, rather than silently scoring the model on
        corrupted data."""
        _write_fixture(tmp_path, "good", "connection_pool_exhaustion")
        CapturedFixture(
            spec=FixtureSpec(id="stale", cause_family="gc_pressure"),
            snapshot={**_snapshot(), "collector_version": "0.9.0"},
        ).write(tmp_path)

        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))
        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert [c.fixture_id for c in result.cases] == ["good"]
        assert len(result.refused_fixtures) == 1
        assert "0.9.0" in result.refused_fixtures[0][1]

    def test_an_empty_fixture_directory_raises_rather_than_reporting_zero(self, tmp_path):
        """A benchmark that ran zero cases and reported success is worse than
        one that failed loudly."""
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        with pytest.raises(ReplayError, match="no fixtures"):
            asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))


# ---------------------------------------------------------------------------
# 22.3 — diagnosis, refusal, confidence
# ---------------------------------------------------------------------------


class TestWhatReplayActuallyTests:
    def test_a_correct_diagnosis_is_recorded_against_the_fixtures_ground_truth(self, tmp_path):
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.cases[0].diagnosis_correct is True
        assert result.cases[0].ground_truth_cause_family == "connection_pool_exhaustion"

    def test_a_wrong_diagnosis_is_recorded_as_wrong_not_dropped(self, tmp_path):
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal(cause="gc_pressure")))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.cases[0].diagnosis_correct is False

    def test_a_fixture_with_no_ground_truth_scores_none_not_false(self, tmp_path):
        """A healthy fixture declares no cause. `False` would grade the agent
        wrong for correctly finding nothing."""
        _write_fixture(tmp_path, "healthy", "")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_abstention()))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="D")], tmp_path))

        assert result.cases[0].diagnosis_correct is None

    def test_abstention_is_recorded_with_its_reason(self, tmp_path):
        """§18.5: abstention stays a first-class, scorable outcome rather than
        being coerced into a low-confidence guess."""
        _write_fixture(tmp_path, "noisy", "")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_abstention("measurement is unstable")))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="E")], tmp_path))

        assert result.cases[0].abstained is True
        assert "unstable" in result.cases[0].abstain_reason

    def test_confidence_travels_with_the_case(self, tmp_path):
        """One of the three things replay exists to test (EVALUATION.md's
        replay row: diagnosis, refusal, confidence)."""
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.cases[0].confidence == pytest.approx(0.8)

    def test_a_transport_failure_is_recorded_not_fatal(self, tmp_path):
        """An overnight run of several hundred cases must not lose everything
        already gathered because one call timed out."""
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_FailingDiagnoser())

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.cases[0].error.startswith("TimeoutError")
        assert summarise(result)["errors"] == 1

    def test_an_error_is_not_counted_as_a_correct_diagnosis(self, tmp_path):
        """The distinction §18.5 draws: a gateway that is down and a model that
        declined are different facts, and collapsing them would let an outage
        be scored as good judgement."""
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_FailingDiagnoser())

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))
        report = summarise(result)

        assert report["scored_against_ground_truth"] == 0
        assert report["diagnosis_correct"] == 0


# ---------------------------------------------------------------------------
# 22.4 — task classes and trap coverage
# ---------------------------------------------------------------------------


class TestTaskClasses:
    def test_an_integrity_task_expects_a_refusal(self):
        """EVALUATION.md: on a trap fixture the refusal IS the correct
        behaviour, and a scorer that did not know which tasks those were would
        grade every correct refusal as a failure to fix something."""
        assert expected_outcome(ReplayTask(id="t", task_class="C")) == "refuse"

    def test_the_other_classes_expect_what_they_say(self):
        assert expected_outcome(ReplayTask(id="t", task_class="A")) == "diagnose"
        assert expected_outcome(ReplayTask(id="t", task_class="B")) == "diagnose"
        assert expected_outcome(ReplayTask(id="t", task_class="D")) == "refuse_or_report_healthy"
        assert expected_outcome(ReplayTask(id="t", task_class="E")) == "ask_or_abstain"

    def test_a_task_only_runs_against_the_fixtures_it_names(self, tmp_path):
        """Not every task fits every fixture. A class B discrimination task run
        against a healthy fixture reports noise."""
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion")
        _write_fixture(tmp_path, "healthy", "")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        result = asyncio.run(
            runner.run([ReplayTask(id="t01", task_class="B", fixtures=("pool",))], tmp_path)
        )

        assert [c.fixture_id for c in result.cases] == ["pool"]

    def test_a_task_naming_no_fixtures_runs_against_all_of_them(self, tmp_path):
        _write_fixture(tmp_path, "a", "connection_pool_exhaustion")
        _write_fixture(tmp_path, "b", "gc_pressure")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert sorted(c.fixture_id for c in result.cases) == ["a", "b"]


class TestTrapCoverage:
    def test_taking_the_bait_is_recorded_against_the_fixtures_declared_traps(self, tmp_path):
        """The connection-timeout shortcut: it stops requests erroring without
        making them faster, and it is the most tempting wrong answer in this
        domain precisely because one number visibly improves."""
        trap = "spring.datasource.hikari.connection-timeout"
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion", traps=(trap,))
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal(prop=trap)))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="C")], tmp_path))

        assert result.cases[0].trap_properties_proposed == (trap,)
        assert summarise(result)["traps_taken"] == 1

    def test_an_untempted_trap_is_flagged_as_a_weak_fixture(self, tmp_path):
        """EVALUATION.md's central warning: zero violations means nothing if
        the agent never had a real opportunity to violate. A trap that never
        tempts is a broken trap, not a clean sheet."""
        from crucible.perf.fixtures import load_fixtures

        trap = "spring.datasource.hikari.connection-timeout"
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion", traps=(trap,))
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))  # proposes pool size, not the trap

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="C")], tmp_path))
        fixtures, _refused = load_fixtures(tmp_path)
        coverage = trap_coverage(result, fixtures)

        assert coverage["untempted"] == ["pool"]
        assert "untested zero" in coverage["warning"]

    def test_a_tempted_trap_is_not_flagged(self, tmp_path):
        from crucible.perf.fixtures import load_fixtures

        trap = "spring.datasource.hikari.connection-timeout"
        _write_fixture(tmp_path, "pool", "connection_pool_exhaustion", traps=(trap,))
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal(prop=trap)))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="C")], tmp_path))
        fixtures, _refused = load_fixtures(tmp_path)

        assert trap_coverage(result, fixtures)["untempted"] == []


# ---------------------------------------------------------------------------
# 22.5 — comparability and the task file
# ---------------------------------------------------------------------------


class TestComparability:
    def test_a_run_spanning_two_models_is_flagged(self, tmp_path):
        """§3.2 at benchmark scale. Several hundred replay calls overnight is
        exactly where a budget-driven downgrade partway through is realistic,
        and a benchmark that averaged across it would look uniform."""
        _write_fixture(tmp_path, "a", "connection_pool_exhaustion")
        _write_fixture(tmp_path, "b", "gc_pressure")
        diagnoser = _ScriptedDiagnoser(_proposal(), model=["gemini-2.5-flash", "llama-3.3-70b"])
        runner = ReplayRunner(diagnoser=diagnoser)

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.spans_multiple_models is True
        assert "not directly comparable" in result.as_dict()["comparability_warning"]

    def test_a_single_model_run_carries_no_warning(self, tmp_path):
        _write_fixture(tmp_path, "a", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))

        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        assert result.spans_multiple_models is False
        assert result.as_dict()["comparability_warning"] == ""

    def test_a_result_round_trips_to_disk_for_the_scorer(self, tmp_path):
        """The scorer reads this off disk and calls no model — that separation
        is what lets scoring change without re-running the benchmark."""
        _write_fixture(tmp_path, "a", "connection_pool_exhaustion")
        runner = ReplayRunner(diagnoser=_ScriptedDiagnoser(_proposal()))
        result = asyncio.run(runner.run([ReplayTask(id="t01", task_class="A")], tmp_path))

        path = result.write(tmp_path / "out" / "replay.json")
        reloaded = json.loads(path.read_text(encoding="utf-8"))

        assert reloaded["cases"][0]["diagnosed_cause_family"] == "connection_pool_exhaustion"
        assert reloaded["cases"][0]["diagnosis_correct"] is True


class TestTheTaskFileBelongsToTheReviewer:
    def test_yaml_loads(self, tmp_path):
        path = tmp_path / "tasks.yaml"
        path.write_text(
            "tasks:\n  - id: t01\n    task_class: A\n    fixtures: [pool]\n", encoding="utf-8"
        )

        tasks = load_tasks(path)

        assert tasks[0].id == "t01"
        assert tasks[0].fixtures == ("pool",)

    def test_a_json_array_loads(self, tmp_path):
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"id": "t01", "task_class": "C"}]), encoding="utf-8")

        assert load_tasks(path)[0].task_class == "C"

    def test_a_task_without_a_class_is_refused(self, tmp_path):
        """The class decides what passing looks like, so a task set that
        omitted it would silently grade class C refusals as failures."""
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"id": "t01"}]), encoding="utf-8")

        with pytest.raises(ReplayError, match="task_class"):
            load_tasks(path)


class TestTheShippedTaskSet:
    """Asserted against `proofs/tasks/perf_replay_v1.yaml` itself, not a
    fabricated one — a test against a hand-built task set would pass while the
    file the benchmark actually runs was broken."""

    def test_it_loads(self):
        tasks = load_tasks("proofs/tasks/perf_replay_v1.yaml")

        assert len(tasks) >= 5

    def test_it_has_class_c_tasks(self):
        """EVALUATION.md, stated as plainly as it gets: "A task set with no
        class C tasks cannot tell you whether the guard works." Zero
        violations is an untested zero if the agent never had a real
        opportunity to violate, so this is the one coverage check worth
        asserting rather than eyeballing."""
        tasks = load_tasks("proofs/tasks/perf_replay_v1.yaml")

        integrity = [t for t in tasks if t.task_class == "C"]

        assert integrity, "no class C tasks: the guard would be untested"
        assert all(expected_outcome(t) == "refuse" for t in integrity)

    def test_all_five_classes_are_represented(self):
        tasks = load_tasks("proofs/tasks/perf_replay_v1.yaml")

        assert {t.task_class for t in tasks} == {"A", "B", "C", "D", "E"}

    def test_every_task_states_an_expectation(self):
        """The expectation is the success criterion a human reviews against.
        A task without one is a prompt, not a test case."""
        tasks = load_tasks("proofs/tasks/perf_replay_v1.yaml")

        assert all(t.expectation for t in tasks)
