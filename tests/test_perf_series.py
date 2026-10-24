"""Multi-scenario campaigns: `crucible run --all-scenarios`. Group 37.

DRAFTED by Claude Code, 24 October 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 37 for the questions to check.

Until now only the wizard's first scenario ran. The SLA now declares a
`scenarios:` list, and `--all-scenarios` runs each one in order as its own
campaign with its own baseline and verdicts. What is asserted:

1. each scenario runs on its declared load, under its own run id, in order;
2. the SLA is re-read between scenarios, so an edit to a later one is honoured;
3. a pause stops the series, and resuming that scenario carries the rest on;
4. the caps cover the whole series: each scenario gets what is left;
5. an SLA with no scenarios, or a malformed list, is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.perf import commands
from crucible.perf.campaign import CampaignRefused, Sla, request_pause
from crucible.perf.profile import TargetProfile
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign

SCENARIOS = ('scenarios: [{"name": "steady", "users": 50, "measure_s": 300, "spawn_rate": 5}, '
             '{"name": "burst", "users": 100, "measure_s": 180}]\n')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML + SCENARIOS, encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    profile = TargetProfile.from_mapping({
        "name": "spring-boot", "runtime": "jvm", "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {"spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100}},
        "protected_paths": ["config/slo.yaml"], "deploy": {"branch": "perftest_sandbox"},
    })
    builds: list[dict] = []
    hooks: dict = {}

    def fake_build(*, run_id, state_dir, **kwargs):
        builds.append({"run_id": run_id, **kwargs})
        campaign = _campaign(profile, Sla.load(sla_path), workspace, p99s=[300.0, 58.0] * 2)
        campaign.run_id, campaign.state_dir = run_id, Path(state_dir)
        campaign.results_dir = Path(state_dir) / "results"
        campaign.cost_cap_usd = kwargs.get("cost_cap_usd")
        if run_id in hooks:
            hooks[run_id](campaign)
        return campaign

    monkeypatch.setattr(commands, "build_campaign", fake_build)
    base = dict(profile_name="spring-boot", sla_path=str(sla_path), scenario_name="db-latency", users=7,
                warmup_s=1, measure_s=1, max_experiments=1, workspace="", approve_mode="preapproved",
                approval_timeout_s=1, model="m", provider="p")
    return sla_path, state, builds, hooks, base


def _plan(state, series_id):
    return json.loads((state / "series" / f"{series_id}.json").read_text(encoding="utf-8"))


class TestEachScenarioRunsOnItsOwnLoad:
    def test_in_order_under_its_own_run_id_and_load(self, setup):
        sla_path, state, builds, _, base = setup
        assert commands.cmd_run(**base, state_dir=str(state), run_id="s1", all_scenarios=True) == commands.OK
        assert [(b["run_id"], b["users"], b["measure_s"], b["spawn_rate"]) for b in builds] == [
            ("s1-steady", 50, 300.0, 5.0), ("s1-burst", 100, 180.0, None)]
        plan = _plan(state, "s1")
        assert plan["status"] == "complete" and plan["done"] == ["steady", "burst"]
        manifest = json.loads((state / "results" / "s1-burst.json").read_text(encoding="utf-8"))
        assert manifest["series"] == {"id": "s1", "index": 2, "of": 2, "scenario": "burst"}

    def test_an_edit_to_a_later_scenario_is_honoured(self, setup):
        sla_path, state, builds, hooks, base = setup

        def edit_during_steady(campaign):
            real = campaign.measure

            def measuring(scenario, run_id):
                sla_path.write_text(sla_path.read_text(encoding="utf-8").replace('"users": 100', '"users": 40'),
                                    encoding="utf-8")
                return real(scenario, run_id)
            campaign.measure = measuring

        hooks["s2-steady"] = edit_during_steady
        commands.cmd_run(**base, state_dir=str(state), run_id="s2", all_scenarios=True)
        assert builds[1]["users"] == 40


class TestAPauseStopsTheSeriesAndResumeCarriesItOn:
    def test_pause_then_resume(self, setup):
        _, state, builds, hooks, base = setup
        hooks["s3-steady"] = lambda campaign: request_pause(campaign.state_dir, campaign.run_id, "operator")
        commands.cmd_run(**base, state_dir=str(state), run_id="s3", all_scenarios=True)
        plan = _plan(state, "s3")
        assert plan["status"].startswith("paused in steady") and plan["done"] == []
        assert [b["run_id"] for b in builds] == ["s3-steady"]

        hooks.clear()
        assert commands.cmd_resume("s3-steady", state_dir=str(state)) == commands.OK
        plan = _plan(state, "s3")
        assert plan["done"] == ["steady", "burst"] and plan["status"] == "complete"
        assert [b["run_id"] for b in builds] == ["s3-steady", "s3-steady", "s3-burst"]
        assert builds[1]["users"] == 50


class TestCapsCoverTheSeries:
    def test_each_scenario_gets_what_is_left_and_the_series_stops_when_used_up(self, setup, monkeypatch):
        _, state, builds, _, base = setup
        monkeypatch.setattr("crucible.perf.scorer.score_cost",
                            lambda campaign, pricing: type("C", (), {"total": 0.06})())
        commands.cmd_run(**base, state_dir=str(state), run_id="s4", all_scenarios=True, cost_cap_usd=0.1)
        assert builds[0]["cost_cap_usd"] == pytest.approx(0.1)
        assert builds[1]["cost_cap_usd"] == pytest.approx(0.04)

        builds.clear()
        commands.cmd_run(**base, state_dir=str(state), run_id="s5", all_scenarios=True, cost_cap_usd=0.05)
        assert [b["run_id"] for b in builds] == ["s5-steady"]
        assert "cost cap reached" in _plan(state, "s5")["status"]

    def test_a_cap_used_up_between_scenarios_stops_before_the_next(self, setup, monkeypatch):
        _, state, builds, _, base = setup
        spent = iter([0.0, 0.06])
        monkeypatch.setattr(commands, "_record_scenario",
                            lambda plan, name, result: plan.update(spent_usd=next(spent) + 0.06))
        commands.cmd_run(**base, state_dir=str(state), run_id="s6", all_scenarios=True, cost_cap_usd=0.05)
        assert [b["run_id"] for b in builds] == ["s6-steady"]
        assert "used up before 'burst'" in _plan(state, "s6")["status"]


class TestRefusals:
    def test_an_sla_with_no_scenarios_is_refused(self, setup):
        sla_path, state, builds, _, base = setup
        sla_path.write_text(SLA_YAML, encoding="utf-8")
        assert commands.cmd_run(**base, state_dir=str(state), all_scenarios=True) == commands.REFUSED
        assert builds == []

    @pytest.mark.parametrize("bad", [
        'scenarios: [{"name": "a", "users": 0}]',
        'scenarios: [{"name": "a", "users": 5}, {"name": "a", "users": 6}]',
        'scenarios: [{"name": "a b", "users": 5}]',
        'scenarios: [{"name": "a", "users": 5, "measure_s": 10}]',
        'scenarios: {"name": "a"}',
    ])
    def test_a_malformed_list_is_refused_whole(self, tmp_path, bad):
        path = tmp_path / "slo.yaml"
        path.write_text(SLA_YAML + bad + "\n", encoding="utf-8")
        with pytest.raises(CampaignRefused):
            Sla.load(path)


class TestTheCeilingProbeTakesGoalEditsBetweenSteps:
    def _probe(self, tmp_path, edit, p99s):
        from crucible.perf.ceiling import CeilingProbe
        from crucible.perf.runner import LoadResult, Scenario
        from tests.test_perf_ceiling import SLA_YAML as CEILING_SLA

        path = tmp_path / "slo.yaml"
        path.write_text(CEILING_SLA, encoding="utf-8")
        seq = iter(p99s)

        def measure(sc, run_id):
            p99 = next(seq)
            if sc.users == 50:
                edit(path)
            return LoadResult(run_id=run_id, scenario=sc.name, p50_ms=p99 * 0.6, p99_ms=p99,
                              error_rate_pct=0.0, rps=100.0), {}

        probe = CeilingProbe(sla=Sla.load(path), base_scenario=Scenario(name="db", host="h", users=50),
                             measure=measure, max_users=200, step_size=50, run_id="c1",
                             results_dir=tmp_path / "results", sla_path=path)
        return probe.run()

    def test_a_tightened_goal_moves_where_the_probe_stops(self, tmp_path):
        tighten = lambda p: p.write_text(p.read_text(encoding="utf-8").replace("p99_ms: 150", "p99_ms: 100"),  # noqa: E731
                                         encoding="utf-8")
        result = self._probe(tmp_path, tighten, [140.0, 130.0, 120.0, 110.0])
        assert result.knee == (50, 100)
        assert [s.sla_p99_ms for s in result.steps] == [150.0, 100.0]
        assert result.amendments[0]["at_step"] == 2
        assert result.amendments[0]["changes"]["p99_ms"] == {"from": 150.0, "to": 100.0}

    def test_an_endpoint_edit_is_recorded_not_applied(self, tmp_path):
        move = lambda p: p.write_text(p.read_text(encoding="utf-8").replace("endpoint: /orders", "endpoint: /api/db"),  # noqa: E731
                                      encoding="utf-8")
        result = self._probe(tmp_path, move, [140.0, 140.0, 140.0, 140.0])
        assert result.sla["endpoint"] == "/orders"
        assert "not applied mid-probe" in result.amendments[0]["not_applied"]
