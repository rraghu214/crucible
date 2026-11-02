"""Audit log (§13) and event stream (on_event callback).

Key behaviours:
- AuditLog appends one JSONL record per action with kind, run_id, experiment
  and at_epoch_s fields.
- Every significant action (guard refusal, approval, apply, revert, abort,
  pause) is recorded.
- on_event fires at key moments and does not raise on a bad callback.
- Structured abort/pause fields are set on CampaignResult.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from crucible.perf.campaign import (
    AuditLog,
    Campaign,
    CampaignResult,
    Sla,
    request_abort,
    request_pause,
)
from crucible.perf.runner import LoadResult, Scenario

# ---------------------------------------------------------------------------
# Shared fixtures (same SLA YAML as pause-resume tests)
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


class _FakeGit:
    """Injected so campaign tests never need a real git repo."""

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


def _build_campaign(
    profile, sla, workspace, *,
    p99s: list[float],
    approve: bool = True,
    max_experiments: int = 3,
    on_event=None,
    state_dir: Path | None = None,
) -> tuple[Campaign, list[float]]:
    from crucible.perf.applicator import Applicator
    from crucible.perf.approval import DenyingGate, PreapprovedGate  # noqa: F401

    measurements_taken: list[float] = []
    seq = iter(p99s)

    def _measure(scenario, run_id):
        p99 = next(seq)
        measurements_taken.append(p99)
        return (
            LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=p99, error_rate_pct=0.0),
            {"p99_ms": p99},
        )

    if state_dir is None:
        state_dir = Path(tempfile.mkdtemp())

    campaign = Campaign(
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
        on_event=on_event,
    )
    return campaign, measurements_taken


# ---------------------------------------------------------------------------
# AuditLog unit tests
# ---------------------------------------------------------------------------


class TestAuditLogAppendsRecords:
    """Each action type is written as a parseable JSONL line."""

    def test_guard_refusal_is_appended(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.guard_refusal(1, reason="property not allowed")

        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert record["kind"] == "guard_refusal"
        assert record["run_id"] == "run-1"
        assert record["experiment"] == 1
        assert "at_epoch_s" in record
        assert record["reason"] == "property not allowed"

    def test_approval_is_appended(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.approval(1, approved=True, reason="looks good")

        record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
        assert record["kind"] == "approval"
        assert record["approved"] is True

    def test_apply_is_appended(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.apply(1, prop="pool.size", old_value=2, new_value=20)

        record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
        assert record["kind"] == "apply"
        assert record["prop"] == "pool.size"
        assert record["old_value"] == 2
        assert record["new_value"] == 20

    def test_revert_is_appended(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.revert(1, reason="INCONCLUSIVE")

        record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
        assert record["kind"] == "revert"
        assert record["experiment"] == 1

    def test_abort_has_no_experiment_number(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.abort(reason="watchdog tripped")

        record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
        assert record["kind"] == "abort"
        assert record["experiment"] is None
        assert record["reason"] == "watchdog tripped"

    def test_multiple_records_are_separate_lines(self, tmp_path):
        log = AuditLog(tmp_path, "run-1")
        log.guard_refusal(1, reason="a")
        log.approval(1, approved=True)
        log.apply(1, prop="x", old_value=1, new_value=2)

        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        kinds = [json.loads(line)["kind"] for line in lines]
        assert kinds == ["guard_refusal", "approval", "apply"]

    def test_noop_log_writes_nothing(self, tmp_path):
        log = AuditLog(None, "run-1")
        log.guard_refusal(1, reason="irrelevant")
        # No file should be created
        assert not (tmp_path / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# Structured abort/pause fields on CampaignResult
# ---------------------------------------------------------------------------


class TestStructuredAbortField:
    """result.aborted and result.abort_reason are set when the campaign is aborted."""

    def test_abort_sets_aborted_true(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],  # baseline fails SLA, will get one experiment
            state_dir=state_dir,
        )
        request_abort(state_dir, campaign.run_id, reason="integration test")

        result: CampaignResult = asyncio.run(campaign.run())

        assert result.aborted is True
        assert "integration test" in result.abort_reason

    def test_normal_completion_leaves_aborted_false(self, profile, sla, workspace):
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            # baseline 300ms (fails), then after-apply 100ms (passes) for 1 experiment
            p99s=[300.0, 100.0],
        )
        result: CampaignResult = asyncio.run(campaign.run())

        assert result.aborted is False
        assert result.abort_reason == ""

    def test_abort_reason_included_in_as_dict(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],
            state_dir=state_dir,
        )
        request_abort(state_dir, campaign.run_id)
        result = asyncio.run(campaign.run())

        d = result.as_dict()
        assert "aborted" in d
        assert "abort_reason" in d


class TestStructuredPauseField:
    """result.paused and result.pause_reason are set when the campaign is paused."""

    def test_pause_sets_paused_true(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],
            state_dir=state_dir,
        )
        request_pause(state_dir, campaign.run_id, reason="deploy window")

        result = asyncio.run(campaign.run())

        assert result.paused is True
        assert "deploy window" in result.pause_reason

    def test_pause_and_abort_are_mutually_exclusive_for_paused(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],
            state_dir=state_dir,
        )
        request_pause(state_dir, campaign.run_id)
        result = asyncio.run(campaign.run())

        assert result.paused is True
        assert result.aborted is False


# ---------------------------------------------------------------------------
# Event stream (on_event callback)
# ---------------------------------------------------------------------------


class TestEventStream:
    """The on_event callback fires at key moments without affecting behaviour."""

    def test_baseline_done_event_fires(self, profile, sla, workspace):
        events: list[dict] = []
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            # baseline passes the SLA → campaign stops, nothing to diagnose
            p99s=[100.0],
            on_event=events.append,
        )
        asyncio.run(campaign.run())

        kinds = [e["kind"] for e in events]
        assert "baseline_done" in kinds

    def test_experiment_started_event_fires(self, profile, sla, workspace):
        events: list[dict] = []
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],  # baseline fails, one experiment
            on_event=events.append,
        )
        asyncio.run(campaign.run())

        kinds = [e["kind"] for e in events]
        assert "experiment_started" in kinds

    def test_verdict_event_fires_with_correct_fields(self, profile, sla, workspace):
        events: list[dict] = []
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],
            on_event=events.append,
        )
        asyncio.run(campaign.run())

        verdict_events = [e for e in events if e["kind"] == "verdict"]
        assert len(verdict_events) == 1
        assert "verdict" in verdict_events[0]
        assert "kept" in verdict_events[0]
        assert "p99_after" in verdict_events[0]

    def test_aborted_event_fires_on_abort(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        events: list[dict] = []
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],
            state_dir=state_dir,
            on_event=events.append,
        )
        request_abort(state_dir, campaign.run_id)
        asyncio.run(campaign.run())

        kinds = [e["kind"] for e in events]
        assert "aborted" in kinds

    def test_started_event_carries_sla_and_profile(self, profile, sla, workspace):
        events: list[dict] = []
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[100.0],
            on_event=events.append,
        )
        asyncio.run(campaign.run())

        started = next(e for e in events if e["kind"] == "started")
        assert "sla" in started
        assert started["profile"] == "spring-boot"

    def test_bad_callback_does_not_crash_the_campaign(self, profile, sla, workspace):
        """A callback that raises must not propagate into the campaign loop."""
        def _bad_callback(event):
            raise RuntimeError("callback exploded")

        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[100.0],  # baseline passes, campaign ends cleanly
            on_event=_bad_callback,
        )
        result = asyncio.run(campaign.run())

        # Campaign finished despite bad callback
        assert result.finished_at_epoch_s is not None

    def test_audit_log_records_abort(self, profile, sla, workspace):
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0],
            state_dir=state_dir,
        )
        request_abort(state_dir, campaign.run_id)
        asyncio.run(campaign.run())

        audit_path = state_dir / "audit.jsonl"
        assert audit_path.exists()
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").strip().split("\n")]
        kinds = [r["kind"] for r in records]
        assert "abort" in kinds

    def test_audit_log_records_approval(self, profile, sla, workspace):
        """An approved experiment leaves an 'approval' record in the audit log."""
        state_dir = Path(tempfile.mkdtemp())
        campaign, _ = _build_campaign(
            profile, sla, workspace,
            p99s=[300.0, 100.0],  # baseline fails, one experiment approved+measured
            approve=True,
            state_dir=state_dir,
        )
        asyncio.run(campaign.run())

        audit_path = state_dir / "audit.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").strip().split("\n")]
        kinds = [r["kind"] for r in records]
        assert "approval" in kinds
        assert "apply" in kinds
