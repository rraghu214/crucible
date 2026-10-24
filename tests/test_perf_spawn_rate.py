"""Ramp becomes Locust's spawn rate. Group 35.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 35 for the questions to check.

The wizard's Ramp column was ignored: Locust always started users at its fixed
10 per second. Operator, 24 October 2026: "why 10 req/second? shouldn't it be
spawn rate?" It is the spawn rate (users started per second), and now it is
set. What is asserted:

1. `--spawn-rate` reaches the scenario; without it the SLA's at_load.spawn_rate
   is used, then 10;
2. a rate of zero or less, or not a number, is refused;
3. the wizard turns Ramp into users / ramp seconds, passes it to `run`, and
   records it in slo.yaml's at_load.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.campaign import Sla
from crucible.perf.commands import build_campaign, resolve_spawn_rate
from crucible.ui import actions as A
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext

REPO = Path(__file__).resolve().parents[1]


def _sla(tmp_path, spawn_line="    spawn_rate: 10\n"):
    text = (REPO / "config" / "slo.yaml").read_text(encoding="utf-8")
    if not spawn_line:
        text = text.replace("    spawn_rate: 10\n", "")
    else:
        text = text.replace("    spawn_rate: 10\n", spawn_line)
    path = tmp_path / "slo.yaml"
    path.write_text(text, encoding="utf-8")
    return Sla.load(path)


class TestResolution:
    def test_the_flag_wins(self, tmp_path):
        assert resolve_spawn_rate(2.5, _sla(tmp_path)) == 2.5

    def test_without_the_flag_the_slas_at_load_is_used(self, tmp_path):
        assert resolve_spawn_rate(None, _sla(tmp_path, "    spawn_rate: 4\n")) == 4.0

    def test_with_neither_it_is_lokusts_default_of_10(self, tmp_path):
        assert resolve_spawn_rate(None, _sla(tmp_path, "")) == 10.0

    @pytest.mark.parametrize("bad", [0, -1, "fast"])
    def test_a_rate_that_cannot_start_anyone_is_refused(self, tmp_path, bad):
        with pytest.raises(ValueError, match="spawn rate"):
            resolve_spawn_rate(bad, _sla(tmp_path))


def test_the_campaigns_scenario_carries_the_rate(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    (tmp_path / "state").mkdir()
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=80, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
        approve_mode="file", approval_timeout_s=1, model="m", provider="p", spawn_rate=2.67,
    )
    assert campaign.scenario.spawn_rate == 2.67


def test_the_action_builders_pass_it_as_one_token():
    assert "--spawn-rate=2.5" in A.run_args(spawn_rate=2.5)
    assert "--spawn-rate=2.5" in A.ceiling_args(max_users=300, spawn_rate=2.5)
    assert not any(a.startswith("--spawn-rate") for a in A.run_args())


def test_the_cli_accepts_it(monkeypatch):
    from crucible import cli
    from crucible.perf import commands

    seen = {}
    monkeypatch.setattr(commands, "cmd_run", lambda **kw: seen.update(kw) or 0)
    monkeypatch.setattr("sys.argv", ["crucible", "run", "--spawn-rate=3"])
    cli.main()
    assert seen["spawn_rate"] == 3.0


def test_the_wizard_turns_ramp_into_a_rate_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "t")
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "locust", tmp_path / "locust")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=tmp_path, state_dir=tmp_path / "state")
    started = []
    monkeypatch.setattr(perf_ui, "start_job", lambda state, cwd, args, label="": started.append(args) or
                        SimpleNamespace(id="j", label=label, status="running", started_at_epoch_s=0.0,
                                        exit_code=None, command_line=""))
    body = {"endpoint": "/api/db", "p99_ms": 120, "error_rate_pct": 1.0, "users": 80,
            "config_file": "src/main/resources/application.properties", "base_branch": "main",
            "restart_command": "./mvnw spring-boot:run", "health_url": "http://localhost:8080/actuator/health",
            "mode": "Semi-Automated", "run_id": "ramp-1",
            "scenarios": [{"name": "steady_80u", "users": 80, "duration_s": 300, "ramp_s": 30}]}
    response = TestClient(app).post("/v1/perf/wizard/launch", json=body, headers={"Authorization": "Bearer t"})
    assert response.status_code == 200, response.text
    assert "--spawn-rate=2.6666666666666665" in started[0]
    assert Sla.load(tmp_path / "config" / "slo.yaml").at_load["spawn_rate"] == 2.667
    assert any("80 users over 30 s" in n for n in response.json()["notes"])
