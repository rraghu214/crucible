"""Operator edits to the SLA and profile mid-campaign. Group 33.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 33 for the questions to check.

Operator decisions, 24 October 2026: the files may change mid-run (a goal
relaxed from 2 s to 4 s, a 900 tpm load found unnecessary) and the campaign
honours the change at its next experiment boundary. Semi-Automated pauses to
confirm; the other modes take it without pausing. A load change re-measures the
baseline first, because a verdict compares two measurements at the same load.
The agent still cannot write either file (§4.4). What is asserted:

1. both files are fingerprinted on the manifest at start;
2. a goal edit is taken at the next boundary and judged against from then on;
3. a load edit re-measures the baseline before the next verdict;
4. Semi-Automated pauses on an edit and takes it on resume;
5. an edit that would not load, or would move the deploy branch or reach
   production, is recorded and not applied.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from crucible.perf.applicator import Applicator
from crucible.perf.campaign import Sla
from crucible.perf.profile import TargetProfile
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign, _FakeGit

REPO = Path(__file__).resolve().parents[1]
LOAD = "  at_load:\n    users: 50\n"
SLA_WITH_LOAD = SLA_YAML.replace("  error_rate_pct: 1.0\n", "  error_rate_pct: 1.0\n" + LOAD)


@pytest.fixture
def files(tmp_path):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_WITH_LOAD, encoding="utf-8")
    profile_path = tmp_path / "spring-boot.yaml"
    shutil.copy(REPO / "config" / "profiles" / "spring-boot.yaml", profile_path)
    shutil.copy(REPO / "config" / "profiles" / "spring-boot.SKILL.md", tmp_path / "spring-boot.SKILL.md")
    workspace = tmp_path / "ws"
    (workspace / "src" / "main" / "resources").mkdir(parents=True)
    (workspace / "src" / "main" / "resources" / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    return sla_path, profile_path, workspace


def _guarded(files, *, p99s, edit_on_measure=None, edit=None, max_experiments=2, confirm=False):
    sla_path, profile_path, workspace = files
    profile = TargetProfile.load(profile_path)
    campaign = _campaign(profile, Sla.load(sla_path), workspace, p99s=p99s, max_experiments=max_experiments)
    campaign.applicator = Applicator(profile=profile, workspace=workspace, _git=_FakeGit())
    campaign.guarded_files = (sla_path, profile_path)
    campaign.confirm_config_changes = confirm
    real, calls = campaign.measure, []

    def measuring(scenario, run_id):
        calls.append((run_id, scenario.users))
        if edit_on_measure is not None and len(calls) == edit_on_measure:
            edit()
        return real(scenario, run_id)

    campaign.measure = measuring
    campaign.calls = calls
    return campaign


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set(path: Path, old: str, new: str):
    def edit():
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    return edit


class TestFingerprintedAtStart:
    def test_both_files_are_recorded_on_the_manifest(self, files):
        sla_path, profile_path, _ = files
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0]).run())
        assert result.as_dict()["guarded_files"] == {str(sla_path): _sha(sla_path), str(profile_path): _sha(profile_path)}
        assert result.amendments == []


class TestAGoalEditIsHonoured:
    def test_a_relaxed_goal_is_judged_against_from_the_next_experiment(self, files):
        """p99 250 fails 120 ms but meets 400 ms: meeting it proves the new goal was used."""
        campaign = _guarded(files, p99s=[300.0, 299.0, 250.0],
                            edit_on_measure=2, edit=_set(files[0], "p99_ms: 120", "p99_ms: 400"))
        result = asyncio.run(campaign.run())
        assert not result.aborted and not result.paused
        [amendment] = result.amendments
        assert amendment["at_experiment"] == 2 and amendment["changes"]["p99_ms"] == {"from": 120.0, "to": 400.0}
        assert amendment["rebaselined"] is None and "automatically" in amendment["accepted"]
        assert result.experiments[-1].sla_met_after is True
        assert result.sla["p99_ms"] == 400.0

    def test_a_comment_only_edit_changes_nothing(self, files):
        campaign = _guarded(files, p99s=[300.0, 299.0, 298.0],
                            edit_on_measure=2, edit=_set(files[0], "name: perflab-db-latency", "name: perflab-db-latency  # note"))
        result = asyncio.run(campaign.run())
        assert result.amendments == []


class TestALoadEditReMeasuresTheBaseline:
    def test_the_next_verdict_compares_against_a_baseline_at_the_new_load(self, files):
        campaign = _guarded(files, p99s=[300.0, 299.0, 200.0, 199.0],
                            edit_on_measure=2, edit=_set(files[0], "users: 50", "users: 20"))
        result = asyncio.run(campaign.run())
        run_ids = [r for r, _ in campaign.calls]
        assert run_ids[2].endswith("-rebaseline-02") and campaign.calls[2][1] == 20
        [amendment] = result.amendments
        assert amendment["rebaselined"]["p99_ms"] == 200.0
        assert result.experiments[-1].before["load"]["p99_ms"] == 200.0

    def test_a_new_baseline_that_meets_the_goal_stops_the_campaign(self, files):
        sla = files[0]

        def edit():
            _set(sla, "users: 50", "users: 10")()
            _set(sla, "p99_ms: 120", "p99_ms: 400")()

        campaign = _guarded(files, p99s=[300.0, 299.0, 150.0], edit_on_measure=2, edit=edit)
        result = asyncio.run(campaign.run())
        assert "new baseline already meets the SLA" in result.stopped_reason
        assert len(result.experiments) == 1


class TestSemiAutomatedPausesToConfirm:
    def test_it_pauses_then_takes_the_edit_on_resume(self, files):
        campaign = _guarded(files, p99s=[300.0, 299.0, 250.0], confirm=True,
                            edit_on_measure=2, edit=_set(files[0], "p99_ms: 120", "p99_ms: 400"))
        paused = asyncio.run(campaign.run())
        assert paused.paused and "Semi-Automated pauses to confirm" in paused.stopped_reason
        assert paused.amendments == []

        resumed = asyncio.run(campaign.resume())
        [amendment] = resumed.amendments
        assert amendment["accepted"] == "operator resume"
        assert resumed.experiments[-1].sla_met_after is True


class TestEditsThatAreNotApplied:
    def test_an_unloadable_edit_is_recorded_and_the_run_continues(self, files):
        campaign = _guarded(files, p99s=[300.0, 299.0, 298.0],
                            edit_on_measure=2, edit=_set(files[0], "p99_ms: 120", "p99_ms: fast"))
        result = asyncio.run(campaign.run())
        assert not result.aborted
        assert "do not load" in result.amendments[0]["rejected"]
        assert result.sla["p99_ms"] == 120.0

    def test_a_production_environment_is_refused(self, files):
        campaign = _guarded(files, p99s=[300.0, 299.0, 298.0],
                            edit_on_measure=2, edit=_set(files[0], "kind: pre-prod", "kind: production"))
        result = asyncio.run(campaign.run())
        assert "do not load" in result.amendments[0]["rejected"]

    def test_a_deploy_branch_edit_is_not_applied_mid_run(self, files):
        profile_path = files[1]
        campaign = _guarded(files, p99s=[300.0, 299.0, 298.0], edit_on_measure=2,
                            edit=_set(profile_path, "branch: perftest_sandbox", "branch: perftest_other"))
        result = asyncio.run(campaign.run())
        assert campaign.profile.deploy.branch == "perftest_sandbox"
        assert any("lock on 'perftest_sandbox'" in n for a in result.amendments for n in a.get("notes", []))


@pytest.mark.parametrize("approve, confirm", [("file", True), ("preapproved", False)])
def test_build_campaign_guards_its_files_and_only_semi_automated_confirms(tmp_path, monkeypatch, approve, confirm):
    from crucible.perf.commands import build_campaign

    monkeypatch.chdir(REPO)
    (tmp_path / "state").mkdir()
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
        approve_mode=approve, approval_timeout_s=1, model="m", provider="p",
    )
    assert {p.name for p in campaign.guarded_files} == {"slo.yaml", "spring-boot.yaml"}
    assert campaign.confirm_config_changes is confirm
