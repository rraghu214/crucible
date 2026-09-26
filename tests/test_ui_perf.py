"""Campaign UI assertions -- the nineteen screens. New group 28.

DRAFTED by Claude Code, 26 September 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 28 for the questions to check.

`crucible/ui/perf_ui.py` renders `docs/crucible-screens-v2.html`'s nineteen
screens as declarative surfaces over the data on disk. What is asserted here is
not how the screens look -- that is the design file's job -- but the four
properties that would make a screen dishonest or unsafe:

1. every screen passes the same validator an untrusted agent's surface does;
2. the navigation is the design's, not a reinterpretation of it;
3. nothing from the mock-up's example data appears as though it were measured,
   and an absent campaign is declared rather than drawn as an empty "all clear";
4. Approve is bound to the parked parameters, needs the control token, and the
   catalog's action set is not widened to get there.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.approval import ApprovalRequest
from crucible.perf.collector import COLLECTOR_VERSION
from crucible.perf.verdicts import IMPROVED
from crucible.ui import perf_ui
from crucible.ui.catalog import COMPONENTS, REGISTERED_ACTIONS
from crucible.ui.perf_ui import SCREENS, PerfContext, build_screen

REPO = Path(__file__).resolve().parents[1]
DESIGN = REPO / "docs" / "crucible-screens-v2.html"
POOL = "spring.datasource.hikari.maximum-pool-size"


@pytest.fixture
def ctx(tmp_path) -> PerfContext:
    """The real config and fixtures, an empty state directory and journal."""
    (tmp_path / "results").mkdir()
    return PerfContext(root=REPO, results_dir=str(tmp_path / "results"), state_dir=tmp_path / "state")


def _texts(screen: dict) -> str:
    """Every value a screen shows, flattened -- data model and literal labels."""
    surface = screen["surface"]
    literals = [str(c.get(k, "")) for c in surface["components"] for k in ("title", "label", "columns", "labels")]
    return json.dumps(surface["dataModel"], ensure_ascii=False) + " " + " ".join(literals)


def _campaign(run_id="run-A", environment="oracle-ashburn-boxa", **overrides) -> dict:
    base = {
        "run_id": run_id, "profile": "spring-boot", "scenario": "db-latency",
        "collector_version": COLLECTOR_VERSION, "started_at_epoch_s": 1_759_000_000.0,
        "finished_at_epoch_s": 1_759_001_000.0,
        "sla": {"name": "perflab-db-latency", "p99_ms": 120, "noise_p99_spread_pct": 2.08,
                "noise_measured_on": "2026-09-12", "environment_name": environment,
                "environment_kind": "pre-prod", "at_load": {"users": 50}},
        "baseline": {"sla_met": False, "load": {"p50_ms": 1200, "p95_ms": 1300, "p99_ms": 1300, "rps": 35},
                     "snapshot": {"available_evidence": {"metrics": True, "traces": False}}},
        "models_used": ["gemini-3.5-flash-lite"], "ruled_out": ["gc_pressure"],
        "stopped_reason": "SLA met after experiment 1",
        "experiments": [{
            "experiment": 1, "cause_family": "connection_pool_exhaustion", "verdict": IMPROVED, "kept": True,
            "margin_over_noise": 45.2, "proposal": {"changes": [{"prop": POOL, "value": 20, "previous": "2"}]},
            "diagnosis": {"served_by_model": "gemini-3.5-flash-lite", "input_tokens": 4000, "output_tokens": 300},
            "approval": {"state": "approved"}, "apply_result": {"applied": True}, "deploy": {"verified": True},
            "verdict_reason": "p99 fell", "predicted_p99_ms": 140,
            "after": {"load": {"p50_ms": 63, "p95_ms": 78, "p99_ms": 93, "rps": 186}},
        }],
    }
    base.update(overrides)
    return base


def _park(ctx: PerfContext, run_id="run-live", params=None) -> Path:
    """A campaign holding its lock, with experiment 1 parked for approval."""
    lock = ctx.state / "locks" / "perftest_sandbox.lock"
    lock.mkdir(parents=True)
    (lock / "holder.json").write_text(json.dumps({"run_id": run_id, "since_epoch_s": 1_759_000_000.0}))
    request = ApprovalRequest(run_id=run_id, experiment=1, summary=f"{POOL} -> 20",
                              params=params or {POOL: 20}, cause_family="connection_pool_exhaustion")
    run_dir = ctx.state / "approvals" / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "001.request.json"
    path.write_text(json.dumps(request.as_dict()))
    return path


# ---------------------------------------------------------------------------
# 28.1 -- every screen is a valid surface, including with nothing to show
# ---------------------------------------------------------------------------


class TestEveryScreenPassesTheInjectionWall:
    @pytest.mark.parametrize("screen", SCREENS, ids=lambda s: f"{s.number}-{s.title}")
    def test_the_validator_rejects_nothing(self, screen, ctx):
        """The builder is trusted code, and its output is still validated
        before it is served -- the same stance routes.py takes with the run
        surface. A rejection here means a screen ships a component the client
        will silently drop."""
        built = build_screen(screen.number, ctx)

        assert built["rejections"] == []
        assert {c["type"] for c in built["surface"]["components"]} <= set(COMPONENTS)

    @pytest.mark.parametrize("screen", SCREENS, ids=lambda s: f"{s.number}-{s.title}")
    def test_no_screen_fails_on_an_empty_state_directory(self, screen, ctx):
        """Principle 2: screens 13-15 need a campaign and must say none is
        running, not crash. A traceback reads as "broken", which is a different
        claim from "idle"."""
        built = build_screen(screen.number, ctx)

        assert "could not be built" not in _texts(built)

    def test_a_builder_that_raises_becomes_a_notice_not_a_500(self, ctx, monkeypatch):
        def explode(_ctx, _q):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(perf_ui, "SCREENS", (perf_ui.Screen(1, "Home", "Structure", explode),))

        built = build_screen(1, ctx)

        assert "disk on fire" in _texts(built)
        assert built["rejections"] == []


# ---------------------------------------------------------------------------
# 28.2 -- the navigation is the design's
# ---------------------------------------------------------------------------


def _design_navigation() -> list[tuple[int, str, str]]:
    sidebar = DESIGN.read_text(encoding="utf-8").split("<nav", 1)[1].split("</nav>", 1)[0]
    out, group = [], ""
    for grp, number, title in re.findall(
        r'<div class="grp">([^<]+)</div>|<button class="nav[^"]*"[^>]*>(\d+) · ([^<]+)</button>', sidebar
    ):
        if grp:
            group = html.unescape(grp)
        else:
            out.append((int(number), html.unescape(title).strip(), group))
    return out


class TestTheNavigationIsTheDesigns:
    def test_nineteen_screens_in_the_designs_groups_and_order(self):
        """Read from the design file itself, so a renamed or regrouped screen
        in either place fails here rather than drifting quietly -- the failure
        AGENTS.md records for two copies of the assertions doc."""
        ours = [(s.number, s.title, s.group) for s in SCREENS]

        assert ours == _design_navigation()
        assert len(ours) == 19


# ---------------------------------------------------------------------------
# 28.3 -- nothing is invented; absence is declared
# ---------------------------------------------------------------------------

#: Distinctive example values from the mock-up. None of them is data.
MOCKUP_EXAMPLES = ("payments-api", "orders-api", "pricing-svc", "checkout latency", "cmp-2026-1005-a41f",
                   "38 / 40", "1,300", "preprod-1", "acme/")


class TestNothingFromTheMockUpIsShownAsData:
    @pytest.mark.parametrize("screen", SCREENS, ids=lambda s: f"{s.number}-{s.title}")
    def test_no_example_figure_appears(self, screen, ctx):
        """The design file is a picture of what a screen holds. Rendering its
        example numbers would be principle 1's failure exactly: a claim with no
        measurement behind it, looking identical to one with."""
        shown = _texts(build_screen(screen.number, ctx))

        leaked = [example for example in MOCKUP_EXAMPLES if example in shown]
        assert leaked == []


class TestAbsenceIsDeclaredNotDrawnAsZero:
    def test_live_campaign_says_none_is_running(self, ctx):
        assert "No campaign is running" in _texts(build_screen(13, ctx))

    def test_report_and_history_say_there_are_no_manifests(self, ctx):
        assert "No campaign manifests" in _texts(build_screen(16, ctx))
        assert "No campaign manifests" in _texts(build_screen(17, ctx))

    def test_a_class_with_no_task_is_not_measured_rather_than_zero(self, ctx):
        """0 / 0 and "not measured" are different statements. There is no class
        B or E task in config/tasks/, and the screen must not read as a
        failed class."""
        shown = _texts(build_screen(18, ctx))

        assert "0 / 0" not in shown
        assert "not measured" in shown

    def test_the_quadrant_is_not_yet_measured_without_a_live_campaign(self, ctx):
        """Replay cannot fill the 2x2: every cell needs a measured fix."""
        shown = _texts(build_screen(18, ctx))

        assert shown.count("not yet measured -- no scored live campaign") == 4

    def test_playbooks_are_not_built_rather_than_zero(self, ctx):
        """A count of 0 would claim somebody looked and found none."""
        assert "not built" in _texts(build_screen(1, ctx))


# ---------------------------------------------------------------------------
# 28.4 -- screens that read campaigns read the manifests
# ---------------------------------------------------------------------------


class TestAfterwardsScreensReadTheJournal:
    def test_the_report_renders_the_manifests_own_headline_and_limits(self, ctx):
        Path(ctx.results_dir, "run-A.json").write_text(json.dumps(_campaign()))

        shown = _texts(build_screen(16, ctx))

        assert "run-A" in shown
        assert "93" in shown
        assert "trace" in shown.lower(), "limits_of_this_result must say traces were never checked"

    def test_history_refuses_a_comparison_across_environments(self, ctx):
        """DESIGN.md section 8, through report.compare -- the UI adds no second
        comparability rule of its own."""
        Path(ctx.results_dir, "run-A.json").write_text(json.dumps(_campaign("run-A")))
        Path(ctx.results_dir, "run-B.json").write_text(
            json.dumps(_campaign("run-B", environment="local", started_at_epoch_s=1_759_100_000.0)))

        shown = _texts(build_screen(17, ctx, {"a": "run-A", "b": "run-B"}))

        assert "NOT COMPARABLE" in shown

    def test_a_replay_file_in_results_is_named_not_rendered_as_a_campaign(self, ctx):
        Path(ctx.results_dir, "replay.json").write_text(json.dumps({"cases": []}))

        shown = _texts(build_screen(17, ctx))

        assert "replay.json" in shown
        assert "not a campaign manifest" in shown


# ---------------------------------------------------------------------------
# 28.5 -- Approve is bound, gated, and the action set is not widened
# ---------------------------------------------------------------------------


def _client(ctx: PerfContext) -> TestClient:
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = ctx
    return TestClient(app)


class TestApprovalFromTheBrowserIsBound:
    def test_the_card_is_bound_to_the_parked_params(self, ctx):
        _park(ctx)

        surface = build_screen(13, ctx)["surface"]
        card = next(c for c in surface["components"] if c["type"] == "ApprovalCard")

        assert card["confirm"]["args"] == card["params"], "confirm must send exactly what the card shows"
        assert surface["dataModel"][card["params"]["$bind"][1:]] == {POOL: 20}

    def test_approving_different_values_is_refused_and_writes_nothing(self, ctx, monkeypatch):
        """A compromised client widening 20 to 100 is refused by
        hitl.decide_resume, the same check the CLI's file gate runs."""
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
        request_file = _park(ctx)

        response = _client(ctx).post(
            "/v1/perf/runs/run-live/experiments/1/decision",
            json={"action": "approve", "args": {POOL: 100}}, headers={"Authorization": "Bearer tok"},
        )

        assert response.status_code == 409
        assert not request_file.with_name("001.decision.json").exists()

    def test_a_matching_approval_writes_the_requests_values(self, ctx, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
        request_file = _park(ctx)

        response = _client(ctx).post(
            "/v1/perf/runs/run-live/experiments/1/decision",
            json={"action": "approve", "args": {POOL: 20}}, headers={"Authorization": "Bearer tok"},
        )

        assert response.status_code == 200
        decision = json.loads(request_file.with_name("001.decision.json").read_text())
        assert decision["params"] == {POOL: 20}
        assert decision["responder"].startswith("ui:")

    def test_a_decided_proposal_cannot_be_decided_again(self, ctx, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
        _park(ctx)
        client = _client(ctx)
        headers = {"Authorization": "Bearer tok"}
        client.post("/v1/perf/runs/run-live/experiments/1/decision",
                    json={"action": "reject"}, headers=headers)

        again = client.post("/v1/perf/runs/run-live/experiments/1/decision",
                            json={"action": "approve", "args": {POOL: 20}}, headers=headers)

        assert again.status_code == 409

    def test_no_token_configured_fails_closed(self, ctx, monkeypatch):
        """auth.py's rule: an unset token is a closed door, never an open one."""
        monkeypatch.delenv("CRUCIBLE_CONTROL_TOKEN", raising=False)
        _park(ctx)

        response = _client(ctx).post("/v1/perf/runs/run-live/experiments/1/decision",
                                     json={"action": "approve", "args": {POOL: 20}})

        assert response.status_code == 503

    def test_a_wrong_token_is_refused(self, ctx, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
        _park(ctx)

        response = _client(ctx).post("/v1/perf/runs/run-live/experiments/1/decision",
                                     json={"action": "approve", "args": {POOL: 20}},
                                     headers={"Authorization": "Bearer nope"})

        assert response.status_code == 401


class TestTheActionSetIsNotWidened:
    def test_the_registered_actions_are_unchanged(self):
        """Abort and Pause are shown as CLI commands rather than buttons. A
        browser Abort would need a new registered action, and that widens the
        event invariant for every surface an agent can compose."""
        assert REGISTERED_ACTIONS == frozenset({"approve", "reject", "rerun", "request_data"})

    def test_abort_is_offered_as_the_command_that_does_it(self, ctx):
        _park(ctx)

        assert "crucible abort run-live" in _texts(build_screen(13, ctx))


@pytest.fixture
def offline_probes(monkeypatch):
    """Preflight's two network probes, stubbed. These tests are about which
    checks run, not whether Box A answers from wherever the suite runs."""
    from crucible.perf import commands

    monkeypatch.setattr(commands, "_check_metrics",
                        lambda *a, **k: commands.PreflightCheck("metrics reachable", False, "stub", blocking=False))
    monkeypatch.setattr(commands, "_check_version_endpoint",
                        lambda *a, **k: commands.PreflightCheck("target reports its commit", False, "stub",
                                                                blocking=False))
    return commands


class TestPreflightShowsTheCLIsChecks:
    def test_the_screen_and_the_cli_run_one_list_of_checks(self, ctx, offline_probes, capsys):
        """preflight_checks was split out of cmd_preflight so the screen cannot
        grow a second, drifting list."""
        commands = offline_probes

        shown = _texts(build_screen(12, ctx, {"run": "1"}))
        commands.cmd_preflight(sla_path=str(REPO / "config" / "slo.yaml"))
        printed = capsys.readouterr().out

        for name in re.findall(r"\[(?:PASS|FAIL|warn)\] ([^:]+):", printed):
            if name in ("profile loads", "SLA loads"):
                continue  # the screen states these by rendering at all
            assert name.replace(str(REPO / "config" / "slo.yaml"), "") in shown.replace(
                str(REPO / "config" / "slo.yaml"), ""), name

    def test_the_restarting_rehearsal_is_never_offered_from_the_browser(self, ctx, offline_probes):
        """--apply-probe restarts the target. It stays a deliberate CLI act."""
        shown = _texts(build_screen(12, ctx, {"run": "1", "apply_probe": "1"}))

        assert "skipped (pass --apply-probe" in shown
