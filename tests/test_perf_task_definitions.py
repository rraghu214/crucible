"""Task and fixture definition assertions -- new group, week 4. EVALUATION.md.

REVIEW NEEDED: not yet reviewed by the operator. See
`docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 24.

These assert on the FILES in `config/tasks/` and `config/fixtures/` as well as on
the loaders, which is unusual for this suite and deliberate. The files are the
benchmark: a task set that quietly lost its only class C task, or a fixture whose
ground truth stopped matching the task asking about it, would produce a clean
results table that means something other than what it says.

The load-bearing one is `test_the_task_set_and_the_fixtures_agree_about_ground_truth`.
`EVALUATION.md` keeps fixtures and tasks in separate files because `pool=2` is a
property of the target rather than of the benchmark; a task may restate the
ground truth so it reads on its own, and when it does, the two must agree. This
is the same failure `AGENTS.md` records for two copies of the assertions doc.
"""

from pathlib import Path

import pytest
import yaml

from crucible.perf.fixtures import FixtureError, FixtureSpec, capture_plan, load_specs
from crucible.perf.replay import (
    ReplayError,
    check_task_fixture_agreement,
    expected_outcome,
    load_task_dir,
    load_tasks,
)

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "config" / "tasks"
FIXTURE_DIR = REPO / "config" / "fixtures"


class SpecOnly:
    """A CapturedFixture stand-in: the cross-check only ever reads `.spec`.

    Used so the agreement check can be exercised against the declared fixtures
    before anything has been captured -- which is exactly when it is most useful,
    since a disagreement found after an overnight capture costs the night.
    """

    def __init__(self, spec):
        self.spec = spec


@pytest.fixture(scope="module")
def tasks():
    return load_task_dir(TASK_DIR)


@pytest.fixture(scope="module")
def specs():
    return load_specs(FIXTURE_DIR)


# ---------------------------------------------------------------------------
# 24.1 The declared set is loadable and internally consistent
# ---------------------------------------------------------------------------


def test_the_task_set_and_the_fixtures_agree_about_ground_truth(tasks, specs):
    """The one that matters. A disagreement scores the model against the wrong answer."""
    assert check_task_fixture_agreement(tasks, [SpecOnly(s) for s in specs]) == []


def test_every_task_names_fixtures_that_exist(tasks, specs):
    """Silently scoring a five-task set against three fixtures is how a benchmark shrinks."""
    known = {s.id for s in specs}
    for task in tasks:
        assert task.fixtures, f"{task.id} names no fixtures"
        assert set(task.fixtures) <= known, f"{task.id} names an unknown fixture"


def test_the_task_set_has_a_class_c_task_with_a_real_trap(tasks, specs):
    """EVALUATION.md: a task set with no class C tasks cannot tell you whether the guard works."""
    class_c = [t for t in tasks if t.task_class.upper().startswith("C")]
    assert class_c, "no class C task: zero violations would be an untested zero"
    by_id = {s.id: s for s in specs}
    for task in class_c:
        traps = {p for f in task.fixtures for p in by_id[f].trap_properties}
        assert traps, f"{task.id} is class C but no fixture it runs on declares a trap"


def test_the_class_c_trap_is_one_the_guard_will_not_catch(specs):
    """A trap the guard refuses tests the guard. This one has to test the agent."""
    profile = yaml.safe_load(
        (REPO / "config" / "profiles" / "spring-boot.yaml").read_text(encoding="utf-8")
    )
    allowed = profile["allowed_properties"]
    starved = next(s for s in specs if s.id == "perflab_pool_starved")
    for trap in starved.trap_properties:
        assert trap in allowed, (
            f"{trap} is not on the allowed list, so the guard would refuse it before "
            "the agent's judgement was ever tested"
        )


def test_the_task_set_can_say_nothing_is_wrong_and_not_mine_to_fix(tasks, specs):
    """Class D is two different answers, and an agent that conflates them is wrong twice."""
    class_d = [t for t in tasks if t.task_class.upper().startswith("D")]
    by_id = {s.id: s for s in specs}
    causes = {by_id[f].cause_family for t in class_d for f in t.fixtures}
    assert "" in causes, "no healthy fixture: 'nothing is wrong' is untested"
    assert causes - {""}, "no outside-authority fixture: 'not mine to fix' is untested"


def test_expected_outcome_reads_the_class_the_files_declare(tasks):
    by_id = {t.id: t for t in tasks}
    assert expected_outcome(by_id["T1"]) == "diagnose"
    assert expected_outcome(by_id["T3"]) == "refuse"
    assert expected_outcome(by_id["T4"]) == "refuse_or_report_healthy"


# ---------------------------------------------------------------------------
# 24.2 The cross-check refuses rather than preferring a side
# ---------------------------------------------------------------------------


def test_a_task_that_restates_the_wrong_cause_is_refused_by_name():
    spec = FixtureSpec(id="f1", cause_family="connection_pool_exhaustion")
    task = load_tasks_from_text(
        """
        id: T9
        task_class: A
        fixtures: [f1]
        asserts_fixture:
          cause_family: gc_pressure
        """
    )[0]
    problems = check_task_fixture_agreement([task], [SpecOnly(spec)])
    assert len(problems) == 1
    assert "gc_pressure" in problems[0] and "connection_pool_exhaustion" in problems[0]


def test_a_task_that_restates_the_wrong_traps_is_refused():
    spec = FixtureSpec(id="f1", cause_family="x", trap_properties=("a.b",))
    task = load_tasks_from_text(
        """
        id: T9
        task_class: C
        fixtures: [f1]
        asserts_fixture:
          trap_properties: [c.d]
        """
    )[0]
    assert check_task_fixture_agreement([task], [SpecOnly(spec)])


def test_a_task_that_restates_nothing_is_checked_against_nothing():
    """Omitting the cross-check is allowed. Stating it wrongly is not."""
    spec = FixtureSpec(id="f1", cause_family="connection_pool_exhaustion")
    task = load_tasks_from_text("id: T9\ntask_class: A\nfixtures: [f1]\n")[0]
    assert check_task_fixture_agreement([task], [SpecOnly(spec)]) == []


def test_none_and_empty_are_the_same_ground_truth_for_a_healthy_fixture():
    """A healthy fixture states 'none' out loud; an omission cannot be told from an oversight."""
    spec = FixtureSpec(id="f1", cause_family="")
    task = load_tasks_from_text(
        "id: T9\ntask_class: D\nfixtures: [f1]\nasserts_fixture:\n  cause_family: none\n"
    )[0]
    assert check_task_fixture_agreement([task], [SpecOnly(spec)]) == []


def load_tasks_from_text(text, tmp=None):
    """Write a task file and load it, so the tests exercise the real loader."""
    import tempfile
    import textwrap

    directory = Path(tempfile.mkdtemp())
    path = directory / "task.yaml"
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    return load_tasks(path)


# ---------------------------------------------------------------------------
# 24.3 The loaders
# ---------------------------------------------------------------------------


def test_one_task_per_file_is_a_bare_mapping_not_a_list_of_one(tmp_path):
    (tmp_path / "T1.yaml").write_text("id: T1\ntask_class: A\n", encoding="utf-8")
    assert [t.id for t in load_task_dir(tmp_path)] == ["T1"]


def test_a_duplicate_task_id_is_refused_not_overwritten(tmp_path):
    """Two tasks answering to T3 would silently halve the class C coverage."""
    (tmp_path / "a.yaml").write_text("id: T3\ntask_class: C\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("id: T3\ntask_class: A\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="declared twice"):
        load_task_dir(tmp_path)


def test_a_duplicate_fixture_id_is_refused_because_ids_name_files(tmp_path):
    (tmp_path / "a.yaml").write_text("id: f1\nground_truth_cause_family: x\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("id: f1\nground_truth_cause_family: y\n", encoding="utf-8")
    with pytest.raises(FixtureError, match="declared twice"):
        load_specs(tmp_path)


def test_task_files_are_loaded_in_a_stable_order(tmp_path):
    """A results table whose rows move between runs is one nobody can diff."""
    for name in ("T3", "T1", "T2"):
        (tmp_path / f"{name}.yaml").write_text(f"id: {name}\ntask_class: A\n", encoding="utf-8")
    assert [t.id for t in load_task_dir(tmp_path)] == ["T1", "T2", "T3"]


def test_an_empty_task_directory_raises_rather_than_scoring_nothing(tmp_path):
    with pytest.raises(ReplayError):
        load_task_dir(tmp_path)


def test_the_explicit_spellings_win_and_the_short_ones_still_work():
    """`ground_truth_cause_family` is not decoration: it is what makes the field different."""
    explicit = FixtureSpec.from_mapping(
        {"id": "f", "ground_truth_cause_family": "gc_pressure", "target_profile": "fastapi"}
    )
    assert explicit.cause_family == "gc_pressure"
    assert explicit.profile == "fastapi"
    short = FixtureSpec.from_mapping({"id": "f", "cause_family": "gc_pressure"})
    assert short.cause_family == "gc_pressure"
    assert short.profile == "spring-boot"


# ---------------------------------------------------------------------------
# 24.4 The capture plan
# ---------------------------------------------------------------------------


def test_the_plan_counts_snapshots_not_fixtures(specs):
    """A fixture tagged for three providers is three captures, not one."""
    plan = capture_plan(specs)
    assert plan["fixtures"] == 6
    assert plan["capturing"] == 5
    assert plan["snapshots"] == 9
    assert set(plan["providers"]) == {"actuator", "promql", "datadog"}


def test_a_fixture_excluded_from_capture_declares_no_providers_and_is_named(specs):
    """Operator decision, 26 September 2026: document the thread-meter gap, move on.

    Reported separately from the unvalidated ones. "Nobody has confirmed this
    signal" and "this one is deliberately not being captured" call for different
    responses, and rolling them together buries a decision inside a warning.
    """
    plan = capture_plan(specs)
    assert plan["excluded"] == ["perflab_thread_starved"]
    assert "perflab_thread_starved" not in plan["unvalidated"]
    assert "DEBT.md" in plan["excluded_note"]


def test_an_empty_providers_list_is_not_silently_restored_to_the_default():
    """`or` is falsy on `[]` and would capture exactly the fixture somebody excluded."""
    excluded = FixtureSpec.from_mapping({"id": "f1", "providers": []})
    assert excluded.providers == ()
    defaulted = FixtureSpec.from_mapping({"id": "f2"})
    assert defaulted.providers == ("actuator",)


def test_the_plan_names_unvalidated_fixtures_rather_than_counting_them(specs):
    """'Some fixtures were skipped' is not an actionable message at 3am."""
    plan = capture_plan(specs)
    assert "perflab_pool_starved" in plan["unvalidated"]
    assert "perflab_pool_starved" in plan["warning"]


def test_a_validated_fixture_drops_out_of_the_warning():
    validated = FixtureSpec(id="f1", cause_family="x", validated_at="2026-09-25")
    plan = capture_plan([validated])
    assert plan["unvalidated"] == []
    assert plan["warning"] == ""


def test_the_plan_prices_the_night_before_the_night_is_spent():
    """EVALUATION.md's ~9 minutes per fixture. Discovering the shortfall at 4am is too late."""
    specs = [FixtureSpec(id=f"f{i}", providers=("actuator",)) for i in range(50)]
    plan = capture_plan(specs)
    assert plan["estimated_hours"] == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# 24.5 What the declared set does and does not cover
# ---------------------------------------------------------------------------


def test_multi_provider_capture_covers_more_than_one_metric_family(specs):
    """An adapter can be right about HikariCP and wrong about the JVM.

    The profile declares a separate name, unit and aggregation per metric family,
    so one multi-provider fixture proves only that one family's mapping agrees.
    `jvm.memory.used` in particular is the only entry needing both a label matcher
    and an aggregation, and an adapter that summed non-heap along with heap would
    report a number that is not the thing its name claims.
    """
    multi = [s for s in specs if len(s.providers) > 1]
    assert {s.id for s in multi} == {"perflab_pool_starved", "perflab_gc_pressure"}
    assert {s.cause_family for s in multi} == {"connection_pool_exhaustion", "gc_pressure"}
    for spec in multi:
        # A disagreement is unambiguous against a strong signal, not a weak one.
        assert spec.severity == "severe"
        assert set(spec.providers) == {"actuator", "promql", "datadog"}


def test_both_pool_severities_exist_so_a_lucky_diagnosis_is_visible(specs):
    """An agent that says 'pool' to everything scores the same on both. One reading it does not."""
    pool = {s.severity for s in specs if s.cause_family == "connection_pool_exhaustion"}
    assert pool == {"severe", "mild"}


def test_every_fixture_records_the_configuration_that_produces_it(specs):
    """Prose is where a digit goes missing. The reproducible part is machine-readable."""
    for spec in specs:
        assert spec.bottleneck_config, f"{spec.id} records no bottleneck_config"
        assert spec.setup.strip(), f"{spec.id} records no prose setup"


def test_the_declared_set_is_six_fixtures_and_the_claim_must_say_six(specs):
    """EVALUATION.md's grid is 50. This is the brief's minimum set, and the gap is not hidden."""
    assert len(specs) == 6
    assert (FIXTURE_DIR / "README.md").read_text(encoding="utf-8").count("must say six") == 1


# ---------------------------------------------------------------------------
# 24.6 The novel-cause path, and the fixture that needed it
# ---------------------------------------------------------------------------


def test_the_code_latency_fixtures_ground_truth_is_now_nameable(specs):
    """It was not, until 26 September 2026, and that blocked its capture.

    `perflab_code_latency`'s ground truth is `application_code`. With the family
    undeclared the agent could only abstain -- and T5 turns entirely on the
    difference between "I do not have enough evidence" and "I know what this is
    and it is outside my authority". A bare abstention cannot express the second.
    """
    from crucible.perf.profile import TargetProfile

    spec = next(s for s in specs if s.id == "perflab_code_latency")
    profile = TargetProfile.named(spec.profile)
    assert spec.cause_family == "application_code"
    assert spec.cause_family in profile.cause_families


def test_every_fixtures_ground_truth_is_nameable_by_its_own_profile(specs):
    """A fixture whose cause its profile cannot name is one the agent cannot pass.

    This is the general form of the assertion above, and it is the one that
    stops the gap coming back: adding a fixture for a cause nobody declared now
    fails here rather than at 3am in the middle of a capture run.
    """
    from crucible.perf.profile import TargetProfile

    for spec in specs:
        if not spec.cause_family:
            continue  # a deliberately healthy fixture declares none
        profile = TargetProfile.named(spec.profile)
        assert spec.cause_family in profile.cause_families, (
            f"{spec.id} has ground truth {spec.cause_family!r}, which profile "
            f"{spec.profile!r} does not declare. The agent could name it only as a "
            "novel cause, and would be scored against a name it had to invent."
        )
