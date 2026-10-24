"""Python hooks: isolated, allowlisted, configured from the SLA and the wizard. Group 41.

DRAFTED by Claude Code, 24 October 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 41 for the questions to check.

Operator decisions, 24 October 2026: a hook is a Python file, and it must not be
able to read Crucible or its reports. The engine ran hooks as command lines with
Crucible's whole environment, control token included, and nothing let an
operator configure one. What is asserted:

1. a hook is an uploaded .py file run as `python -I`; shell commands are refused;
2. its environment is an allowlist: never Crucible's own variables, only the
   ones the operator passes through, plus the hook context;
3. a failing or overrunning hook blocks the experiment;
4. the SLA's hooks block is validated whole and builds the campaign's hooks;
5. hook files are uploaded with the token, syntax-checked, never run on upload;
6. the wizard writes the hooks block and refuses a hook not yet uploaded.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.campaign import CampaignRefused, Sla
from crucible.perf.hooks import HookFailed, HookSet, hooks_from_mapping
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext
from tests.test_perf_campaign import SLA_YAML

REPO = Path(__file__).resolve().parents[1]
DUMP_ENV = "import json, os, sys\njson.dump(dict(os.environ), open('env.json', 'w'))\nsys.exit(0)\n"


def _hooks(tmp_path, files: dict[str, str], **kwargs) -> HookSet:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    for name, text in files.items():
        (hooks_dir / name).write_text(text, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return HookSet(hooks_dir=str(hooks_dir), workspace=str(workspace), run_id="run-7", **kwargs)


class TestAHookIsAnIsolatedPythonFile:
    def test_it_runs_in_the_workspace_with_the_allowlisted_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "super-secret")
        monkeypatch.setenv("GEMINI_API_KEY", "also-secret")
        monkeypatch.setenv("PGPASSWORD", "db-pass")
        hooks = _hooks(tmp_path, {"dump.py": DUMP_ENV}, before_each="dump.py", pass_env=("PGPASSWORD",))
        result = asyncio.run(hooks.run_before_each(3))
        assert result.ok and result.exit_code == 0
        env = json.loads((tmp_path / "ws" / "env.json").read_text(encoding="utf-8"))
        assert "CRUCIBLE_CONTROL_TOKEN" not in env and "GEMINI_API_KEY" not in env
        assert env["PGPASSWORD"] == "db-pass"
        assert (env["CRUCIBLE_HOOK_STAGE"], env["CRUCIBLE_HOOK_RUN_ID"], env["CRUCIBLE_HOOK_EXPERIMENT"]) == (
            "before_each", "run-7", "3")

    def test_isolated_mode_ignores_pythonpath(self, tmp_path, monkeypatch):
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "json.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
        monkeypatch.setenv("PYTHONPATH", str(evil))
        hooks = _hooks(tmp_path, {"dump.py": DUMP_ENV}, before_each="dump.py")
        assert asyncio.run(hooks.run_before_each(1)).ok

    @pytest.mark.parametrize("name", ["psql -c 'truncate orders'", "../reset.py", "reset.sh"])
    def test_anything_but_a_plain_py_name_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError):
            hooks_from_mapping({"before_each": name})
        with pytest.raises(HookFailed, match="plain name"):
            asyncio.run(_hooks(tmp_path, {}, before_each=name).run_before_each(1))

    def test_crucible_variables_cannot_be_passed_through(self):
        with pytest.raises(ValueError, match="never reach a hook"):
            hooks_from_mapping({"before_each": "a.py", "pass_env": ["CRUCIBLE_CONTROL_TOKEN"]})


class TestAFailingHookBlocks:
    def test_a_non_zero_exit_blocks_with_its_output(self, tmp_path):
        hooks = _hooks(tmp_path, {"bad.py": "print('reset failed: db down')\nraise SystemExit(3)\n"},
                       before_each="bad.py")
        with pytest.raises(HookFailed, match="exited 3: reset failed: db down"):
            asyncio.run(hooks.run_before_each(1))

    def test_an_overrun_is_killed_and_blocks(self, tmp_path):
        hooks = _hooks(tmp_path, {"slow.py": "import time\ntime.sleep(30)\n"}, before_each="slow.py", timeout_s=1)
        with pytest.raises(HookFailed, match="timed out"):
            asyncio.run(hooks.run_before_each(1))

    def test_a_hook_not_uploaded_blocks(self, tmp_path):
        with pytest.raises(HookFailed, match="has not been uploaded"):
            asyncio.run(_hooks(tmp_path, {}, before_each="missing.py").run_before_each(1))

    def test_on_abort_failure_is_recorded_not_raised(self, tmp_path):
        hooks = _hooks(tmp_path, {"bad.py": "raise SystemExit(1)\n"}, on_abort="bad.py")
        result = asyncio.run(hooks.run_on_abort())
        assert not result.ok and result.exit_code == 1


class TestTheSlaConfiguresHooks:
    def _sla(self, tmp_path, block: str) -> Sla:
        path = tmp_path / "slo.yaml"
        path.write_text(SLA_YAML + f"hooks: {block}\n", encoding="utf-8")
        return Sla.load(path)

    def test_a_good_block_is_normalised(self, tmp_path):
        sla = self._sla(tmp_path, '{"before_each": "reset.py", "pass_env": ["PGPASSWORD"]}')
        assert sla.hooks == {"before_each": "reset.py", "timeout_s": 60.0, "pass_env": ["PGPASSWORD"]}

    @pytest.mark.parametrize("block", ['{"before_each": "reset.sh"}', '{"before": "a.py"}',
                                       '{"before_each": "a.py", "timeout_s": 0}',
                                       '{"pass_env": ["CRUCIBLE_X"]}'])
    def test_a_bad_block_is_refused_whole(self, tmp_path, block):
        with pytest.raises(CampaignRefused):
            self._sla(tmp_path, block)

    def test_build_campaign_builds_the_hooks_in_state_and_workspace(self, tmp_path, monkeypatch):
        from crucible.perf.commands import build_campaign

        monkeypatch.chdir(REPO)
        sla = tmp_path / "slo.yaml"
        sla.write_text((REPO / "config" / "slo.yaml").read_text(encoding="utf-8").replace(
            "hooks: {}", 'hooks: {"before_each": "reset.py", "timeout_s": 30}'), encoding="utf-8")
        (tmp_path / "state").mkdir()
        campaign = build_campaign(
            profile_name="spring-boot", sla_path=str(sla), scenario_name="s", users=5, warmup_s=1, measure_s=1,
            max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="", approve_mode="file",
            approval_timeout_s=1, model="m", provider="p")
        assert campaign.hooks.before_each == "reset.py" and campaign.hooks.timeout_s == 30
        assert Path(campaign.hooks.hooks_dir) == tmp_path / "state" / "hooks"
        assert campaign.hooks.workspace is None and campaign.hooks.run_id == "t"


class TestUploadAndWizard:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "t")
        shutil.copytree(REPO / "config", tmp_path / "config")
        shutil.copytree(REPO / "locust", tmp_path / "locust")
        app = FastAPI()
        app.include_router(perf_ui.router)
        app.state.perf_context = PerfContext(root=tmp_path, state_dir=tmp_path / "state")
        monkeypatch.setattr(perf_ui, "start_job", lambda *a, **k: SimpleNamespace(
            id="j", label="", status="running", started_at_epoch_s=0.0, exit_code=None, command_line=""))
        return TestClient(app), tmp_path

    def test_upload_needs_the_token_is_syntax_checked_and_never_run(self, client):
        tc, root = client
        marker = root / "ran.txt"
        text = f"open({str(marker)!r}, 'w').write('ran')\n"
        assert tc.post("/v1/perf/hooks", json={"name": "a.py", "text": text}).status_code in (401, 503)
        assert tc.post("/v1/perf/hooks", json={"name": "a.py", "text": "def (:\n"},
                       headers={"Authorization": "Bearer t"}).status_code == 422
        assert tc.post("/v1/perf/hooks", json={"name": "a.py", "text": text},
                       headers={"Authorization": "Bearer t"}).status_code == 200
        assert not marker.exists()
        assert [f["name"] for f in tc.get("/v1/perf/hooks").json()["files"]] == ["a.py"]

    def _body(self, hooks):
        return {"endpoint": "/api/db", "p99_ms": 120, "error_rate_pct": 1.0, "users": 50,
                "config_file": "src/main/resources/application.properties", "base_branch": "main",
                "restart_command": "./mvnw spring-boot:run", "health_url": "http://localhost:8080/actuator/health",
                "mode": "Semi-Automated", "run_id": "h1",
                "scenarios": [{"name": "s1", "users": 50, "duration_s": 60, "ramp_s": 5}], "hooks": hooks}

    def test_the_wizard_writes_hooks_and_refuses_one_not_uploaded(self, client):
        tc, root = client
        auth = {"Authorization": "Bearer t"}
        hooks = {"before_each": "reset.py", "timeout_s": 90, "pass_env": ["PGPASSWORD"]}
        response = tc.post("/v1/perf/wizard/save", json=self._body(hooks), headers=auth)
        assert response.status_code == 409 and "reset.py has not been uploaded" in response.json()["detail"]
        tc.post("/v1/perf/hooks", json={"name": "reset.py", "text": "pass\n"}, headers=auth)
        assert tc.post("/v1/perf/wizard/save", json=self._body(hooks), headers=auth).status_code == 200
        assert Sla.load(root / "config" / "slo.yaml").hooks == {
            "before_each": "reset.py", "timeout_s": 90.0, "pass_env": ["PGPASSWORD"]}
        assert tc.get("/v1/perf/wizard").json()["hooks"]["before_each"] == "reset.py"


def test_a_hooks_edit_mid_run_is_taken_at_the_next_boundary(tmp_path):
    from tests.test_perf_file_guard import LOAD, _guarded

    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML.replace("  error_rate_pct: 1.0\n", "  error_rate_pct: 1.0\n" + LOAD) + "hooks: {}\n",
                        encoding="utf-8")
    profile_path = tmp_path / "spring-boot.yaml"
    shutil.copy(REPO / "config" / "profiles" / "spring-boot.yaml", profile_path)
    shutil.copy(REPO / "config" / "profiles" / "spring-boot.SKILL.md", tmp_path / "spring-boot.SKILL.md")
    workspace = tmp_path / "ws"
    (workspace / "src" / "main" / "resources").mkdir(parents=True)
    from tests.test_perf_campaign import PROPERTIES

    (workspace / "src" / "main" / "resources" / "application.properties").write_text(PROPERTIES, encoding="utf-8")

    def edit():
        sla_path.write_text(sla_path.read_text(encoding="utf-8").replace(
            "hooks: {}", 'hooks: {"after_each": "note.py"}'), encoding="utf-8")

    campaign = _guarded((sla_path, profile_path, workspace), p99s=[300.0, 299.0, 298.0], edit_on_measure=2, edit=edit)
    campaign.hooks = HookSet(hooks_dir=str(tmp_path / "hooks"))
    result = asyncio.run(campaign.run())
    assert "hooks" in result.amendments[0]["changes"]
    assert campaign.hooks.after_each == "note.py"
