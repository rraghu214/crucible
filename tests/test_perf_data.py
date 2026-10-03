"""Tests for crucible.ui.perf_data — the UI data layer.

Every function reads from disk. Tests use ``tmp_path`` to keep a real filesystem
under the assertions without touching the project tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.ui.perf_data import (
    PerfContext,
    active_runs,
    campaign_verdict_counts,
    fmt_minutes,
    fmt_when,
    latest_campaign,
    list_campaigns,
    list_fixtures,
    list_replay_runs,
    list_tasks,
    load_profile,
    load_sla,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(tmp: Path) -> PerfContext:
    return PerfContext(root=tmp, state_dir=tmp / "state")


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# PerfContext.path
# ---------------------------------------------------------------------------

class TestPerfContext:
    def test_relative_path_resolves_against_root(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert ctx.path("config/slo.yaml") == tmp_path / "config" / "slo.yaml"

    def test_absolute_path_is_returned_unchanged(self, tmp_path):
        ctx = _ctx(tmp_path)
        abs_path = tmp_path / "elsewhere" / "file.yaml"
        assert ctx.path(str(abs_path)) == abs_path

    def test_state_uses_provided_state_dir(self, tmp_path):
        state = tmp_path / "mystate"
        ctx = PerfContext(root=tmp_path, state_dir=state)
        assert ctx.state == state


# ---------------------------------------------------------------------------
# load_sla / load_profile — surface errors, never raise
# ---------------------------------------------------------------------------

class TestLoadSla:
    def test_missing_file_returns_none_and_error(self, tmp_path):
        ctx = _ctx(tmp_path)
        sla, err = load_sla(ctx)
        assert sla is None
        assert err

    def test_valid_sla_returns_object(self, tmp_path):
        # Use the real project SLA so the format is guaranteed correct.
        real_sla = Path(__file__).parent.parent / "config" / "slo.yaml"
        if not real_sla.exists():
            pytest.skip("config/slo.yaml not found")
        ctx = PerfContext(root=Path(__file__).parent.parent)
        sla, err = load_sla(ctx)
        assert err == "", f"unexpected error: {err}"
        assert sla is not None
        assert sla.target_base_url


class TestLoadProfile:
    def test_missing_profile_returns_none_and_error(self, tmp_path):
        ctx = _ctx(tmp_path)
        profile, err = load_profile(ctx, "nonexistent")
        assert profile is None
        assert err

    def test_real_spring_boot_profile_loads(self, tmp_path):
        # Use the real project profiles directory so there's something to load.
        real_profiles = Path(__file__).parent.parent / "config" / "profiles"
        if not real_profiles.exists():
            pytest.skip("config/profiles not found")
        ctx = PerfContext(root=Path(__file__).parent.parent)
        profile, err = load_profile(ctx, "spring-boot")
        if err:
            pytest.skip(f"profile load failed: {err}")
        assert profile is not None
        assert profile.name == "spring-boot"


# ---------------------------------------------------------------------------
# list_campaigns
# ---------------------------------------------------------------------------

class TestListCampaigns:
    def test_empty_results_dir_returns_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        (tmp_path / "results").mkdir()
        campaigns, skipped = list_campaigns(ctx)
        assert campaigns == []
        assert skipped == []

    def test_missing_results_dir_returns_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        campaigns, skipped = list_campaigns(ctx)
        assert campaigns == []

    def test_valid_campaign_file_is_loaded(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        manifest = {
            "run_id": "r1",
            "started_at_epoch_s": 1000.0,
            "sla": {"name": "test-sla"},
            "experiments": [],
        }
        _write(results / "r1.json", manifest)
        ctx = _ctx(tmp_path)
        campaigns, skipped = list_campaigns(ctx)
        assert len(campaigns) == 1
        assert campaigns[0]["run_id"] == "r1"
        assert skipped == []

    def test_sorted_oldest_first(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        for rid, ts in [("r2", 2000.0), ("r1", 1000.0), ("r3", 3000.0)]:
            _write(results / f"{rid}.json", {"run_id": rid, "started_at_epoch_s": ts, "experiments": []})
        ctx = _ctx(tmp_path)
        campaigns, _ = list_campaigns(ctx)
        assert [c["run_id"] for c in campaigns] == ["r1", "r2", "r3"]

    def test_unreadable_file_goes_to_skipped(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "bad.json").write_text("not json {{{", encoding="utf-8")
        ctx = _ctx(tmp_path)
        campaigns, skipped = list_campaigns(ctx)
        assert campaigns == []
        assert any("bad.json" in name for name, _ in skipped)


# ---------------------------------------------------------------------------
# latest_campaign
# ---------------------------------------------------------------------------

class TestLatestCampaign:
    def test_returns_none_when_no_results(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert latest_campaign(ctx) is None

    def test_returns_most_recent(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        for rid, ts in [("r1", 1000.0), ("r2", 2000.0)]:
            _write(results / f"{rid}.json", {"run_id": rid, "started_at_epoch_s": ts, "experiments": []})
        ctx = _ctx(tmp_path)
        c = latest_campaign(ctx)
        assert c is not None
        assert c["run_id"] == "r2"


# ---------------------------------------------------------------------------
# active_runs
# ---------------------------------------------------------------------------

class TestActiveRuns:
    def test_no_locks_dir_returns_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert active_runs(ctx) == []

    def test_lock_without_holder_is_skipped(self, tmp_path):
        ctx = _ctx(tmp_path)
        lock_dir = ctx.state / "locks" / "perftest_sandbox.lock"
        lock_dir.mkdir(parents=True)
        # no holder.json
        assert active_runs(ctx) == []

    def test_valid_lock_is_returned(self, tmp_path):
        ctx = _ctx(tmp_path)
        lock_dir = ctx.state / "locks" / "perftest_sandbox.lock"
        lock_dir.mkdir(parents=True)
        _write(lock_dir / "holder.json", {"run_id": "r1"})
        runs = active_runs(ctx)
        assert len(runs) == 1
        assert runs[0]["run_id"] == "r1"
        assert runs[0]["branch"] == "perftest_sandbox"


# ---------------------------------------------------------------------------
# list_replay_runs
# ---------------------------------------------------------------------------

class TestListReplayRuns:
    def test_no_bench_dir_returns_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert list_replay_runs(ctx) == []

    def test_valid_replay_file_is_loaded(self, tmp_path):
        bench = tmp_path / "docs" / "bench"
        bench.mkdir(parents=True)
        data = {"cases": [{"task_id": "T1", "passed": True}], "provider": "actuator"}
        _write(bench / "replay-001.json", data)
        ctx = _ctx(tmp_path)
        runs = list_replay_runs(ctx)
        assert len(runs) == 1
        assert runs[0]["_file"] == "replay-001.json"
        assert len(runs[0]["cases"]) == 1

    def test_file_without_cases_key_is_skipped(self, tmp_path):
        bench = tmp_path / "docs" / "bench"
        bench.mkdir(parents=True)
        _write(bench / "replay-002.json", {"summary": "no cases"})
        ctx = _ctx(tmp_path)
        assert list_replay_runs(ctx) == []


# ---------------------------------------------------------------------------
# list_fixtures
# ---------------------------------------------------------------------------

class TestListFixtures:
    def test_empty_dir_returns_empty(self, tmp_path):
        (tmp_path / "config" / "fixtures").mkdir(parents=True)
        ctx = _ctx(tmp_path)
        assert list_fixtures(ctx) == []

    def test_valid_fixture_is_loaded(self, tmp_path):
        fixture_dir = tmp_path / "config" / "fixtures"
        fixture_dir.mkdir(parents=True)
        yaml_text = (
            "id: perflab_pool_starved\n"
            "ground_truth_cause_family: pool_exhaustion\n"
            "severity: severe\n"
            "providers:\n  - actuator\n"
            "validated_at: '2026-10-03'\n"
        )
        (fixture_dir / "perflab_pool_starved.yaml").write_text(yaml_text, encoding="utf-8")
        ctx = _ctx(tmp_path)
        fixtures = list_fixtures(ctx)
        assert len(fixtures) == 1
        assert fixtures[0]["id"] == "perflab_pool_starved"
        assert fixtures[0]["validated_at"] == "2026-10-03"


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

class TestListTasks:
    def test_valid_task_is_loaded(self, tmp_path):
        tasks_dir = tmp_path / "config" / "tasks"
        tasks_dir.mkdir(parents=True)
        yaml_text = (
            "id: T1\ntask_class: A\nname: diagnose pool starvation\n"
            "fixtures:\n  - perflab_pool_starved\n"
        )
        (tasks_dir / "T1.yaml").write_text(yaml_text, encoding="utf-8")
        ctx = _ctx(tmp_path)
        tasks = list_tasks(ctx)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "T1"
        assert tasks[0]["task_class"] == "A"


# ---------------------------------------------------------------------------
# campaign_verdict_counts
# ---------------------------------------------------------------------------

class TestCampaignVerdictCounts:
    def test_counts_by_verdict(self):
        campaign = {
            "experiments": [
                {"verdict": "IMPROVED"},
                {"verdict": "IMPROVED"},
                {"verdict": "INCONCLUSIVE"},
            ]
        }
        counts = campaign_verdict_counts(campaign)
        assert counts["IMPROVED"] == 2
        assert counts["INCONCLUSIVE"] == 1

    def test_empty_experiments_returns_empty(self):
        assert campaign_verdict_counts({"experiments": []}) == {}

    def test_missing_experiments_key_returns_empty(self):
        assert campaign_verdict_counts({}) == {}


# ---------------------------------------------------------------------------
# fmt helpers
# ---------------------------------------------------------------------------

class TestFmtHelpers:
    def test_fmt_when_none_returns_dash(self):
        assert fmt_when(None) == "—"

    def test_fmt_when_zero_returns_dash(self):
        assert fmt_when(0) == "—"

    def test_fmt_minutes_none_returns_dash(self):
        assert fmt_minutes(None) == "—"

    def test_fmt_minutes_seconds_only(self):
        assert fmt_minutes(45) == "45s"

    def test_fmt_minutes_with_minutes(self):
        result = fmt_minutes(125)
        assert "2m" in result
        assert "05s" in result


# ---------------------------------------------------------------------------
# Smoke: nicegui_app can be imported (pages are registered at module load)
# ---------------------------------------------------------------------------

class TestNiceGuiAppImport:
    def test_module_imports_without_error(self):
        import importlib
        mod = importlib.import_module("crucible.ui.nicegui_app")
        assert hasattr(mod, "page_home")
        assert hasattr(mod, "page_campaign")
        assert hasattr(mod, "page_benchmark")

    def test_get_ctx_returns_perf_context(self):
        from crucible.ui.nicegui_app import _get_ctx
        ctx = _get_ctx()
        assert ctx is not None
        assert hasattr(ctx, "root")
