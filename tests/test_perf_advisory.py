"""Advisory mode: diagnose and propose with the exact diff, never apply. Group 38.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 25 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 38 for the questions to check.

The wizard offered Advisory ("AI diagnoses and suggests. You apply every change
manually") and Launch refused it. What is asserted:

1. an Advisory run measures and diagnoses, then records the proposal with the
   exact diff, and writes, deploys, approves and re-measures nothing;
2. its verdict is NOT_MEASURED: an unapplied proposal never looks verified;
3. the proposal still has to pass the guard;
4. without the config file in the workspace, the diff is declared unavailable,
   not invented;
5. it needs no deploy pipeline, and the CLI, actions and wizard all carry it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crucible.perf.applicator import Applicator, Change, Proposal
from crucible.perf.campaign import NOT_MEASURED, Sla
from crucible.perf.profile import TargetProfile
from crucible.ui import actions as A
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign, _FakeDeployer

REPO = Path(__file__).resolve().parents[1]
POOL = "spring.datasource.hikari.maximum-pool-size"


@pytest.fixture
def parts(tmp_path):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    profile = TargetProfile.from_mapping({
        "name": "spring-boot", "runtime": "jvm", "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {POOL: {"type": "int", "min": 1, "max": 100}},
        "protected_paths": ["config/slo.yaml"], "deploy": {"branch": "perftest_sandbox"},
    })
    return profile, Sla.load(sla_path), workspace


class TestNothingIsApplied:
    def test_it_records_the_exact_diff_and_touches_nothing(self, parts):
        profile, sla, workspace = parts
        deployer = _FakeDeployer(results=[])
        campaign = _campaign(profile, sla, workspace, p99s=[300.0], deployer=deployer, max_experiments=3)
        campaign.advisory = True
        measured = []
        real = campaign.measure
        campaign.measure = lambda sc, rid: measured.append(rid) or real(sc, rid)

        result = asyncio.run(campaign.run())

        assert (workspace / "application.properties").read_text(encoding="utf-8") == PROPERTIES
        assert deployer.commits == [] and measured == [f"{campaign.run_id}-baseline"]
        [manifest] = result.experiments
        assert manifest.verdict == NOT_MEASURED and "nothing was applied" in manifest.verdict_reason
        assert not manifest.approval and not manifest.apply_result
        assert "-spring.datasource.hikari.maximum-pool-size=10" in manifest.advisory["diff"]
        assert "+spring.datasource.hikari.maximum-pool-size=20" in manifest.advisory["diff"]
        assert manifest.advisory["changes"] == [{"prop": POOL, "from": "10", "to": 20}]
        assert "advisory run" in result.stopped_reason

    def test_a_proposal_outside_the_profile_is_still_refused(self, parts):
        profile, sla, workspace = parts
        outside = Proposal(cause_family="connection_pool_exhaustion",
                           changes=(Change(prop=POOL, value=500),), reasoning="x", confidence=0.5)
        campaign = _campaign(profile, sla, workspace, p99s=[300.0], proposal=outside, max_experiments=1)
        campaign.advisory = True
        result = asyncio.run(campaign.run())
        assert all(m.refused_by_guard for m in result.experiments)
        # The refusal is recorded; no diff is ever offered for a refused proposal.
        assert all("diff" not in m.advisory for m in result.experiments)
        assert all(a.get("refused") for m in result.experiments for a in m.advisory["alternatives"])


class _VaryingDiagnoser:
    """Proposes the pool sizes given in turn, as a model asked for alternatives would."""

    def __init__(self, values):
        self.values, self.ruled_out_seen = list(values), []

    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings="", stakeholder_request=""):
        from crucible.perf.diagnosis import Diagnosis

        self.ruled_out_seen.append(tuple(ruled_out))
        value = self.values.pop(0) if self.values else 20
        proposal = Proposal(cause_family="connection_pool_exhaustion", changes=(Change(prop=POOL, value=value),),
                            reasoning=f"try {value}", confidence=0.5)
        return Diagnosis(proposal=proposal, provider="gemini", model="gemini-x", input_tokens=1000, output_tokens=100)


class TestAlternatives:
    def _run(self, parts, values):
        profile, sla, workspace = parts
        campaign = _campaign(profile, sla, workspace, p99s=[300.0])
        campaign.advisory = True
        campaign.diagnoser = _VaryingDiagnoser(values)
        return campaign, asyncio.run(campaign.run())

    def test_up_to_three_ranked_alternatives_each_with_its_own_diff(self, parts):
        campaign, result = self._run(parts, [20, 30, 40, 50])
        [manifest] = result.experiments
        alternatives = manifest.advisory["alternatives"]
        assert [a["rank"] for a in alternatives] == [1, 2, 3]
        assert ["+spring.datasource.hikari.maximum-pool-size=" + v in a["diff"]
                for a, v in zip(alternatives, ["20", "30", "40"])] == [True, True, True]
        assert manifest.advisory["diff"] == alternatives[0]["diff"]
        assert "already proposed" in " ".join(campaign.diagnoser.ruled_out_seen[1])
        assert "3 ranked proposal(s)" in result.stopped_reason

    def test_a_repeated_proposal_stops_the_search_early(self, parts):
        _, result = self._run(parts, [20, 20, 20])
        assert len(result.experiments[0].advisory["alternatives"]) == 1

    def test_a_refused_alternative_is_kept_with_its_reason_and_the_rest_still_offered(self, parts):
        _, result = self._run(parts, [20, 500, 30])
        alternatives = result.experiments[0].advisory["alternatives"]
        assert "refused" in alternatives[1] and "diff" not in alternatives[1]
        assert "diff" in alternatives[0] and "diff" in alternatives[2]

    def test_every_alternatives_model_call_is_priced(self, parts):
        from crucible.economics.pricing import Pricing
        from crucible.perf.scorer import score_cost

        _, result = self._run(parts, [20, 30, 40])
        pricing = Pricing.from_mapping({"currency": "USD", "models": {
            "gemini-x": {"input": 1.0, "output": 1.0}}})
        manifest = result.as_dict()
        manifest["experiments"][0]["diagnosis"] = {"served_by_model": "gemini-x", "input_tokens": 1000,
                                                   "output_tokens": 100}
        assert score_cost(manifest, pricing).total == pytest.approx(3 * 1100 / 1e6)


def test_without_the_config_file_the_diff_is_declared_unavailable(parts, tmp_path):
    profile, _, _ = parts
    applicator = Applicator(profile=profile, workspace=tmp_path / "empty")
    preview = applicator.preview(Proposal(cause_family="connection_pool_exhaustion",
                                          changes=(Change(prop=POOL, value=20),), reasoning="x", confidence=0.5))
    assert preview["diff"] == "" and "could not be computed" in preview["note"]
    assert preview["changes"] == [{"prop": POOL, "from": None, "to": 20}]


class TestPlumbing:
    def test_build_campaign_needs_no_deploy_pipeline(self, tmp_path, monkeypatch):
        from crucible.perf import commands

        monkeypatch.chdir(REPO)
        (tmp_path / "state").mkdir()
        monkeypatch.setattr(commands, "_check_workspace_sync",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("advisory must not check sync")))
        campaign = commands.build_campaign(
            profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5, warmup_s=1,
            measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="some/ws",
            approve_mode="file", approval_timeout_s=1, model="m", provider="p", advisory=True)
        assert campaign.advisory and campaign.deployer is None and not campaign.confirm_config_changes

    def test_the_cli_and_actions_carry_it(self, monkeypatch):
        from crucible import cli
        from crucible.perf import commands

        assert "--advisory" in A.run_args(advisory=True) and "--advisory" not in A.run_args()
        seen = {}
        monkeypatch.setattr(commands, "cmd_run", lambda **kw: seen.update(kw) or 0)
        monkeypatch.setattr("sys.argv", ["crucible", "run", "--advisory"])
        cli.main()
        assert seen["advisory"] is True

    def test_the_budget_counts_up_to_three_diagnoses_and_no_deploys(self):
        from crucible.ui.wizard import estimate

        est = estimate(mode="Advisory", warmup_s=120, measure_s=300, experiments=5, verify_timeout_s=300,
                       per_call_costs=[0.002])
        assert est["calls"] == 3 and est["max_s"] == 420 and est["cost"] == pytest.approx(0.006)
