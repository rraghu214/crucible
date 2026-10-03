"""Manual revert and manual DB reset (§11).

§11: "Restart, revert, deploy and database reset can each be switched to
manual when automation isn't possible. The campaign blocks with instructions
rather than failing. Every manual step is recorded on the manifest."

Key behaviours:
- manual_db_reset_instructions: campaign blocks before each experiment and
  waits for operator confirmation before proceeding.
- manual_revert: when a change must be undone, the campaign blocks with
  revert instructions instead of auto-applying the inverse.
- Both record the step on the manifest — the run is not comparable to an
  autonomous one.
- A timeout on either raises CampaignAborted (not silently continuing).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from crucible.perf.approval import ManualStep
from crucible.perf.campaign import Campaign, Sla
from crucible.perf.runner import LoadResult, Scenario

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


class _FakeGit:
    def __init__(self):
        self.n = 0

    def __call__(self, argv):
        if argv[1] == "commit":
            self.n += 1
        if argv[1] == "rev-parse":
            return 0, f"sha{self.n:04d}"
        return 0, ""


class _ApprovingDiagnoser:
    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings="", stakeholder_request=""):
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


class _DenyingDiagnoser:
    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings="", stakeholder_request=""):
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


def _auto_confirm(state_dir: Path, run_id: str, experiment: int, delay_s: float = 0.05) -> None:
    """Write a manual-step confirmation file from a background thread after delay_s."""
    def _write():
        time.sleep(delay_s)
        step = ManualStep(
            run_id=run_id,
            experiment=experiment,
            instructions="",
            state_dir=state_dir,
        )
        path = step.confirmation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"responder": "test-thread", "confirmed_at_epoch_s": time.time()}),
            encoding="utf-8",
        )

    t = threading.Thread(target=_write, daemon=True)
    t.start()


def _build_campaign(
    profile, sla, workspace, *,
    p99s: list[float],
    approve: bool = True,
    manual_db_reset_instructions: str = "",
    manual_revert: bool = False,
    state_dir: Path | None = None,
    max_experiments: int = 1,
) -> Campaign:
    from crucible.perf.applicator import Applicator
    from crucible.perf.approval import DenyingGate, PreapprovedGate

    seq = iter(p99s)

    def _measure(scenario, run_id):
        p99 = next(seq)
        return (
            LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=p99, error_rate_pct=0.0),
            {"p99_ms": p99},
        )

    if state_dir is None:
        state_dir = Path(tempfile.mkdtemp())

    return Campaign(
        profile=profile,
        sla=sla,
        scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
        applicator=Applicator(profile=profile, workspace=workspace, _git=_FakeGit()),
        diagnoser=_ApprovingDiagnoser(),
        measure=_measure,
        approval_gate=PreapprovedGate() if approve else DenyingGate(),
        max_experiments=max_experiments,
        state_dir=state_dir,
        results_dir=state_dir / "results",
        journal=None,
        manual_db_reset_instructions=manual_db_reset_instructions,
        manual_revert=manual_revert,
    )


# ---------------------------------------------------------------------------
# Manual DB reset
# ---------------------------------------------------------------------------


class TestManualDbReset:
    """The campaign blocks before each experiment and waits for operator confirmation."""

    def test_manual_db_reset_blocks_until_confirmed(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],
            manual_db_reset_instructions="truncate orders; truncate line_items;",
            state_dir=state_dir,
            max_experiments=1,
        )
        # Auto-confirm the DB reset step for experiment 1 from a background thread.
        _auto_confirm(state_dir, campaign.run_id, 1, delay_s=0.05)

        result = asyncio.run(campaign.run())

        assert result.experiments
        notes_all = " ".join(result.experiments[0].notes)
        assert "DB reset" in notes_all or "manual_steps" in str(result.experiments[0].manual_steps)

    def test_manual_db_reset_recorded_on_manifest(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],
            manual_db_reset_instructions="truncate orders;",
            state_dir=state_dir,
            max_experiments=1,
        )
        _auto_confirm(state_dir, campaign.run_id, 1, delay_s=0.05)

        result = asyncio.run(campaign.run())

        manifest = result.experiments[0]
        assert any("DB reset" in s for s in manifest.manual_steps)

    def test_manual_db_reset_timeout_aborts_campaign(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],
            manual_db_reset_instructions="truncate orders;",
            state_dir=state_dir,
            max_experiments=1,
        )
        # Override timeout to very short — nobody confirms.
        campaign.manual_step_timeout_s = 0.01

        result = asyncio.run(campaign.run())

        assert result.aborted is True

    def test_no_manual_db_reset_runs_without_blocking(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        # DenyingGate → no actual apply, just guard refusal turned proposal
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],
            approve=False,  # Operator says no — stops after one declined proposal
            state_dir=state_dir,
        )
        result = asyncio.run(campaign.run())
        # Campaign ended normally (operator declined), no blocking
        assert result.finished_at_epoch_s is not None


# ---------------------------------------------------------------------------
# Manual revert
# ---------------------------------------------------------------------------


class TestManualRevert:
    """When manual_revert=True, the campaign blocks for human confirmation on revert."""

    def test_manual_revert_blocks_when_verdict_is_not_improved(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        # p99s: baseline=300ms (fails SLA), after=310ms (WORSE → revert fires).
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 310.0],  # gets worse — WORSE verdict, triggers revert
            manual_revert=True,
            state_dir=state_dir,
        )
        # Auto-confirm the revert step from a background thread.
        _auto_confirm(state_dir, campaign.run_id, 1, delay_s=0.05)

        result = asyncio.run(campaign.run())

        assert result.experiments
        manifest = result.experiments[0]
        # The manifest should record the manual revert step
        assert any("revert" in s for s in manifest.manual_steps) or any(
            "revert" in n for n in manifest.notes
        )

    def test_manual_revert_timeout_aborts_campaign(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        # 310 > 300 → WORSE → revert → timeout
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 310.0],
            manual_revert=True,
            state_dir=state_dir,
        )
        campaign.manual_step_timeout_s = 0.01  # Nobody confirms

        result = asyncio.run(campaign.run())

        assert result.aborted is True

    def test_manual_revert_not_triggered_on_improved(self, profile, sla, workspace):
        """When the verdict is IMPROVED, the change is kept — manual revert does not fire."""
        state_dir = Path(tempfile.mkdtemp())
        campaign = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],  # big improvement — IMPROVED, change is kept
            manual_revert=True,
            state_dir=state_dir,
        )
        result = asyncio.run(campaign.run())

        # Campaign finished (SLA met), no revert happened
        assert result.finished_at_epoch_s is not None
        assert result.experiments[0].kept is True
