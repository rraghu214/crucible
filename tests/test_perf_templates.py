"""Input data and request templates. Group 40.

DRAFTED by Claude Code, 24 October 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 40 for the questions to check.

The wizard's "Input Data & Request Templates" card was a mock: an upload that
listed names, mappings to a hardcoded users.csv, and a POST body "for
/transfer". Now a scenario can declare templated requests (a path, a method, a
query and/or body with ${var} placeholders, a weight, an optional p99) and a
data mapping (var -> uploaded file + column). What is asserted:

1. a request is a path on the target, never another host;
2. every ${var} a template uses must be mapped, and the file and column must
   exist before anything runs;
3. data files are plain names in <state>/load-data/, validated on upload;
4. the runner hands Locust a spec, and replaces the built-in tasks with it;
5. templated endpoints are judged per endpoint, like a mix;
6. the wizard uploads data (token), writes the templates, refuses a missing file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf import loaddata
from crucible.perf.campaign import CampaignRefused, Sla
from crucible.perf.runner import LocustRunner, Scenario, load_env
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext
from tests.test_perf_campaign import SLA_YAML

REPO = Path(__file__).resolve().parents[1]
GOOD = ('[{"endpoint": "/api/orders", "query": "size=${size}", "weight": 3, "p99_ms": 200}, '
        '{"endpoint": "/api/echo", "method": "post", "body": "{\\"page\\": ${page}}"}]')
DATA = '{"size": {"file": "d.csv", "column": "size"}, "page": {"file": "d.csv", "column": "page"}}'


def _sla(tmp_path, requests=GOOD, data=DATA) -> Sla:
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML + f'scenarios: [{{"name": "tpl", "users": 10, "requests": {requests}, "data": {data}}}]\n',
                    encoding="utf-8")
    return Sla.load(path)


class TestTemplatesAreDeclaredSafely:
    def test_a_good_template_is_normalised(self, tmp_path):
        [scenario] = _sla(tmp_path).scenarios
        assert scenario["requests"][1] == {"endpoint": "/api/echo", "method": "POST", "query": "",
                                           "body": '{"page": ${page}}', "weight": 1, "p99_ms": None}
        assert scenario["data"]["size"] == {"file": "d.csv", "column": "size"}

    @pytest.mark.parametrize("endpoint", ["http://evil.example/x", "//evil.example/x", "api/orders",
                                          "/api/orders?size=1"])
    def test_a_request_must_be_a_path_on_the_target(self, tmp_path, endpoint):
        with pytest.raises(CampaignRefused, match="path on the target"):
            _sla(tmp_path, requests=f'[{{"endpoint": "{endpoint}"}}]', data="{}")

    def test_an_unmapped_variable_is_refused(self, tmp_path):
        with pytest.raises(CampaignRefused, match="no data mapping"):
            _sla(tmp_path, data='{"size": {"file": "d.csv", "column": "size"}}')

    @pytest.mark.parametrize("data", ['{"size": {"file": "../d.csv", "column": "x"}}',
                                      '{"size": {"file": "d.txt", "column": "x"}}'])
    def test_a_data_file_must_be_a_plain_csv_or_json_name(self, tmp_path, data):
        with pytest.raises(CampaignRefused):
            _sla(tmp_path, requests='[{"endpoint": "/a", "query": "s=${size}"}]', data=data)

    def test_mix_and_templates_together_are_refused(self, tmp_path):
        path = tmp_path / "slo.yaml"
        path.write_text(SLA_YAML + 'scenarios: [{"name": "x", "users": 1, "mix": {"/a": 50, "/b": 50}, '
                        '"requests": [{"endpoint": "/a"}]}]\n', encoding="utf-8")
        with pytest.raises(CampaignRefused, match="both mix and requests"):
            Sla.load(path)


class TestDataFiles:
    def test_upload_validates_and_lists(self, tmp_path):
        info = loaddata.save(tmp_path, "d.csv", "size,page\n10,1\n25,2\n")
        assert info == {"name": "d.csv", "rows": 2, "columns": ["page", "size"]}
        assert loaddata.listing(tmp_path)[0]["rows"] == 2

    @pytest.mark.parametrize("name, text", [("../d.csv", "a\n1\n"), ("d.txt", "a\n1\n"), ("d.csv", "a\n"),
                                            ("d.json", '{"not": "a list"}'), ("d.json", "not json")])
    def test_bad_files_are_refused(self, tmp_path, name, text):
        with pytest.raises(loaddata.LoadDataError):
            loaddata.save(tmp_path, name, text)

    def test_resolve_needs_the_file_and_column(self, tmp_path):
        loaddata.save(tmp_path, "d.csv", "size\n10\n")
        assert loaddata.resolve(tmp_path, {"s": {"file": "d.csv", "column": "size"}})["s"]["file"].endswith("d.csv")
        with pytest.raises(loaddata.LoadDataError, match="not been uploaded"):
            loaddata.resolve(tmp_path, {"s": {"file": "x.csv", "column": "size"}})
        with pytest.raises(loaddata.LoadDataError, match="does not have"):
            loaddata.resolve(tmp_path, {"s": {"file": "d.csv", "column": "nope"}})


class TestTheRunnerHandsLocustTheSpec:
    def test_the_spec_replaces_tags_and_is_written_for_locust(self, tmp_path):
        scenario = Scenario(name="t", host="h", tags=("db",), requests=({"endpoint": "/a"},),
                            data={"s": {"file": "/x/d.csv", "column": "size"}})
        command = LocustRunner(results_dir=tmp_path)._command(scenario, 60, tmp_path / "r")
        assert "--tags" not in command
        env = load_env(scenario, tmp_path, "run-1")
        spec = json.loads(Path(env["CRUCIBLE_REQUESTS"]).read_text(encoding="utf-8"))
        assert spec == {"requests": [{"endpoint": "/a"}], "data": {"s": {"file": "/x/d.csv", "column": "size"}}}


def test_build_campaign_judges_each_templated_endpoint(tmp_path, monkeypatch):
    from crucible.perf.commands import build_campaign

    monkeypatch.chdir(REPO)
    state = tmp_path / "state"
    state.mkdir()
    loaddata.save(state, "d.csv", "size,page\n10,1\n")
    [scenario] = _sla(tmp_path).scenarios
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="tpl", users=10, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=state, workspace="", approve_mode="file",
        approval_timeout_s=1, model="m", provider="p", requests=scenario["requests"], data=scenario["data"])
    assert [r["endpoint"] for r in campaign.scenario.requests] == ["/api/orders", "/api/echo"]
    assert campaign.mix_targets == {"/api/orders": 200.0, "/api/echo": 120.0}
    with pytest.raises(ValueError, match="not been uploaded"):
        build_campaign(
            profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="tpl", users=10, warmup_s=1,
            measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "empty", workspace="",
            approve_mode="file", approval_timeout_s=1, model="m", provider="p",
            requests=scenario["requests"], data=scenario["data"])


class TestTheWizard:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
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
        return TestClient(app), tmp_path, started

    def _body(self, **over):
        body = {"endpoint": "/api/db", "p99_ms": 120, "error_rate_pct": 1.0, "users": 10,
                "config_file": "src/main/resources/application.properties", "base_branch": "main",
                "restart_command": "./mvnw spring-boot:run", "health_url": "http://localhost:8080/actuator/health",
                "mode": "Semi-Automated", "run_id": "tpl-1",
                "scenarios": [{"name": "s1", "users": 10, "duration_s": 60, "ramp_s": 5}],
                "requests": [{"endpoint": "/api/orders", "query": "size=${size}"}],
                "data": {"size": {"file": "d.csv", "column": "size"}}}
        body.update(over)
        return body

    def test_upload_needs_the_token_and_launch_writes_the_templates(self, client):
        tc, root, started = client
        assert tc.post("/v1/perf/load-data", json={"name": "d.csv", "text": "size\n10\n"}).status_code in (401, 503)
        assert tc.post("/v1/perf/load-data", json={"name": "d.csv", "text": "size\n10\n"},
                       headers={"Authorization": "Bearer t"}).status_code == 200
        assert tc.get("/v1/perf/load-data").json()["files"][0]["name"] == "d.csv"
        response = tc.post("/v1/perf/wizard/launch", json=self._body(), headers={"Authorization": "Bearer t"})
        assert response.status_code == 200, response.text
        [scenario] = Sla.load(root / "config" / "slo.yaml").scenarios
        assert scenario["requests"][0]["query"] == "size=${size}" and scenario["data"]["size"]["column"] == "size"

    def test_a_missing_data_file_is_refused_before_writing(self, client):
        tc, root, started = client
        before = (root / "config" / "slo.yaml").read_bytes()
        response = tc.post("/v1/perf/wizard/launch", json=self._body(), headers={"Authorization": "Bearer t"})
        assert response.status_code == 409 and "not been uploaded" in response.json()["detail"]
        assert (root / "config" / "slo.yaml").read_bytes() == before and started == []
