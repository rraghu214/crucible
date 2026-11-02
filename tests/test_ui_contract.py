"""What the page sends is what the server accepts. Group 53.

DRAFTED by Claude Code, 2 November 2026. REVIEWED AND APPROVED by the operator, 2 November 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 53 for the questions to check.

Trial, 2 November 2026: the dashboard's Approve failed with HTTP 422. The server's
tests posted a hand-written, correct body ({"action": "approve", "args": ...}); the
page sent {approved: true}. Page and server were each tested alone, never together.
These tests take the request body out of the page's own source, fill it with what
the server itself returned, and send it to the real route. What is asserted:

1. Approve and Reject, as the page builds them, are accepted and bound;
2. the command printed on the card and in the log parses as the real CLI;
3. a paused run is listed and resumable, with higher caps;
4. the wizard opens with the saved scenarios and request templates;
5. the run log says when a decision is taken, and each campaign stage prints a line;
6. an error the server returns is shown as text, never "[object Object]".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.approval import ApprovalRequest, FileApprovalGate
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext

REPO = Path(__file__).resolve().parents[1]
PAGE = (REPO / "crucible" / "ui" / "client" / "crucible-ui.html").read_text(encoding="utf-8")
POOL = "spring.datasource.hikari.maximum-pool-size"
AUTH = {"Authorization": "Bearer tok"}


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> PerfContext:
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
    (tmp_path / "results").mkdir()
    return PerfContext(root=REPO, results_dir=str(tmp_path / "results"), state_dir=tmp_path / "state")


def _client(ctx: PerfContext) -> TestClient:
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = ctx
    return TestClient(app)


def _park(ctx: PerfContext, run_id="run-live") -> Path:
    lock = ctx.state / "locks" / "perftest_sandbox.lock"
    lock.mkdir(parents=True)
    (lock / "holder.json").write_text(json.dumps({"run_id": run_id, "since_epoch_s": 1_759_000_000.0}))
    request = ApprovalRequest(run_id=run_id, experiment=1, summary=f"{POOL} -> 50", params={POOL: 50},
                              cause_family="connection_pool_exhaustion")
    run_dir = ctx.state / "approvals" / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "001.request.json"
    path.write_text(json.dumps(request.as_dict()))
    return path


def _page_body(function: str, url_fragment: str, values: dict[str, object]) -> dict:
    """The object literal ``function`` passes to apiPost for ``url_fragment``, as JSON.

    Each identifier in ``values`` is replaced by what the page would hold at that
    moment, taken from the server's own response by the caller.
    """
    source = PAGE[PAGE.index(f"function {function}("):]
    source = source[:source.index("\n}\n")]
    literal = re.search(re.escape(url_fragment) + r"[^,]*,(\{[^}]*\})\)", source).group(1)
    for name, value in values.items():
        literal = literal.replace(name, json.dumps(value))
    literal = re.sub(r"([{,])\s*([A-Za-z_]\w*)\s*:", r'\1"\2":', literal)
    return json.loads(literal.replace("'", '"'))


# -- 53.1 ---------------------------------------------------------------------


class TestTheDecisionThePageSendsIsAccepted:
    def test_approve_as_the_page_builds_it_is_accepted_and_bound(self, ctx):
        request_file = _park(ctx)
        client = _client(ctx)
        shown = client.get("/v1/perf/campaign/active", headers=AUTH).json()["approvals"][0]["params"]
        body = _page_body("doApprove", "/decision'", {"_pendingParams": shown})

        response = client.post("/v1/perf/runs/run-live/experiments/1/decision", json=body, headers=AUTH)

        assert response.status_code == 200, response.text
        decision = json.loads(request_file.with_name("001.decision.json").read_text())
        assert decision["action"] == "approve" and decision["params"] == {POOL: 50}

    def test_reject_as_the_page_builds_it_is_accepted(self, ctx):
        request_file = _park(ctx)
        body = _page_body("doReject", "/decision'", {})

        response = _client(ctx).post("/v1/perf/runs/run-live/experiments/1/decision", json=body, headers=AUTH)

        assert response.status_code == 200, response.text
        assert json.loads(request_file.with_name("001.decision.json").read_text())["action"] == "reject"

    def test_the_old_body_is_what_failed(self, ctx):
        """{approved: true}, which the page sent until now, is refused and writes nothing."""
        request_file = _park(ctx)
        response = _client(ctx).post("/v1/perf/runs/run-live/experiments/1/decision",
                                     json={"responder": "operator", "approved": True}, headers=AUTH)
        assert response.status_code == 422
        assert not request_file.with_name("001.decision.json").exists()


# -- 53.2 ---------------------------------------------------------------------


class TestThePrintedCommandIsTheRealCli:
    def _parses(self, line: str):
        from crucible.cli import build_parser

        return build_parser().parse_args(line.replace("<your name>", "raghu").split()[1:])

    def test_the_card_command_parses(self):
        template = re.search(r"cli\.textContent='(crucible approve )'\+\(data\.run_id\|\|''\)\+'([^']*)'"
                             r"\+\(ap\.experiment\?\?''\)\+'([^']*)'", PAGE)
        assert template, "the card's command line was not found"
        line = template.group(1) + "run-live" + template.group(2) + "1" + template.group(3)
        args = self._parses(line)
        assert args.run_id == "run-live" and args.experiment == 1 and args.responder == "raghu"

    def test_the_log_command_parses(self, tmp_path, capsys):
        gate = FileApprovalGate(run_id="run-live", state_dir=tmp_path, timeout_s=0.0,
                                _sleep=lambda s: None)
        with pytest.raises(Exception):  # times out at once: no decision
            gate.request(ApprovalRequest(run_id="run-live", experiment=1, summary="", params={POOL: 50}))
        line = next(x for x in capsys.readouterr().out.splitlines() if "crucible approve" in x)
        args = self._parses(line[line.index("crucible approve"):])
        assert args.experiment == 1 and args.responder == "raghu"


# -- 53.3 ---------------------------------------------------------------------


class TestAPausedRunCanBeResumedFromTheDashboard:
    def test_a_paused_run_is_listed_with_its_reason(self, ctx):
        (ctx.state / "pauses").mkdir(parents=True)
        (ctx.state / "pauses" / "run-p.paused.json").write_text("{}")
        Path(ctx.results_dir, "run-p.json").write_text(json.dumps({"run_id": "run-p", "pause_reason": "cost cap reached"}))

        data = _client(ctx).get("/v1/perf/campaign/active", headers=AUTH).json()

        assert data["active"] is False
        assert data["paused"] == [{"run_id": "run-p", "reason": "cost cap reached"}]

    def test_a_series_paused_between_scenarios_is_listed(self, ctx):
        (ctx.state / "series").mkdir(parents=True)
        (ctx.state / "series" / "s1.json").write_text(json.dumps({"id": "s1", "status": "paused: cap used up"}))
        assert _client(ctx).get("/v1/perf/campaign/active").json()["paused"] == [
            {"run_id": "s1", "reason": "paused: cap used up"}]

    def test_resume_as_the_page_builds_it_starts_the_cli_resume_with_the_new_caps(self, ctx, monkeypatch):
        started = []
        monkeypatch.setattr(perf_ui, "start_job", lambda state, cwd, args, label="": started.append(args) or
                            type("J", (), {"id": "j", "label": label, "status": "running", "started_at_epoch_s": 0.0,
                                           "exit_code": None, "command_line": ""})())
        source = PAGE[PAGE.index("async function doResume("):]
        assert "body.cost_cap_usd=Number(cost)" in source and "body.wall_clock_cap_h=Number(wall)" in source

        response = _client(ctx).post("/v1/perf/campaign/run-p/resume",
                                     json={"cost_cap_usd": 2.0, "wall_clock_cap_h": 1.5}, headers=AUTH)

        assert response.status_code == 200, response.text
        assert started[0][:2] == ["resume", "run-p"]
        assert "--cost-cap=2.0" in started[0] and "--wall-clock-cap=1.5" in started[0]


# -- 53.4 ---------------------------------------------------------------------


class TestTheWizardOpensWithWhatIsSaved:
    def test_saved_scenarios_and_templates_are_returned_in_the_rows_format(self, tmp_path):
        import yaml

        from crucible.ui.wizard import current

        data = yaml.safe_load((REPO / "config" / "slo.yaml").read_text(encoding="utf-8"))
        data["scenarios"] = [
            {"name": "burst_50u", "users": 50, "measure_s": 180, "spawn_rate": 3.333},
            {"name": "mixed", "users": 40, "measure_s": 300,
             "mix": {"/api/db": {"weight": 60, "p99_ms": 150}, "/api/orders": {"weight": 40}}}]
        sla = tmp_path / "slo.yaml"
        sla.write_text(yaml.safe_dump(data), encoding="utf-8")
        values = current(sla, REPO / "config" / "profiles" / "spring-boot.yaml", REPO / "locust" / "locustfile.py")

        assert values["scenarios"] == [
            {"name": "burst_50u", "users": 50, "duration_s": 180, "ramp_s": 15, "mix": ""},
            {"name": "mixed", "users": 40, "duration_s": 300, "ramp_s": 0, "mix": "/api/db=60@150, /api/orders=40"}]

    def test_the_mix_text_round_trips_through_the_wizards_parser(self):
        from crucible.ui.perf_ui import parse_mix_text
        from crucible.ui.wizard import _scenario_row

        mix = {"/api/db": {"weight": 60, "p99_ms": 150.0}, "/api/orders": {"weight": 40, "p99_ms": None}}
        row = _scenario_row({"name": "m", "users": 10, "measure_s": 60, "mix": mix})
        assert parse_mix_text(row["mix"]) == mix

    def test_the_experiment_count_is_saved_and_read_back(self, tmp_path):
        """Operator, 2 November 2026: save it in slo.yaml. A reload showed 5 after 2 was chosen."""
        import shutil

        from crucible.perf.campaign import Sla
        from crucible.ui.wizard import WizardForm, current, render

        sla = tmp_path / "slo.yaml"
        shutil.copy(REPO / "config" / "slo.yaml", sla)
        paths = (sla, REPO / "config" / "profiles" / "spring-boot.yaml", REPO / "locust" / "locustfile.py")
        values = current(*paths)
        form = WizardForm(endpoint=values["endpoint"], p99_ms=values["p99_ms"], error_rate_pct=values["error_rate_pct"],
                          users=values["users"], config_file=values["config_file"], base_branch=values["base_branch"],
                          restart_command=values["restart_command"], health_url=values["health_url"], max_experiments=2)
        sla.write_text(render(form, *paths)["sla"], encoding="utf-8")

        assert Sla.load(sla).max_experiments == 2
        assert current(*paths)["experiments"] == 2

    def test_a_file_without_the_plan_block_gets_one(self, tmp_path):
        import yaml

        from crucible.ui.wizard import WizardForm, current, render

        data = yaml.safe_load((REPO / "config" / "slo.yaml").read_text(encoding="utf-8"))
        data.pop("plan", None)
        sla = tmp_path / "slo.yaml"
        sla.write_text(yaml.safe_dump(data), encoding="utf-8")
        paths = (sla, REPO / "config" / "profiles" / "spring-boot.yaml", REPO / "locust" / "locustfile.py")
        values = current(*paths)
        assert values["experiments"] == 5  # nothing saved yet: the page's old default
        form = WizardForm(endpoint=values["endpoint"], p99_ms=values["p99_ms"], error_rate_pct=values["error_rate_pct"],
                          users=values["users"], config_file=values["config_file"], base_branch=values["base_branch"],
                          restart_command=values["restart_command"], health_url=values["health_url"], max_experiments=3)
        sla.write_text(render(form, *paths)["sla"], encoding="utf-8")
        assert current(*paths)["experiments"] == 3

    @pytest.mark.parametrize("bad", [0, 51, "2", True])
    def test_an_out_of_range_count_is_refused(self, tmp_path, bad):
        import yaml

        from crucible.perf.campaign import CampaignRefused, Sla

        data = yaml.safe_load((REPO / "config" / "slo.yaml").read_text(encoding="utf-8"))
        data["plan"] = {"max_experiments": bad}
        sla = tmp_path / "slo.yaml"
        sla.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(CampaignRefused, match="max_experiments"):
            Sla.load(sla)

    def test_the_page_builds_its_rows_from_the_saved_scenarios(self):
        source = PAGE[PAGE.index("async function loadWizard("):]
        source = source[:source.index("\n}\n")]
        assert "c.scenarios.forEach" in source and "_scenarioRow(s.name,s.users" in source
        assert "c.requests.forEach" in source
        assert "_setVal('wz-experiments',c.experiments)" in source


# -- 53.5 ---------------------------------------------------------------------


class TestTheLogKeepsTalking:
    def test_the_decision_is_logged_when_it_is_taken(self, tmp_path, capsys):
        from crucible.perf.approval import write_decision

        request = ApprovalRequest(run_id="run-live", experiment=1, summary="", params={POOL: 50})
        gate = FileApprovalGate(run_id="run-live", state_dir=tmp_path, timeout_s=5.0, _sleep=lambda s: None)
        gate.run_dir.mkdir(parents=True, exist_ok=True)
        gate.request_path(1).write_text(json.dumps(request.as_dict()))
        write_decision(tmp_path, "run-live", 1, action="approve", responder="raghu")

        gate.request(request)

        assert "[approval] experiment 1: approved by raghu" in capsys.readouterr().out

    def test_each_campaign_event_prints_one_line_with_every_field(self, tmp_path, capsys):
        """Operator, 2 November 2026: "whatever details are available of the run, just
        relay it out". Nested fields as compact JSON, nothing dropped or cut short."""
        from crucible.perf.campaign import Campaign

        campaign = Campaign.__new__(Campaign)
        campaign.run_id, campaign.on_event = "run-live", None
        long_reason = "x" * 300
        campaign._emit("applying", experiment=1, commit="ab645b2", reason=long_reason,
                       changes=[{"prop": POOL, "previous": "2", "value": 50}])
        out = capsys.readouterr().out
        assert out.strip() == (f"[campaign] applying: experiment=1, commit=ab645b2, reason={long_reason}, "
                               f'changes=[{{"prop":"{POOL}","previous":"2","value":50}}]')


# -- 53.6 ---------------------------------------------------------------------


def test_a_validation_error_is_shown_as_text():
    source = PAGE[PAGE.index("async function apiPost("):]
    source = source[:source.index("\n}\n")]
    assert "typeof d.detail==='string'" in source and "JSON.stringify(d.detail" in source
