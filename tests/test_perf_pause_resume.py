"""Pause/resume: §7 guarantees that pausing preserves verified experiments.

Key behaviours under test:
- A pause marker stops the loop at the next boundary (not mid-apply).
- The paused state is written to disk with the correct loop counters.
- The pause marker is removed from the state before resume re-enters.
- Resume continues from the saved experiment boundary without re-running the
  baseline measurement.
- request_pause / pause_requested are readable from a separate process.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from crucible.perf.campaign import (
    Campaign,
    CampaignRefused,
    Sla,
    pause_marker_path,
    pause_requested,
    paused_state_path,
    request_pause,
)
from crucible.perf.runner import LoadResult, Scenario

# ---------------------------------------------------------------------------
# SLA and profile fixtures
# ---------------------------------------------------------------------------

SLA_YAML = """\
name: perflab
environment:
  name: staging
  kind: pre-prod
  target_base_url: http://localhost:8080
objective:
  endpoint: /orders
  p99_ms: 150
  error_rate_pct: 5.0
noise:
  p99_spread_pct: 2.08
  measured_on: 2026-09-12
host_contention:
  cpu_steal_abort_pct: 10.0
"""

PROPERTIES = "spring.datasource.hikari.maximum-pool-size=2\n"


@pytest.fixture
def sla(tmp_path):
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML, encoding="utf-8")
    return Sla.load(path)


@pytest.fixture
def profile():
    from crucible.perf.profile import TargetProfile

    return TargetProfile.from_mapping({
        "name": "spring-boot",
        "runtime": "jvm",
        "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {
            "spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100},
        },
        "protected_paths": ["config/slo.yaml"],
        "deploy": {"branch": "perftest_sandbox"},
    })


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _DenyingDiagnoser:
    """Proposes an allowed change; the DenyingGate rejects it before apply."""

    async def diagnose(
        self, snapshot: dict[str, Any], sla: Any, *,
        ruled_out=(), prior_findings="", stakeholder_request=""
    ) -> Any:
        from crucible.perf.applicator import Change, Proposal
        from crucible.perf.diagnosis import Diagnosis

        return Diagnosis(
            proposal=Proposal(
                cause_family="connection_pool_exhaustion",
                changes=(Change(prop="spring.datasource.hikari.maximum-pool-size", value=5),),
                reasoning="test",
                confidence=0.8,
                predicted_p99_ms=120.0,
            ),
            provider="gemini",
            model="gemini-2.5-flash",
        )


def _build_campaign(
    profile,
    sla,
    workspace,
    *,
    p99s: list[float],
    max_experiments: int = 3,
    measure_fn=None,
) -> tuple[Campaign, list[float]]:
    """Return (campaign, measurements_taken_list).

    ``p99s`` is consumed one per call to ``measure``.  measurements_taken_list
    records each p99 as it is consumed, so tests can assert how many actual
    measurements happened.
    """
    from crucible.perf.applicator import Applicator
    from crucible.perf.approval import DenyingGate

    measurements_taken: list[float] = []
    seq = iter(p99s)

    def _default_measure(scenario, run_id):
        p99 = next(seq)
        measurements_taken.append(p99)
        load = LoadResult(
            run_id=run_id, scenario=scenario.name, p50_ms=p99 * 0.6, p99_ms=p99,
            mean_ms=p99 * 0.7, error_rate_pct=0.0, rps=100.0,
        )
        return load, {"p99_ms": p99, "collector_version": "1.2.0"}

    state = Path(tempfile.mkdtemp())
    campaign = Campaign(
        profile=profile,
        sla=sla,
        scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
        applicator=Applicator(profile=profile, workspace=workspace),
        diagnoser=_DenyingDiagnoser(),
        measure=measure_fn or _default_measure,
        approval_gate=DenyingGate(),
        max_experiments=max_experiments,
        state_dir=state,
        results_dir=state / "results",
        journal=None,
    )
    return campaign, measurements_taken


# ---------------------------------------------------------------------------
# Marker round-trips
# ---------------------------------------------------------------------------


class TestPauseMarkerRoundTrip:
    """The marker must be readable from a separate process — it is a file."""

    def test_request_pause_creates_a_readable_marker(self, tmp_path):
        request_pause(tmp_path, "run-x", "unrelated deploy underway")
        assert pause_requested(tmp_path, "run-x") == "unrelated deploy underway"

    def test_no_marker_returns_none(self, tmp_path):
        assert pause_requested(tmp_path, "run-x") is None

    def test_empty_reason_gets_a_default_string(self, tmp_path):
        request_pause(tmp_path, "run-x", "")
        assert pause_requested(tmp_path, "run-x") == "operator pause"

    def test_marker_path_is_in_the_pauses_subdirectory(self, tmp_path):
        path = pause_marker_path(tmp_path, "run-y")
        assert path.parent.name == "pauses"
        assert path.name == "run-y.pause"


# ---------------------------------------------------------------------------
# Pause during a running campaign
# ---------------------------------------------------------------------------


class TestPauseStopsTheLoop:
    """A pause marker halts the loop at the NEXT experiment boundary."""

    def test_pause_before_any_experiment_stops_immediately(
        self, profile, sla, workspace, tmp_path
    ):
        """A pause written before the campaign starts is seen at the first
        experiment boundary, before any experiment runs."""
        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id, "maintenance window")

        result = asyncio.run(campaign.run())

        assert result.experiments == []
        assert "maintenance window" in result.stopped_reason

    def test_pause_state_file_is_written_on_pause(self, profile, sla, workspace, tmp_path):
        """The loop state must be on disk so resume() can continue from it."""
        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id, "test pause")

        asyncio.run(campaign.run())

        state_file = paused_state_path(campaign.state_dir, campaign.run_id)
        assert state_file.exists(), "paused state file was not written"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["run_id"] == campaign.run_id
        assert "current_snapshot" in state
        assert "current_load" in state
        assert "baseline" in state

    def test_pause_state_records_correct_counters(self, profile, sla, workspace, tmp_path):
        """number_prev and charged_prev reflect only COMPLETED experiments."""
        campaign, _ = _build_campaign(
            profile, sla, workspace, p99s=[300.0] * 6, max_experiments=3
        )
        campaign.state_dir = tmp_path / "state"
        # Pause at the first boundary (0 experiments completed).
        request_pause(campaign.state_dir, campaign.run_id, "test")

        asyncio.run(campaign.run())

        state = json.loads(
            paused_state_path(campaign.state_dir, campaign.run_id).read_text(encoding="utf-8")
        )
        assert state["number_prev"] == 0
        assert state["charged_prev"] == 0

    def test_pause_is_not_abort_nothing_is_redeployed(
        self, profile, sla, workspace, tmp_path
    ):
        """Abort triggers a redeploy of the last good commit; pause must not."""
        deploys: list[str] = []

        class _TrackingDeployer:
            target = type("T", (), {"branch": "perftest_sandbox", "base_branch": "main"})()

            def deploy(self, commit, *, target_url=None):
                deploys.append(commit)
                from crucible.perf.deploy import DeployResult
                return DeployResult(commit=commit, deployed=True, verified=True)

        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        campaign.deployer = _TrackingDeployer()
        request_pause(campaign.state_dir, campaign.run_id, "no redeploy expected")

        asyncio.run(campaign.run())

        assert deploys == [], "pause must not trigger any redeploy"

    def test_baseline_is_still_measured_before_the_pause_check(
        self, profile, sla, workspace, tmp_path
    ):
        """The baseline is measured once, then the loop checks for pause at the
        first experiment boundary. The baseline measurement must not be skipped."""
        campaign, measurements = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id)

        asyncio.run(campaign.run())

        assert len(measurements) == 1, "exactly one baseline measurement before the pause"


# ---------------------------------------------------------------------------
# Resume from saved state
# ---------------------------------------------------------------------------


class TestResume:
    """resume() continues from the paused boundary, not from the baseline."""

    def test_resume_requires_a_paused_state_file(self, profile, sla, workspace, tmp_path):
        """Calling resume() on a run that was never paused is refused."""
        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"

        with pytest.raises(CampaignRefused, match="no paused state"):
            asyncio.run(campaign.resume())

    def test_resume_removes_the_pause_marker(self, profile, sla, workspace, tmp_path):
        """The pause marker must be cleared so the resumed campaign does not
        immediately re-pause at the first experiment boundary."""
        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id)
        asyncio.run(campaign.run())

        # Confirm the marker exists after the pause.
        assert pause_marker_path(campaign.state_dir, campaign.run_id).exists()

        # On resume the marker should be removed before the loop re-enters.
        asyncio.run(campaign.resume())

        assert not pause_marker_path(campaign.state_dir, campaign.run_id).exists()

    def test_resume_does_not_re_measure_the_baseline(
        self, profile, sla, workspace, tmp_path
    ):
        """The baseline was already measured in the first run. Resume must use
        the saved baseline rather than running another eight-minute load window
        (the whole point of pause over abort)."""
        campaign, measurements = _build_campaign(
            profile, sla, workspace, p99s=[300.0] * 10, max_experiments=3
        )
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id)

        asyncio.run(campaign.run())
        # Exactly one baseline measurement before the pause.
        assert len(measurements) == 1

        asyncio.run(campaign.resume())

        # After resume: still just 1 measurement (DenyingGate means the
        # experiment proposals are denied before the "after" measure runs,
        # so no extra measures are taken). The key assertion is that we did
        # NOT re-measure the baseline — which would make it 2.
        assert len(measurements) == 1, (
            "resume must not re-measure the baseline; "
            f"got {len(measurements)} total measurements: {measurements}"
        )

    def test_resumed_result_baseline_matches_the_paused_state(
        self, profile, sla, workspace, tmp_path
    ):
        """The CampaignResult written by resume() must carry the original
        baseline so the report can compare before/after correctly."""
        campaign, _ = _build_campaign(profile, sla, workspace, p99s=[300.0] * 6)
        campaign.state_dir = tmp_path / "state"
        request_pause(campaign.state_dir, campaign.run_id)

        first_result = asyncio.run(campaign.run())
        paused_baseline = first_result.baseline

        resumed_result = asyncio.run(campaign.resume())

        assert resumed_result.baseline == paused_baseline
