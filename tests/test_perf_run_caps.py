"""Run caps: a cost cap and a wall-clock cap. Group 36.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 36 for the questions to check.

Operator decisions, 24 October 2026:
- a cap is checked at each experiment boundary, and an experiment whose
  estimated cost would cross the cost cap is not started (36.1.1);
- reaching a cap PAUSES the campaign in every mode; the operator resumes with a
  higher cap, or aborts, which finishes the run so the report can be made
  (36.1.2);
- paused time never counts toward the wall clock, and a cap that cannot fit the
  scenarios is called out before anything runs: a warning, and a "start anyway"
  confirmation at Launch (36.2, option a).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from crucible.perf import commands
from crucible.perf.campaign import CampaignResult, Sla, paused_state_path
from crucible.perf.profile import TargetProfile
from crucible.ui import actions as A
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def campaign_parts(tmp_path):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    profile = TargetProfile.from_mapping({
        "name": "spring-boot", "runtime": "jvm", "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {"spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100}},
        "protected_paths": ["config/slo.yaml"], "deploy": {"branch": "perftest_sandbox"},
    })
    return profile, Sla.load(sla_path), workspace


class TestTheCostCap:
    def test_reaching_it_pauses_the_campaign(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 5, max_experiments=5)
        campaign.cost_cap_usd = 0.002
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        result = asyncio.run(campaign.run())
        assert len(result.experiments) == 2
        assert result.paused and not result.aborted
        assert result.cap_hit["cap"] == "cost" and result.cap_hit["before_experiment"] == 3
        assert "cost cap reached before experiment 3" in result.stopped_reason
        assert "resume" in result.stopped_reason and "abort" in result.stopped_reason

    def test_an_experiment_that_would_cross_it_is_not_started(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 5, max_experiments=5)
        campaign.cost_cap_usd = 0.0025
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        campaign.typical_call_cost_usd = 0.001
        result = asyncio.run(campaign.run())
        # $0.002 spent after two; plus ~$0.001 for a third would pass $0.0025,
        # although $0.002 alone has not reached it.
        assert len(result.experiments) == 2
        assert result.cap_hit["next_estimate_usd"] == 0.001
        assert "would be crossed by experiment 3" in result.stopped_reason

    def test_without_history_there_is_no_estimate(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 3, max_experiments=3)
        campaign.cost_cap_usd = 0.0025
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        result = asyncio.run(campaign.run())
        assert len(result.experiments) == 3 and result.cap_hit == {}

    def test_resuming_with_a_higher_cap_continues(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 5, max_experiments=3)
        campaign.cost_cap_usd = 0.002
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        assert asyncio.run(campaign.run()).paused
        campaign.cost_cap_usd = 1.0
        resumed = asyncio.run(campaign.resume())
        assert not resumed.paused and len(resumed.experiments) == 3

    def test_resuming_without_a_new_cap_keeps_the_old_one(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 5, max_experiments=4)
        campaign.cost_cap_usd = 0.002
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        asyncio.run(campaign.run())
        campaign.cost_cap_usd = None
        resumed = asyncio.run(campaign.resume())
        assert resumed.paused and campaign.cost_cap_usd == 0.002


class TestTheWallClockCap:
    def test_reaching_it_pauses_before_the_next_experiment(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0, 299.0], max_experiments=3)
        campaign.wall_clock_cap_s = 1e-6
        result = asyncio.run(campaign.run())
        assert result.paused and result.experiments == [] and result.cap_hit["cap"] == "wall_clock"

    def test_time_spent_paused_does_not_count(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0])
        campaign.wall_clock_cap_s = 3600
        two_hours_ago = time.time() - 7200
        paused_most = CampaignResult(run_id="r", started_at_epoch_s=two_hours_ago, paused_seconds=7000)
        never_paused = CampaignResult(run_id="r", started_at_epoch_s=two_hours_ago)
        assert campaign._cap_reached(paused_most, 4) == ""
        assert "wall-clock cap reached" in campaign._cap_reached(never_paused, 4)


class TestAbortFinishesAPausedRun:
    def test_the_manifest_is_finished_as_aborted_and_the_pause_cleared(self, campaign_parts, tmp_path):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 3, max_experiments=3)
        campaign.cost_cap_usd = 0.001
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        asyncio.run(campaign.run())
        assert paused_state_path(campaign.state_dir, campaign.run_id).exists()

        assert commands._abort_paused(campaign.state_dir, campaign.run_id, "out of budget",
                                      results_dir=campaign.results_dir) == commands.OK
        manifest = json.loads((campaign.results_dir / f"{campaign.run_id}.json").read_text(encoding="utf-8"))
        assert manifest["aborted"] and not manifest["paused"]
        assert "aborted while paused" in manifest["stopped_reason"] and manifest["finished_at_epoch_s"]
        assert not paused_state_path(campaign.state_dir, campaign.run_id).exists()


class TestACapThatCannotFitIsCalledOut:
    def test_four_half_hour_scenarios_do_not_fit_one_and_a_half_hours(self):
        windows = [(120, 1680)] * 4
        warning = commands.wall_clock_warning(1.5, windows)
        assert "cannot fit" in warning and "2.00 h" in warning
        assert commands.wall_clock_warning(2.5, windows) == ""
        assert commands.wall_clock_warning(None, windows) == ""

    def test_the_cli_warns_but_does_not_refuse(self, monkeypatch, capsys):
        monkeypatch.setattr(commands, "build_campaign", lambda **kw: (_ for _ in ()).throw(ValueError("stop here")))
        commands.cmd_run(state_dir="x", run_id="r", warmup_s=120.0, measure_s=1680.0, wall_clock_cap_h=0.25)
        assert "WARNING: the wall-clock cap of 0.25 h cannot fit" in capsys.readouterr().out


def test_the_caps_in_force_are_on_the_manifest(campaign_parts):
    campaign = _campaign(*campaign_parts, p99s=[300.0, 58.0])
    campaign.cost_cap_usd, campaign.wall_clock_cap_s = 0.5, 7200
    campaign.cost_so_far = lambda result: 0.0
    manifest = asyncio.run(campaign.run()).as_dict()
    assert manifest["caps"] == {"cost_usd": 0.5, "wall_clock_s": 7200}
    assert manifest["cap_hit"] == {}


class TestPlumbing:
    def test_build_campaign_converts_hours_and_refuses_non_positive_caps(self, tmp_path, monkeypatch):
        monkeypatch.chdir(REPO)
        (tmp_path / "state").mkdir()
        kwargs = dict(profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5,
                      warmup_s=1, measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state",
                      workspace="", approve_mode="file", approval_timeout_s=1, model="m", provider="p")
        campaign = commands.build_campaign(**kwargs, cost_cap_usd=0.25, wall_clock_cap_h=2)
        assert (campaign.cost_cap_usd, campaign.wall_clock_cap_s) == (0.25, 7200)
        with pytest.raises(ValueError, match="cost cap"):
            commands.build_campaign(**kwargs, cost_cap_usd=0)

    def test_the_action_builder_passes_each_cap_as_one_token(self):
        args = A.run_args(cost_cap=0.25, wall_clock_cap=2)
        assert "--cost-cap=0.25" in args and "--wall-clock-cap=2.0" in args

    def test_the_cli_accepts_caps_on_run_and_resume(self, monkeypatch):
        from crucible import cli

        seen = {}
        monkeypatch.setattr(commands, "cmd_run", lambda **kw: seen.update(kw) or 0)
        monkeypatch.setattr("sys.argv", ["crucible", "run", "--cost-cap=0.1", "--wall-clock-cap=8"])
        cli.main()
        assert (seen["cost_cap_usd"], seen["wall_clock_cap_h"]) == (0.1, 8.0)
        monkeypatch.setattr(commands, "cmd_resume", lambda run_id, **kw: seen.update(resume=kw) or 0)
        monkeypatch.setattr("sys.argv", ["crucible", "resume", "r1", "--cost-cap=0.5"])
        cli.main()
        assert seen["resume"]["cost_cap_usd"] == 0.5
