"""The SLA and profile cannot change under a running campaign. Group 33.

DRAFTED by Claude Code, 24 October 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 33 for the questions to check.

Operator decision, 24 October 2026: the files may be edited only once a campaign
has stopped. A running campaign holds both in memory, so an edit cannot change
how it judges; what it can do is make the record lie (the manifest's goal no
longer matches the file beside it) and, worse, change the goal under a paused
campaign that `resume` reloads from disk. What is asserted:

1. both files are fingerprinted on the manifest at start;
2. an edit aborts the run at the next check, names the file, and the
   experiment in flight gets no verdict;
3. a paused campaign whose files changed refuses to resume;
4. `build_campaign` guards the SLA and profile it actually loaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from crucible.perf.campaign import NOT_MEASURED, CampaignRefused, Sla, paused_state_path
from crucible.perf.profile import TargetProfile
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign


@pytest.fixture
def files(tmp_path):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML, encoding="utf-8")
    profile_path = tmp_path / "spring-boot.yaml"
    profile_path.write_text("name: spring-boot\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    profile = TargetProfile.from_mapping({
        "name": "spring-boot", "runtime": "jvm", "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {"spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100}},
        "protected_paths": ["config/slo.yaml"], "deploy": {"branch": "perftest_sandbox"},
    })
    return sla_path, profile_path, workspace, profile


def _guarded(files, *, p99s, edit_on_measure=None, edit=None, max_experiments=1):
    sla_path, profile_path, workspace, profile = files
    campaign = _campaign(profile, Sla.load(sla_path), workspace, p99s=p99s, max_experiments=max_experiments)
    campaign.guarded_files = (sla_path, profile_path)
    if edit_on_measure is not None:
        real, calls = campaign.measure, {"n": 0}

        def measuring(scenario, run_id):
            calls["n"] += 1
            if calls["n"] == edit_on_measure:
                edit()
            return real(scenario, run_id)

        campaign.measure = measuring
    return campaign


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFingerprintedAtStart:
    def test_both_files_are_recorded_on_the_manifest(self, files):
        sla_path, profile_path, *_ = files
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0]).run())
        assert result.as_dict()["guarded_files"] == {str(sla_path): _sha(sla_path), str(profile_path): _sha(profile_path)}
        assert not result.aborted


class TestAnEditStopsTheRun:
    def test_an_sla_edit_during_an_experiment_aborts_before_its_verdict(self, files):
        sla_path = files[0]
        edit = lambda: sla_path.write_text(SLA_YAML.replace("p99_ms: 120", "p99_ms: 400"), encoding="utf-8")  # noqa: E731
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0], edit_on_measure=2, edit=edit).run())
        assert result.aborted and str(sla_path) in result.abort_reason
        assert result.experiments[-1].verdict == NOT_MEASURED
        assert str(sla_path) in result.experiments[-1].verdict_reason

    def test_a_profile_edit_during_the_baseline_aborts_before_any_experiment(self, files):
        profile_path = files[1]
        edit = lambda: profile_path.write_text("name: spring-boot\nconfig_file: other\n", encoding="utf-8")  # noqa: E731
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0], edit_on_measure=1, edit=edit).run())
        assert result.aborted and str(profile_path) in result.abort_reason
        assert result.experiments == []

    def test_a_deleted_file_counts_as_changed(self, files):
        profile_path = files[1]
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0], edit_on_measure=1, edit=profile_path.unlink).run())
        assert result.aborted and str(profile_path) in result.abort_reason

    def test_rewriting_identical_bytes_is_not_a_change(self, files):
        sla_path = files[0]
        same = lambda: sla_path.write_bytes(sla_path.read_bytes())  # noqa: E731
        result = asyncio.run(_guarded(files, p99s=[300.0, 58.0], edit_on_measure=2, edit=same).run())
        assert not result.aborted


class TestAPausedCampaignCannotResumeOnEditedFiles:
    def _paused(self, files, campaign):
        sla_path, profile_path, *_ = files
        state = paused_state_path(campaign.state_dir, campaign.run_id)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({
            "guarded_files": {str(sla_path): _sha(sla_path), str(profile_path): _sha(profile_path)},
            "current_snapshot": {}, "current_load": {}, "baseline": {}, "experiments": [],
        }), encoding="utf-8")

    def test_an_edit_while_paused_refuses_resume_and_names_the_file(self, files):
        sla_path = files[0]
        campaign = _guarded(files, p99s=[300.0, 58.0])
        self._paused(files, campaign)
        sla_path.write_text(SLA_YAML.replace("p99_ms: 120", "p99_ms: 400"), encoding="utf-8")
        with pytest.raises(CampaignRefused, match="changed while it was paused"):
            asyncio.run(campaign.resume())


def test_build_campaign_guards_the_files_it_loaded(tmp_path, monkeypatch):
    from pathlib import Path

    from crucible.perf.commands import build_campaign

    repo = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo)
    (tmp_path / "state").mkdir()
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
        approve_mode="file", approval_timeout_s=1, model="m", provider="p",
    )
    assert {p.name for p in campaign.guarded_files} == {"slo.yaml", "spring-boot.yaml"}
