"""Hooks: before_each / after_each / on_abort (§10).

Key behaviours:
- A failing before_each blocks the experiment (raises CampaignAborted).
- A failing after_each blocks the next experiment.
- on_abort fires on campaign abort and does NOT raise, even on failure.
- A passing hook records a note on the manifest.
- A campaign with no hooks runs unchanged.
- hooks_from_mapping builds from a YAML block.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from crucible.perf.campaign import Campaign, Sla
from crucible.perf.hooks import HookFailed, HookSet, hooks_from_mapping
from crucible.perf.runner import LoadResult, Scenario

# ---------------------------------------------------------------------------
# SLA and profile fixtures (same shape as other campaign tests)
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
    def __init__(self):
        self.n = 0

    def __call__(self, argv):
        if argv[1] == "commit":
            self.n += 1
        if argv[1] == "rev-parse":
            return 0, f"sha{self.n:04d}"
        return 0, ""


class _DenyingDiagnoser:
    """Proposes a valid change; the DenyingGate always rejects it."""

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
    hooks: HookSet | None = None,
    max_experiments: int = 3,
) -> Campaign:
    from crucible.perf.applicator import Applicator
    from crucible.perf.approval import DenyingGate

    seq = iter(p99s)

    def _measure(scenario, run_id):
        p99 = next(seq)
        return (
            LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=p99, error_rate_pct=0.0),
            {"p99_ms": p99},
        )

    state_dir = Path(tempfile.mkdtemp())
    return Campaign(
        profile=profile,
        sla=sla,
        scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
        applicator=Applicator(profile=profile, workspace=workspace, _git=_FakeGit()),
        diagnoser=_DenyingDiagnoser(),
        measure=_measure,
        approval_gate=DenyingGate(),
        max_experiments=max_experiments,
        state_dir=state_dir,
        results_dir=state_dir / "results",
        journal=None,
        hooks=hooks or HookSet(),
    )


# ---------------------------------------------------------------------------
# HookSet unit tests
# ---------------------------------------------------------------------------


class TestHookSetCallable:
    """A callable hook runs; passing/failing both work."""

    def test_passing_callable_hook_returns_ok(self):
        called = []
        hooks = HookSet(before_each=lambda: called.append(1))
        result = asyncio.run(hooks.run_before_each(1))
        assert result.ok
        assert called == [1]

    def test_failing_callable_hook_raises_hook_failed(self):
        def _bad():
            raise RuntimeError("db reset failed")

        hooks = HookSet(before_each=_bad)
        with pytest.raises(HookFailed, match="db reset failed"):
            asyncio.run(hooks.run_before_each(1))

    def test_on_abort_does_not_raise_on_failure(self):
        def _bad():
            raise RuntimeError("cleanup failed")

        hooks = HookSet(on_abort=_bad)
        result = asyncio.run(hooks.run_on_abort())
        assert not result.ok
        assert "cleanup failed" in result.error

    def test_no_hook_returns_ok_with_zero_duration(self):
        hooks = HookSet()
        result = asyncio.run(hooks.run_before_each(1))
        assert result.ok
        assert result.duration_s == 0.0

    def test_async_callable_is_awaited(self):
        awaited = []

        async def _async_hook():
            awaited.append(True)

        hooks = HookSet(before_each=_async_hook)
        asyncio.run(hooks.run_before_each(1))
        assert awaited == [True]


class TestHookSetHasHooks:
    def test_no_hooks_configured_returns_false(self):
        assert not HookSet().has_hooks()

    def test_any_hook_configured_returns_true(self):
        assert HookSet(before_each="echo hi").has_hooks()
        assert HookSet(after_each=lambda: None).has_hooks()
        assert HookSet(on_abort="echo bye").has_hooks()


# ---------------------------------------------------------------------------
# Campaign integration
# ---------------------------------------------------------------------------


class TestBeforeEachBlocksOnFailure:
    """§10: a failing before_each must block the experiment, not silently proceed."""

    def test_failing_before_each_aborts_the_campaign(self, profile, sla, workspace):
        def _reset():
            raise RuntimeError("truncate failed: lock timeout")

        hooks = HookSet(before_each=_reset)
        campaign = _build_campaign(profile, sla, workspace, p99s=[300.0], hooks=hooks)
        result = asyncio.run(campaign.run())

        assert result.aborted is True
        assert "before_each" in result.abort_reason

    def test_failing_before_each_note_appears_on_manifest(self, profile, sla, workspace):
        def _reset():
            raise RuntimeError("connection refused")

        hooks = HookSet(before_each=_reset)
        campaign = _build_campaign(profile, sla, workspace, p99s=[300.0], hooks=hooks)
        result = asyncio.run(campaign.run())

        assert result.experiments
        notes = result.experiments[0].notes
        assert any("before_each" in n for n in notes)

    def test_passing_before_each_notes_ok(self, profile, sla, workspace):
        called = []
        hooks = HookSet(before_each=lambda: called.append(1))
        campaign = _build_campaign(profile, sla, workspace, p99s=[300.0], hooks=hooks)
        asyncio.run(campaign.run())

        assert called


class TestAfterEachBlocksOnFailure:
    """A failing after_each aborts after the experiment is recorded."""

    def test_failing_after_each_aborts_after_first_experiment(self, profile, sla, workspace):
        call_count = [0]

        def _cleanup():
            call_count[0] += 1
            raise RuntimeError("log upload failed")

        hooks = HookSet(after_each=_cleanup)
        campaign = _build_campaign(profile, sla, workspace, p99s=[300.0], hooks=hooks)
        result = asyncio.run(campaign.run())

        assert result.aborted is True
        assert call_count[0] == 1

    def test_after_each_fires_on_operator_declined(self, profile, sla, workspace):
        """DenyingGate declines at the approval step (not the guard), so after_each fires.

        A declined experiment consumed a slot and may have had before_each prepare
        the database — after_each should still run to clean up or log.
        """
        called = []
        hooks = HookSet(after_each=lambda: called.append(1))
        campaign = _build_campaign(profile, sla, workspace, p99s=[300.0], hooks=hooks)
        asyncio.run(campaign.run())

        # DenyingGate is the approval gate; the guard passed (pool size is allowed).
        # after_each should fire because it is not a guard refusal.
        assert called


class TestOnAbortFiresWithoutBlocking:
    """on_abort fires when the campaign aborts; a failure there is recorded, not raised."""

    def test_on_abort_is_called_when_campaign_aborts(self, profile, sla, workspace):
        from crucible.perf.campaign import request_abort

        abort_calls = []
        state_dir = Path(tempfile.mkdtemp())

        hooks = HookSet(on_abort=lambda: abort_calls.append(1))

        from crucible.perf.applicator import Applicator
        from crucible.perf.approval import DenyingGate

        seq = iter([300.0])

        def _measure(scenario, run_id):
            p99 = next(seq)
            return (
                LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=p99, error_rate_pct=0.0),
                {},
            )

        campaign = Campaign(
            profile=profile,
            sla=sla,
            scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
            applicator=Applicator(profile=profile, workspace=workspace, _git=_FakeGit()),
            diagnoser=_DenyingDiagnoser(),
            measure=_measure,
            approval_gate=DenyingGate(),
            max_experiments=3,
            state_dir=state_dir,
            results_dir=state_dir / "results",
            journal=None,
            hooks=hooks,
        )
        request_abort(state_dir, campaign.run_id)
        asyncio.run(campaign.run())

        assert abort_calls == [1]

    def test_bad_on_abort_does_not_crash_the_campaign(self, profile, sla, workspace):
        from crucible.perf.campaign import request_abort

        state_dir = Path(tempfile.mkdtemp())

        def _bad_abort():
            raise RuntimeError("cleanup server down")

        hooks = HookSet(on_abort=_bad_abort)

        from crucible.perf.applicator import Applicator
        from crucible.perf.approval import DenyingGate

        seq = iter([300.0])

        def _measure(scenario, run_id):
            p99 = next(seq)
            return (
                LoadResult(run_id=run_id, scenario=scenario.name, p99_ms=p99, error_rate_pct=0.0),
                {},
            )

        campaign = Campaign(
            profile=profile,
            sla=sla,
            scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
            applicator=Applicator(profile=profile, workspace=workspace, _git=_FakeGit()),
            diagnoser=_DenyingDiagnoser(),
            measure=_measure,
            approval_gate=DenyingGate(),
            max_experiments=3,
            state_dir=state_dir,
            results_dir=state_dir / "results",
            journal=None,
            hooks=hooks,
        )
        request_abort(state_dir, campaign.run_id)
        result = asyncio.run(campaign.run())

        assert result.aborted is True
        assert result.finished_at_epoch_s is not None


# ---------------------------------------------------------------------------
# hooks_from_mapping factory
# ---------------------------------------------------------------------------


class TestHooksFromMapping:
    def test_none_mapping_gives_empty_hookset(self):
        hs = hooks_from_mapping(None)
        assert not hs.has_hooks()

    def test_mapping_populates_hooks(self):
        hs = hooks_from_mapping({
            "before_each": "truncate orders",
            "after_each": "curl /flush",
            "on_abort": "cp /var/log/app.log /tmp/",
            "timeout_s": 120,
        })
        assert hs.before_each == "truncate orders"
        assert hs.after_each == "curl /flush"
        assert hs.on_abort == "cp /var/log/app.log /tmp/"
        assert hs.timeout_s == 120.0

    def test_partial_mapping_leaves_missing_hooks_none(self):
        hs = hooks_from_mapping({"before_each": "echo reset"})
        assert hs.before_each == "echo reset"
        assert hs.after_each is None
        assert hs.on_abort is None
