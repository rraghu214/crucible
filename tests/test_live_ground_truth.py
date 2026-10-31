"""Live runs record their true cause; GC gets a real lever. Group 46.

DRAFTED by Claude Code, 1 November 2026, while the operator was away. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 46 for the questions to check.

Operator decisions, 1 November 2026: live runs must record the true cause as a
matter of course ("shouldn't it just be an inherent behaviour?"), and gc_pressure
gets a real lever instead of an abstention. What is asserted:

1. a run started from a fixture copies the fixture's cause onto the manifest; an
   unknown fixture refuses; a run against a real service records none;
2. the recorded cause never reaches the model (the journal is the one path from
   old manifests into a prompt);
3. the scorer and the Benchmarks quadrant score live runs from that record;
4. the heap is an allowed, bounded property, read by Box A's deploy hook from
   the file the agent edits, and the GC fixtures and tasks use it;
5. each live benchmark SLA is protected like config/slo.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.perf.applicator import path_is_protected
from crucible.perf.campaign import CampaignResult
from crucible.perf.commands import fixture_ground_truth
from crucible.perf.profile import TargetProfile

REPO = Path(__file__).resolve().parents[1]
POOL = "spring.datasource.hikari.maximum-pool-size"
HEAP = "perflab.jvm.heap-max-mb"


def _manifest(run_id: str, *, named: str, verdict: str, kept: bool, truth: str | None) -> dict:
    return {
        "run_id": run_id, "profile": "spring-boot", "scenario": "db-latency", "started_at_epoch_s": 1.0,
        "collector_version": "1.2.0",
        "baseline": {"load": {"p99_ms": 1200.0}, "sla_met": False},
        "ground_truth": {"fixture": "perflab_x", "cause_family": truth} if truth is not None else {},
        "experiments": [{
            "experiment": 1, "cause_family": named, "verdict": verdict, "kept": kept,
            "verdict_reason": "measured", "collector_version": "1.2.0", "started_at_epoch_s": 1.0,
            "proposal": {"cause_family": named, "changes": [{"prop": POOL, "value": 20}]},
        }],
    }


# -- 46.1 ---------------------------------------------------------------------


class TestTheRunRecordsItsTrueCause:
    def test_it_is_copied_from_the_fixture_file(self):
        truth = fixture_ground_truth("perflab_gc_pressure", REPO / "config" / "fixtures")
        assert truth["cause_family"] == "gc_pressure"
        assert truth["fixture"] == "perflab_gc_pressure"
        assert truth["source"].endswith("perflab_gc_pressure.yaml")

    def test_a_healthy_fixture_records_no_cause(self):
        assert fixture_ground_truth("perflab_healthy", REPO / "config" / "fixtures")["cause_family"] is None

    def test_an_unknown_fixture_refuses_rather_than_guessing(self):
        with pytest.raises(ValueError, match="no fixture"):
            fixture_ground_truth("perflab_no_such_state", REPO / "config" / "fixtures")

    def test_it_is_on_the_manifest_and_empty_by_default(self):
        assert CampaignResult(run_id="r").as_dict()["ground_truth"] == {}
        recorded = {"fixture": "perflab_pool_starved", "cause_family": "connection_pool_exhaustion"}
        assert CampaignResult(run_id="r", ground_truth=recorded).as_dict()["ground_truth"] == recorded

    def test_the_cli_takes_the_fixture(self):
        from crucible.cli import build_parser

        args = build_parser().parse_args(["run", "--fixture", "perflab_pool_starved"])
        assert args.fixture == "perflab_pool_starved"


# -- 46.2 ---------------------------------------------------------------------


def test_the_recorded_cause_never_reaches_a_prompt(tmp_path):
    """The journal turns old manifests into prompt text; the answer must not ride along."""
    from crucible.perf.journal import JournalIndex, render_for_prompt

    manifest = _manifest("r1", named="connection_pool_exhaustion", verdict="IMPROVED", kept=True,
                         truth="SECRET_TRUE_CAUSE")
    (tmp_path / "r1.json").write_text(json.dumps(manifest), encoding="utf-8")
    findings = JournalIndex.load(tmp_path).prior_findings()
    assert findings, "the experiment should become a finding"
    rendered = render_for_prompt(findings) + json.dumps([f.as_dict() for f in findings])
    assert "SECRET_TRUE_CAUSE" not in rendered


# -- 46.3 ---------------------------------------------------------------------


class TestQuadrantsAreScoredFromTheRecord:
    @pytest.mark.parametrize("named,verdict,kept,expected", [
        ("connection_pool_exhaustion", "IMPROVED", True, "CORRECT"),
        ("gc_pressure", "IMPROVED", True, "LUCKY"),
        ("connection_pool_exhaustion", "INCONCLUSIVE", False, "UNLUCKY"),
        ("gc_pressure", "WORSE", False, "WRONG"),
    ])
    def test_the_scorer_reads_the_manifest(self, tmp_path, named, verdict, kept, expected):
        from crucible.perf.scorer import score_journal

        (tmp_path / "r.json").write_text(json.dumps(_manifest(
            "r", named=named, verdict=verdict, kept=kept, truth="connection_pool_exhaustion")), encoding="utf-8")
        (score,) = score_journal(tmp_path).scores
        assert score.diagnosis == (expected,)

    def test_a_run_with_no_record_stays_unscorable(self, tmp_path):
        from crucible.perf.scorer import score_journal

        (tmp_path / "r.json").write_text(json.dumps(_manifest(
            "r", named="connection_pool_exhaustion", verdict="IMPROVED", kept=True, truth=None)), encoding="utf-8")
        assert score_journal(tmp_path).scores[0].diagnosis == ("UNSCORABLE",)

    def test_the_benchmarks_page_counts_them(self, tmp_path):
        from crucible.ui.perf_ui import PerfContext, build_screen

        results = tmp_path / "results"
        results.mkdir()
        for run_id, named in (("a", "connection_pool_exhaustion"), ("b", "gc_pressure")):
            (results / f"{run_id}.json").write_text(json.dumps(_manifest(
                run_id, named=named, verdict="IMPROVED", kept=True, truth="connection_pool_exhaustion")),
                encoding="utf-8")
        ctx = PerfContext(root=REPO, results_dir=str(results), state_dir=tmp_path / "state")
        shown = json.dumps(build_screen(18, ctx)["surface"]["dataModel"])
        assert "CORRECT: 1 -- a exp 1" in shown
        assert "LUCKY: 1 -- b exp 1" in shown
        assert "UNLUCKY: 0" in shown and "WRONG: 0" in shown
        assert "2 live run(s) started from a fixture are scored here" in shown


# -- 46.4 ---------------------------------------------------------------------


class TestTheHeapIsARealLever:
    def test_it_is_allowed_and_bounded(self):
        bounds = TargetProfile.named("spring-boot").allowed_properties[HEAP]
        assert (bounds.kind, bounds.minimum, bounds.maximum) == ("int", 128, 1536)
        assert bounds.violation(4096) and bounds.violation(64)
        assert not bounds.violation(512)

    def test_the_deploy_hook_reads_it_from_the_file_the_agent_edits(self):
        hook = (REPO / "scripts" / "box_a_post_receive.sh").read_text(encoding="utf-8")
        assert "perflab\\.jvm\\.heap-max-mb" in hook and "src/main/resources/application.properties" in hook
        # After PERFLAB_JAVA_OPTS: HotSpot honours the last -Xmx, so the committed value wins.
        assert "java ${PERFLAB_JAVA_OPTS:-} ${HEAP_OPT} -jar" in hook

    def test_the_gc_fixtures_set_their_state_through_it(self):
        from crucible.perf.fixtures import load_specs

        specs = {s.id: s for s in load_specs(REPO / "config" / "fixtures")}
        for fixture in ("perflab_gc_pressure", "perflab_gc_pressure_moderate", "perflab_gc_and_pool"):
            assert HEAP in specs[fixture].bottleneck_config, fixture
            assert "jvm.Xmx" not in specs[fixture].bottleneck_config, fixture

    def test_the_pool_is_now_the_judgement_trap_on_the_gc_fixture(self):
        """The right fix is reachable, so the wrong one is judgement, not the guard."""
        from crucible.perf.fixtures import load_specs

        spec = next(s for s in load_specs(REPO / "config" / "fixtures") if s.id == "perflab_gc_pressure")
        assert POOL in spec.trap_properties

    def test_the_skill_names_the_lever(self):
        assert HEAP in (REPO / "config" / "profiles" / "spring-boot.SKILL.md").read_text(encoding="utf-8")


# -- 46.5 ---------------------------------------------------------------------


@pytest.mark.parametrize("sla", sorted((REPO / "config" / "bench").glob("slo-*.yaml")), ids=lambda p: p.name)
def test_every_live_benchmark_sla_loads_and_is_protected(sla):
    from crucible.perf.campaign import Sla

    loaded = Sla.load(sla)
    assert loaded.environment_kind != "production"
    assert path_is_protected(f"config/bench/{sla.name}", TargetProfile.named("spring-boot").protected_paths)
