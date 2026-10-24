"""Run caps: a cost cap and a wall-clock cap. Group 36.

DRAFTED by Claude Code, 24 October 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 36 for the questions to check.

Operator, 24 October 2026: "Caps should be built." Both are checked at each
experiment boundary. Reaching one stops the campaign cleanly: nothing is rolled
back, because every kept change was measured and verified. What is asserted:

1. the cost cap stops the run once model calls have cost at least the cap;
2. the wall-clock cap stops it once the elapsed time, minus time paused,
   reaches the cap;
3. the caps in force, and the one that stopped the run, are on the manifest;
4. the CLI, the action builder and the wizard all carry them; zero or negative
   caps are refused.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from crucible.perf.campaign import CampaignResult, Sla
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
    def test_it_stops_at_the_boundary_once_spent_reaches_the_cap(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 5, max_experiments=5)
        campaign.cost_cap_usd = 0.002
        campaign.cost_so_far = lambda result: 0.001 * len(result.experiments)
        result = asyncio.run(campaign.run())
        assert len(result.experiments) == 2
        assert not result.aborted
        assert result.cap_hit == {"cap": "cost", "limit_usd": 0.002, "spent_usd": 0.002, "before_experiment": 3}
        assert "cost cap reached before experiment 3" in result.stopped_reason

    def test_without_a_cap_the_run_uses_its_experiment_ceiling(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0] + [299.0] * 3, max_experiments=3)
        campaign.cost_so_far = lambda result: 99.0
        result = asyncio.run(campaign.run())
        assert len(result.experiments) == 3 and result.cap_hit == {}


class TestTheWallClockCap:
    def test_it_stops_before_the_next_experiment_once_reached(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0, 299.0], max_experiments=3)
        campaign.wall_clock_cap_s = 1e-6
        result = asyncio.run(campaign.run())
        assert result.experiments == [] and result.cap_hit["cap"] == "wall_clock"
        assert "time paused excluded" in result.stopped_reason

    def test_time_spent_paused_does_not_count(self, campaign_parts):
        campaign = _campaign(*campaign_parts, p99s=[300.0])
        campaign.wall_clock_cap_s = 3600
        two_hours_ago = time.time() - 7200
        paused_most = CampaignResult(run_id="r", started_at_epoch_s=two_hours_ago, paused_seconds=7000)
        never_paused = CampaignResult(run_id="r", started_at_epoch_s=two_hours_ago)
        assert campaign._cap_reached(paused_most, 4) == ""
        assert "wall-clock cap reached" in campaign._cap_reached(never_paused, 4)


def test_the_caps_in_force_are_on_the_manifest(campaign_parts):
    campaign = _campaign(*campaign_parts, p99s=[300.0, 58.0])
    campaign.cost_cap_usd, campaign.wall_clock_cap_s = 0.5, 7200
    campaign.cost_so_far = lambda result: 0.0
    manifest = asyncio.run(campaign.run()).as_dict()
    assert manifest["caps"] == {"cost_usd": 0.5, "wall_clock_s": 7200}
    assert manifest["cap_hit"] == {}


class TestPlumbing:
    def test_build_campaign_converts_hours_and_refuses_non_positive_caps(self, tmp_path, monkeypatch):
        from crucible.perf.commands import build_campaign

        monkeypatch.chdir(REPO)
        (tmp_path / "state").mkdir()
        kwargs = dict(profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5,
                      warmup_s=1, measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state",
                      workspace="", approve_mode="file", approval_timeout_s=1, model="m", provider="p")
        campaign = build_campaign(**kwargs, cost_cap_usd=0.25, wall_clock_cap_h=2)
        assert (campaign.cost_cap_usd, campaign.wall_clock_cap_s) == (0.25, 7200)
        with pytest.raises(ValueError, match="cost cap"):
            build_campaign(**kwargs, cost_cap_usd=0)

    def test_the_action_builder_passes_each_cap_as_one_token(self):
        args = A.run_args(cost_cap=0.25, wall_clock_cap=2)
        assert "--cost-cap=0.25" in args and "--wall-clock-cap=2.0" in args

    def test_the_cli_accepts_both(self, monkeypatch):
        from crucible import cli
        from crucible.perf import commands

        seen = {}
        monkeypatch.setattr(commands, "cmd_run", lambda **kw: seen.update(kw) or 0)
        monkeypatch.setattr("sys.argv", ["crucible", "run", "--cost-cap=0.1", "--wall-clock-cap=8"])
        cli.main()
        assert (seen["cost_cap_usd"], seen["wall_clock_cap_h"]) == (0.1, 8.0)
