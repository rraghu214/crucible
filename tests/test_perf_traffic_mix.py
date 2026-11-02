"""Traffic-mix scenarios: several endpoints at set weights, each judged on its own p99. Group 39.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 25 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 39 for the questions to check.

Operator, 24 October 2026: distribute load across a service's endpoints
(e.g. 30/25/20/15/10%). Group 31's lesson holds: a blended p99 says nothing
about any one endpoint. So a mix runs its endpoints at their weights, and every
SLA check and verdict is per endpoint. What is asserted:

1. weights are whole numbers totalling 100 over at least two driven endpoints;
2. Locust runs exactly the mix's tags, at the mix's weights;
3. each endpoint's own statistics are read from the run;
4. an experiment is WORSE if any endpoint got worse, even when the blend improved;
5. the SLA is met only when every endpoint meets its own target;
6. the wizard writes a mix and refuses a malformed or undriven one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crucible.perf.campaign import (
    IMPROVED,
    INCONCLUSIVE,
    NOT_MEASURED,
    WORSE,
    CampaignRefused,
    Sla,
    combine_endpoint_verdicts,
    mix_from,
)
from crucible.perf.profile import TargetProfile
from crucible.perf.runner import LoadResult, LocustRunner, Scenario, _read_locust_per_endpoint, mix_env
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign

REPO = Path(__file__).resolve().parents[1]


def _sla(tmp_path, mix_yaml: str) -> Sla:
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML + f'scenarios: [{{"name": "mixed", "users": 100, "mix": {mix_yaml}}}]\n',
                    encoding="utf-8")
    return Sla.load(path)


class TestTheMixIsDeclaredSafely:
    def test_a_good_mix_is_normalised(self, tmp_path):
        sla = _sla(tmp_path, '{"/api/db": {"weight": 60, "p99_ms": 150}, "/api/orders": 40}')
        assert sla.scenarios[0]["mix"] == {"/api/db": {"weight": 60, "p99_ms": 150.0},
                                           "/api/orders": {"weight": 40, "p99_ms": None}}

    @pytest.mark.parametrize("bad", [
        '{"/api/db": 60, "/api/orders": 30}',
        '{"/api/db": 100}',
        '{"/api/db": 50.5, "/api/orders": 49.5}',
        '{"/api/db": 0, "/api/orders": 100}',
        '{"/api/db": {"weight": 60, "p99_ms": -1}, "/api/orders": 40}',
    ])
    def test_a_bad_mix_is_refused_whole(self, tmp_path, bad):
        with pytest.raises(CampaignRefused):
            _sla(tmp_path, bad)

    def test_endpoints_become_tags_and_unset_targets_inherit_the_objective(self, tmp_path, monkeypatch):
        monkeypatch.chdir(REPO)
        sla = _sla(tmp_path, '{"/api/db": {"weight": 60, "p99_ms": 150}, "/api/orders": 40}')
        tags, targets = mix_from(sla.scenarios[0]["mix"], sla)
        assert tags == {"db": 60, "orders": 40}
        assert targets == {"/api/db": 150.0, "/api/orders": 120.0}

    def test_an_endpoint_no_task_drives_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(REPO)
        sla = _sla(tmp_path, '{"/api/db": 60, "/transfer": 40}')
        with pytest.raises(ValueError, match="/transfer"):
            mix_from(sla.scenarios[0]["mix"], sla)


class TestLocustRunsTheMix:
    def test_exactly_the_mix_tags_and_weights(self, tmp_path):
        runner = LocustRunner(results_dir=tmp_path)
        command = runner._command(Scenario(name="m", host="h", mix={"db": 60, "orders": 40}, tags=("ignored",)),
                                  60, tmp_path / "x")
        assert command[command.index("--tags") + 1:] == ["db", "orders"]
        assert mix_env({"db": 60, "orders": 40}) == {"CRUCIBLE_MIX": "db=60,orders=40"}
        assert mix_env({}) == {}

    def test_each_endpoints_statistics_are_read(self, tmp_path):
        csv = tmp_path / "run_stats.csv"
        csv.write_text("Type,Name,Request Count,Failure Count,50%,99%,Requests/s\n"
                       "GET,/api/db,1000,10,40,180,30\n"
                       "GET,/api/orders,500,0,20,90,15\n"
                       ",Aggregated,1500,10,30,170,45\n", encoding="utf-8")
        per = _read_locust_per_endpoint(csv)
        assert set(per) == {"/api/db", "/api/orders"}
        assert per["/api/db"]["p99_ms"] == 180.0 and per["/api/db"]["error_rate_pct"] == 1.0


@pytest.mark.parametrize("verdicts, combined", [
    ({"a": IMPROVED, "b": WORSE}, WORSE),
    ({"a": IMPROVED, "b": INCONCLUSIVE}, IMPROVED),
    ({"a": INCONCLUSIVE, "b": INCONCLUSIVE}, INCONCLUSIVE),
    ({"a": IMPROVED, "b": NOT_MEASURED}, NOT_MEASURED),
])
def test_one_verdict_never_hides_an_endpoint_that_got_worse(verdicts, combined):
    assert combine_endpoint_verdicts(verdicts) == combined


class TestTheCampaignJudgesPerEndpoint:
    def _run(self, tmp_path, measurements, targets, accept=False):
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
        campaign = _campaign(profile, Sla.load(sla_path), workspace, p99s=[0.0] * 4)
        campaign.mix_targets = targets
        campaign.accept_tradeoffs = accept
        seq = iter(measurements)

        def measure(scenario, run_id):
            blended, per = next(seq)
            return LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=blended, error_rate_pct=0.0,
                              per_endpoint={e: {"p99_ms": v, "error_rate_pct": 0.0} for e, v in per.items()}), {}

        campaign.measure = measure
        return asyncio.run(campaign.run())

    def test_a_better_blend_with_a_worse_endpoint_is_worse(self, tmp_path):
        result = self._run(tmp_path, [
            (300.0, {"/api/db": 400.0, "/api/orders": 100.0}),
            (200.0, {"/api/db": 200.0, "/api/orders": 150.0}),
        ], {"/api/db": 120.0, "/api/orders": 120.0})
        [experiment] = result.experiments
        assert experiment.verdict == WORSE
        assert experiment.per_endpoint["/api/orders"]["verdict"] == WORSE
        assert experiment.per_endpoint["/api/db"]["verdict"] == IMPROVED

    def test_the_sla_is_met_only_when_every_endpoint_meets_its_own_target(self, tmp_path):
        result = self._run(tmp_path, [
            (100.0, {"/api/db": 110.0, "/api/orders": 130.0}),
            (90.0, {"/api/db": 100.0, "/api/orders": 110.0}),
        ], {"/api/db": 120.0, "/api/orders": 120.0})
        assert result.baseline["sla_met"] is False
        assert result.experiments[0].sla_met_after is True


def test_build_campaign_runs_a_declared_mix(tmp_path, monkeypatch):
    from crucible.perf.commands import build_campaign

    monkeypatch.chdir(REPO)
    (tmp_path / "state").mkdir()
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="mixed", users=100, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
        approve_mode="file", approval_timeout_s=1, model="m", provider="p",
        mix={"/api/db": {"weight": 70, "p99_ms": None}, "/api/catalog": {"weight": 30, "p99_ms": 80.0}})
    assert campaign.scenario.mix == {"db": 70, "catalog": 30} and campaign.scenario.tags == ()
    assert campaign.mix_targets == {"/api/db": 120.0, "/api/catalog": 80.0}


class TestAcceptedTradeoffs:
    """39.3 (operator, 25 October 2026): opt-in. An endpoint that got slower but
    stays within its own target does not make the change WORSE."""

    def test_the_combiner_tolerates_only_named_endpoints(self):
        verdicts = {"a": IMPROVED, "b": WORSE}
        assert combine_endpoint_verdicts(verdicts) == WORSE
        assert combine_endpoint_verdicts(verdicts, tolerated={"b"}) == IMPROVED
        assert combine_endpoint_verdicts({"a": INCONCLUSIVE, "b": WORSE}, tolerated={"b"}) == INCONCLUSIVE

    def _measure(self, tmp_path, after_orders, accept):
        result = TestTheCampaignJudgesPerEndpoint()._run(tmp_path, [
            (300.0, {"/api/db": 400.0, "/api/orders": 100.0}),
            (200.0, {"/api/db": 200.0, "/api/orders": after_orders}),
        ], {"/api/db": 120.0, "/api/orders": 120.0}, accept=accept)
        return result.experiments[0]

    def test_slower_but_within_target_is_accepted_when_opted_in(self, tmp_path):
        experiment = self._measure(tmp_path, after_orders=115.0, accept=True)
        assert experiment.verdict == IMPROVED
        assert experiment.per_endpoint["/api/orders"]["tradeoff_accepted"] is True

    def test_slower_and_over_target_is_still_worse_when_opted_in(self, tmp_path):
        experiment = self._measure(tmp_path, after_orders=150.0, accept=True)
        assert experiment.verdict == WORSE

    def test_without_the_opt_in_slower_within_target_is_worse(self, tmp_path):
        experiment = self._measure(tmp_path, after_orders=115.0, accept=False)
        assert experiment.verdict == WORSE

    def test_the_sla_and_build_campaign_carry_the_opt_in(self, tmp_path, monkeypatch):
        from crucible.perf.commands import build_campaign

        sla = _sla(tmp_path, '{"/api/db": 60, "/api/orders": 40}')
        assert "accept_tradeoffs" not in sla.scenarios[0]
        monkeypatch.chdir(REPO)
        (tmp_path / "state").mkdir()
        campaign = build_campaign(
            profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="m", users=10, warmup_s=1,
            measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
            approve_mode="file", approval_timeout_s=1, model="m", provider="p",
            mix={"/api/db": {"weight": 60, "p99_ms": None}, "/api/orders": {"weight": 40, "p99_ms": None}},
            accept_tradeoffs=True)
        assert campaign.accept_tradeoffs is True
