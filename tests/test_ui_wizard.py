"""The wizard writes the operator's SLA and deploy settings, then starts the run. Group 32.

DRAFTED by Claude Code, 23 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 32 for the questions to check.

Launch used to send only a run id: every value typed into the wizard was thrown
away and the campaign ran on whatever slo.yaml said. Operator decision: Launch
overwrites slo.yaml and writes the deploy fields to the profile; both stay
hand-editable. What is asserted:

1. edits are targeted: only the named keys change, comments survive, and a
   same-named key in another block is never the one replaced;
2. the result is re-validated by loading it as a campaign would, and read back;
3. nothing is written while a campaign holds a lock, for Advisory, without the
   token, or when the endpoint is one Locust does not drive;
4. Launch starts the run with the operator's values through the CLI path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.campaign import Sla
from crucible.perf.profile import TargetProfile
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext
from crucible.ui.wizard import MARKER, WizardForm, WizardRefused, render, set_value

REPO = Path(__file__).resolve().parents[1]
TOKEN = "test-control-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def root(tmp_path) -> Path:
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "locust", tmp_path / "locust")
    return tmp_path


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "config" / "slo.yaml", root / "config" / "profiles" / "spring-boot.yaml", root / "locust" / "locustfile.py"


def _form(**overrides) -> WizardForm:
    base = dict(endpoint="/api/orders", p99_ms=150.0, error_rate_pct=0.5, users=80,
                config_file="src/main/resources/application.properties", base_branch="develop-base",
                restart_command=["./mvnw", "spring-boot:run", "-Dspring-boot.run.profiles=lab"],
                health_url="http://localhost:8080/actuator/health", operator="operator:ui")
    base.update(overrides)
    return WizardForm(**base)


class TestTargetedEdits:
    def test_only_the_direct_child_is_replaced(self):
        text = "deploy:\n  branch: sandbox\n  base_branch: main  # read once\nrestart:\n  branch: other\n"
        out = set_value(text, ["deploy", "base_branch"], "trunk-base")
        assert out == 'deploy:\n  branch: sandbox\n  base_branch: "trunk-base"  # read once\nrestart:\n  branch: other\n'

    def test_a_key_in_another_block_is_not_matched(self):
        text = "a:\n  b:\n    users: 1\nusers: 2\n"
        assert set_value(text, ["users"], 9) == "a:\n  b:\n    users: 1\nusers: 9\n"

    def test_a_missing_key_or_block_value_is_refused(self):
        with pytest.raises(WizardRefused):
            set_value("a:\n  b: 1\n", ["a", "c"], 2)
        with pytest.raises(WizardRefused):
            set_value("a:\n  b: |\n    text\n", ["a", "b"], "x")

    def test_every_comment_in_the_real_files_survives(self, root):
        sla_path, profile_path, locustfile = _paths(root)
        rendered = render(_form(), sla_path, profile_path, locustfile)
        for old, new in ((sla_path.read_text(encoding="utf-8"), rendered["sla"]),
                         (profile_path.read_text(encoding="utf-8"), rendered["profile"])):
            old_comments = [ln for ln in old.splitlines() if ln.lstrip().startswith("#")]
            new_comments = [ln for ln in new.splitlines() if ln.lstrip().startswith("#") and not ln.startswith(MARKER)]
            assert new_comments == old_comments


class TestRevalidated:
    def test_the_rendered_files_load_with_the_operators_values(self, root, tmp_path):
        sla_path, profile_path, locustfile = _paths(root)
        rendered = render(_form(), sla_path, profile_path, locustfile)
        out = tmp_path / "out"
        out.mkdir()
        (out / "slo.yaml").write_text(rendered["sla"], encoding="utf-8")
        (out / "spring-boot.yaml").write_text(rendered["profile"], encoding="utf-8")
        sla, profile = Sla.load(out / "slo.yaml"), TargetProfile.load(out / "spring-boot.yaml")
        assert (sla.endpoint, sla.p99_ms, sla.error_rate_pct, sla.at_load["users"]) == ("/api/orders", 150, 0.5, 80)
        assert profile.deploy.base_branch == "develop-base"
        assert profile.restart.command == ["./mvnw", "spring-boot:run", "-Dspring-boot.run.profiles=lab"]
        assert sla.noise_p99_spread_pct == 2.08

    def test_render_writes_nothing(self, root):
        sla_path, profile_path, locustfile = _paths(root)
        before = (sla_path.read_bytes(), profile_path.read_bytes())
        render(_form(), sla_path, profile_path, locustfile)
        assert (sla_path.read_bytes(), profile_path.read_bytes()) == before

    def test_a_protected_base_branch_is_refused_by_the_profiles_own_rule(self, root):
        sla_path, profile_path, locustfile = _paths(root)
        with pytest.raises(WizardRefused, match="would not load"):
            render(_form(base_branch="perftest_sandbox"), sla_path, profile_path, locustfile)

    @pytest.mark.parametrize("endpoint", ["/transfer", "/api/dbx"])
    def test_an_endpoint_locust_does_not_drive_is_refused(self, root, endpoint):
        with pytest.raises(WizardRefused, match="drives"):
            render(_form(endpoint=endpoint), *_paths(root))


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=root, state_dir=root / "state")
    started: list[list[str]] = []

    def fake_start_job(state, cwd, args, label=""):
        started.append(list(args))
        return SimpleNamespace(id="job1", label=label, status="running", started_at_epoch_s=0.0,
                               exit_code=None, command_line=" ".join(args))

    monkeypatch.setattr(perf_ui, "start_job", fake_start_job)
    return TestClient(app), started


def _body(**overrides) -> dict:
    body = {"endpoint": "/api/orders", "p99_ms": 150, "error_rate_pct": 0.5, "users": 80,
            "config_file": "src/main/resources/application.properties", "base_branch": "main",
            "restart_command": "./mvnw spring-boot:run", "health_url": "http://localhost:8080/actuator/health",
            "mode": "Semi-Automated", "run_id": "wiz-1",
            "scenarios": [{"name": "steady_80u", "users": 80, "duration_s": 300, "ramp_s": 30},
                          {"name": "burst_160u", "users": 160, "duration_s": 180, "ramp_s": 15}]}
    body.update(overrides)
    return body


class TestLaunch:
    def test_it_writes_both_files_and_starts_the_run_with_the_first_scenario(self, client, root):
        tc, started = client
        response = tc.post("/v1/perf/wizard/launch", json=_body(), headers=AUTH)
        assert response.status_code == 200, response.text
        sla_path, profile_path, _ = _paths(root)
        assert Sla.load(sla_path).endpoint == "/api/orders"
        assert sla_path.read_text(encoding="utf-8").count(MARKER) == 1
        args = started[0]
        assert args[0] == "run" and f"--sla={sla_path}" in args and "--all-scenarios" in args
        assert "--approve=file" in args
        declared = Sla.load(sla_path).scenarios
        assert [(s["name"], s["users"], s["measure_s"], s["spawn_rate"]) for s in declared] == [
            ("steady_80u", 80, 300.0, 2.667), ("burst_160u", 160, 180.0, 10.667)]
        assert any("Runs 2 scenario(s) in order" in n for n in response.json()["notes"])

    def test_the_budget_steps_caps_reach_the_run(self, client):
        tc, started = client
        tc.post("/v1/perf/wizard/launch", json=_body(cost_cap_usd=0.1, wall_clock_cap_h=8), headers=AUTH)
        assert "--cost-cap=0.1" in started[0] and "--wall-clock-cap=8.0" in started[0]

    def test_fully_automated_is_preapproved(self, client):
        tc, started = client
        tc.post("/v1/perf/wizard/launch", json=_body(mode="Fully Automated"), headers=AUTH)
        assert "--approve=preapproved" in started[0]

    def test_a_ceiling_probe_starts_ceiling_with_the_operators_bounds(self, client):
        tc, started = client
        bounds = {"start_users": 50, "step": 25, "max_users": 300, "hold_s": 300}
        tc.post("/v1/perf/wizard/launch", json=_body(mode="Ceiling Probe", ceiling=bounds), headers=AUTH)
        assert started[0][0] == "ceiling" and "--max-users=300" in started[0]

    def test_a_second_launch_leaves_one_marker(self, client, root):
        tc, _ = client
        tc.post("/v1/perf/wizard/launch", json=_body(), headers=AUTH)
        tc.post("/v1/perf/wizard/launch", json=_body(p99_ms=140, run_id="wiz-2"), headers=AUTH)
        text = _paths(root)[0].read_text(encoding="utf-8")
        assert text.count(MARKER) == 1 and Sla.load(_paths(root)[0]).p99_ms == 140


class TestNothingIsWritten:
    def _unchanged(self, root, before):
        sla_path, profile_path, _ = _paths(root)
        assert (sla_path.read_bytes(), profile_path.read_bytes()) == before

    def _before(self, root):
        sla_path, profile_path, _ = _paths(root)
        return sla_path.read_bytes(), profile_path.read_bytes()

    def test_while_a_campaign_holds_a_lock(self, client, root):
        tc, started = client
        lock = root / "state" / "locks" / "perftest_sandbox.lock"
        lock.mkdir(parents=True)
        (lock / "holder.json").write_text('{"run_id": "live-7"}', encoding="utf-8")
        before = self._before(root)
        response = tc.post("/v1/perf/wizard/launch", json=_body(), headers=AUTH)
        assert response.status_code == 409 and "live-7" in response.json()["detail"]
        self._unchanged(root, before)
        assert started == []

    def test_save_is_allowed_mid_campaign_and_starts_nothing(self, client, root):
        """Operator decision, 24 October 2026: edits may land mid-run; the
        campaign takes them at its next boundary (Group 33). Launch would start a
        second campaign and stays refused; Save only writes."""
        tc, started = client
        lock = root / "state" / "locks" / "perftest_sandbox.lock"
        lock.mkdir(parents=True)
        (lock / "holder.json").write_text('{"run_id": "live-7"}', encoding="utf-8")
        response = tc.post("/v1/perf/wizard/save", json=_body(p99_ms=400), headers=AUTH)
        assert response.status_code == 200 and "live-7" in response.json()["note"]
        assert Sla.load(_paths(root)[0]).p99_ms == 400
        assert started == []
        assert tc.post("/v1/perf/wizard/save", json=_body(), headers={}).status_code == 401

    def test_while_a_campaign_is_paused(self, client, root):
        """Pausing releases the lock but has not stopped the campaign; resume
        reloads these files, so they stay frozen until it ends (Group 33)."""
        tc, started = client
        pauses = root / "state" / "pauses"
        pauses.mkdir(parents=True)
        (pauses / "held-3.paused.json").write_text("{}", encoding="utf-8")
        before = self._before(root)
        response = tc.post("/v1/perf/wizard/launch", json=_body(), headers=AUTH)
        assert response.status_code == 409 and "held-3 (paused)" in response.json()["detail"]
        self._unchanged(root, before)
        assert started == []

    def test_for_advisory_mode(self, client, root):
        tc, started = client
        before = self._before(root)
        assert tc.post("/v1/perf/wizard/launch", json=_body(mode="Advisory"), headers=AUTH).status_code == 409
        self._unchanged(root, before)
        assert started == []

    def test_without_the_token(self, client, root, monkeypatch):
        tc, started = client
        monkeypatch.delenv("CRUCIBLE_CONTROL_TOKEN")
        before = self._before(root)
        assert tc.post("/v1/perf/wizard/launch", json=_body(), headers=AUTH).status_code == 503
        self._unchanged(root, before)

    def test_for_an_undriven_endpoint(self, client, root):
        tc, started = client
        before = self._before(root)
        assert tc.post("/v1/perf/wizard/launch", json=_body(endpoint="/transfer"), headers=AUTH).status_code == 409
        self._unchanged(root, before)
        assert started == []

    def test_preview_returns_the_diff_and_writes_nothing(self, client, root):
        tc, _ = client
        before = self._before(root)
        body = tc.post("/v1/perf/wizard/preview", json=_body()).json()
        assert "+  endpoint: \"/api/orders\"" in body["sla_diff"]
        self._unchanged(root, before)


class TestBudget:
    def test_the_arithmetic_is_warmup_plus_measure_and_deploy_up_to_the_timeout(self):
        from crucible.ui.wizard import estimate

        est = estimate(mode="Semi-Automated", warmup_s=120, measure_s=300, experiments=3,
                       verify_timeout_s=300, per_call_costs=[0.001, 0.003])
        assert est["min_s"] == 420 + 420
        assert est["max_s"] == 420 + 3 * 300 + 3 * 420
        assert est["calls"] == 3 and est["cost"] == pytest.approx(0.006)

    def test_with_no_priced_call_on_record_cost_is_not_measured_not_zero(self):
        from crucible.ui.wizard import estimate

        est = estimate(mode="Semi-Automated", warmup_s=120, measure_s=300, experiments=3,
                       verify_timeout_s=300, per_call_costs=[])
        assert est["cost"] is None and all(r["cost"] is None for r in est["rows"])

    def test_a_ceiling_probe_calls_no_model(self):
        from crucible.ui.wizard import estimate

        est = estimate(mode="Ceiling Probe", warmup_s=120, measure_s=300, experiments=0,
                       verify_timeout_s=0, per_call_costs=[0.01], ceiling_steps=11)
        assert est["calls"] == 0 and est["max_s"] == 11 * 420

    def test_the_route_reads_the_profiles_verify_timeout(self, client):
        tc, _ = client
        d = tc.post("/v1/perf/wizard/budget", json=_body(experiments=2)).json()
        deploys = [r for r in d["rows"] if "Deploy" in r["phase"]]
        assert [r["phase"].split(" · ")[0] for r in deploys] == ["steady_80u", "burst_160u"]
        assert all(r["max_s"] == 2 * 300 for r in deploys)
        windows = (120 + 300) * 3 + (120 + 180) * 3
        assert d["max_s"] == windows + 2 * 2 * 300


def test_the_wizard_opens_with_the_files_values_and_the_driven_endpoints(client):
    tc, _ = client
    current = tc.get("/v1/perf/wizard").json()
    assert (current["endpoint"], current["p99_ms"], current["users"]) == ("/api/db", 120, 50)
    assert "/api/orders" in [e["endpoint"] for e in current["endpoints"]]
    assert current["restart_command"] == ["./mvnw", "spring-boot:run"]
