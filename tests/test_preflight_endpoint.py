"""Preflight sends each request the run will send, once, and reads the answer. Group 49.

DRAFTED by Claude Code, 1 November 2026. REVIEWED AND APPROVED by the operator, 1 November 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 49 for the questions to check.

"load profile drives the SLA endpoint" only proved the load script names the
path. Nothing confirmed the endpoint answered, so a wrong path, query, missing
auth or no route from Box B would have been found only after a full baseline of
errors. The operator's review (1 November 2026) set the shape: the probe is the
request the run sends (path, query, method, body), a 3xx that is not followed
blocks, and Test connection checks what is on screen.
"""

from __future__ import annotations

import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.campaign import Sla
from crucible.perf.commands import _check_endpoints_answer, preflight_checks
from crucible.perf.loaddata import data_dir
from crucible.perf.loadprofile import locust_tasks
from crucible.perf.profile import TargetProfile

REPO = Path(__file__).resolve().parents[1]
LOCUSTFILE = REPO / "locust" / "locustfile.py"
STATUS = {"/api/db": 200, "/api/orders": 200, "/api/payload": 200, "/api/users": 201,
          "/api/missing": 404, "/api/secret": 401, "/api/moved": 300, "/api/broken": 500}


class _Target(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str]] = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append((self.command, self.path, self.rfile.read(length).decode() if length else ""))
        self.send_response(STATUS.get(self.path.split("?")[0], 404))
        self.end_headers()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def target():
    _Target.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _sla(base_url: str, endpoint: str = "/api/db", scenarios: list | None = None) -> Sla:
    return Sla(name="t", endpoint=endpoint, p99_ms=200.0, error_rate_pct=1.0, environment_name="box-a",
               environment_kind="preprod", target_base_url=base_url, noise_p99_spread_pct=2.0,
               scenarios=scenarios or [])


def _checks(sla, tmp_path, **kw):
    return _check_endpoints_answer(sla, kw.pop("timeout_s", 2.0), locustfile=kw.pop("locustfile", LOCUSTFILE),
                                   state_dir=tmp_path / "state", **kw)


def _one(checks, method, endpoint):
    return next(c for c in checks if c.name == f"{method} {endpoint} answers")


def _templated(*requests, data=None):
    return [{"name": "s", "requests": list(requests), "data": data or {}}]


def _req(endpoint, method="GET", query="", body=""):
    return {"endpoint": endpoint, "method": method, "query": query, "body": body, "weight": 1, "p99_ms": None}


# -- 49.1 ---------------------------------------------------------------------


class TestTheAnswerDecidesTheResult:
    def test_2xx_passes_with_request_status_and_time(self, target, tmp_path):
        check = _one(_checks(_sla(target), tmp_path), "GET", "/api/db")
        assert check.ok and "GET /api/db: HTTP 200" in check.detail and " ms" in check.detail

    @pytest.mark.parametrize("endpoint, status", [("/api/missing", 404), ("/api/secret", 401), ("/api/moved", 300)])
    def test_4xx_and_an_unfollowed_3xx_block(self, target, tmp_path, endpoint, status):
        """Operator, 1 November 2026: 3xx blocks. urllib follows ordinary redirects,
        as the load generator does, so only a 3xx neither can follow reaches here."""
        sla = _sla(target, scenarios=_templated(_req(endpoint)))
        check = _one(_checks(sla, tmp_path), "GET", endpoint)
        assert not check.ok and check.blocking and f"HTTP {status}" in check.detail

    def test_5xx_warns_but_does_not_block(self, target, tmp_path):
        """A broken endpoint may be the very thing under investigation."""
        check = _one(_checks(_sla(target, scenarios=_templated(_req("/api/broken"))), tmp_path), "GET", "/api/broken")
        assert not check.ok and not check.blocking and "HTTP 500" in check.detail

    def test_no_connection_warns_and_names_the_url(self, tmp_path):
        """Operator, 1 November 2026: like "metrics reachable", because preflight
        may run from a machine with no route to the target. Never a pass."""
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        check = _one(_checks(_sla(f"http://127.0.0.1:{port}"), tmp_path, timeout_s=1.0), "GET", "/api/db")
        assert not check.ok and not check.blocking and f"127.0.0.1:{port}/api/db" in check.detail


# -- 49.2 ---------------------------------------------------------------------


class TestTheProbeIsTheRequestTheRunSends:
    def test_a_built_in_task_is_probed_with_its_query(self, target, tmp_path):
        """/api/orders is requested as /api/orders?size=50; a bare path could answer
        400 where the real request succeeds (operator, 1 November 2026)."""
        sla = _sla(target, scenarios=[{"name": "m", "mix": {"/api/orders": {"weight": 50},
                                                              "/api/payload": {"weight": 50}}}])
        _checks(sla, tmp_path)
        assert ("GET", "/api/orders?size=50", "") in _Target.seen
        assert ("GET", "/api/payload?rows=5000", "") in _Target.seen

    def test_the_query_follows_the_environment_the_load_generator_reads(self, monkeypatch):
        monkeypatch.setenv("PERFLAB_ORDER_SIZE", "7")
        orders = next(t for t in locust_tasks(LOCUSTFILE) if t["endpoint"] == "/api/orders")
        assert orders["request_path"] == "/api/orders?size=7"

    def test_a_path_computed_at_run_time_is_not_guessed(self, target, tmp_path):
        """/api/catalog?page={page} depends on the clock. Reported unprobed, never approximated."""
        check = _one(_checks(_sla(target, "/api/catalog"), tmp_path), "GET", "/api/catalog")
        assert not check.ok and not check.blocking and "not probed" in check.detail
        assert not any(path.startswith("/api/catalog") for _, path, _ in _Target.seen)

    def test_a_template_is_rendered_with_the_first_data_row(self, target, tmp_path):
        folder = data_dir(tmp_path / "state")
        folder.mkdir(parents=True)
        (folder / "users.csv").write_text("name,size\nann lee,3\nbob,4\n", encoding="utf-8")
        data = {"who": {"file": "users.csv", "column": "name"}, "n": {"file": "users.csv", "column": "size"}}
        sla = _sla(target, scenarios=_templated(_req("/api/users", "POST", "size=${n}", '{"name": "${who}"}'),
                                                data=data))
        check = _one(_checks(sla, tmp_path, send_writes=True), "POST", "/api/users")
        assert check.ok, check.detail
        assert ("POST", "/api/users?size=3", '{"name": "ann lee"}') in _Target.seen

    def test_unreadable_data_is_reported_not_probed(self, target, tmp_path):
        data = {"who": {"file": "absent.csv", "column": "name"}}
        sla = _sla(target, scenarios=_templated(_req("/api/users", "POST", body='{"name": "${who}"}'), data=data))
        check = _one(_checks(sla, tmp_path, send_writes=True), "POST", "/api/users")
        assert not check.ok and not check.blocking and "not been uploaded" in check.detail
        assert not any(m == "POST" for m, _, _ in _Target.seen)

    def test_a_write_is_sent_only_when_the_caller_allows_it(self, target, tmp_path):
        """The Preflight screen is a GET anyone logged in can open; it must not write."""
        sla = _sla(target, scenarios=_templated(_req("/api/users", "DELETE")))
        check = _one(_checks(sla, tmp_path), "DELETE", "/api/users")
        assert not check.ok and not check.blocking and "not probed here" in check.detail
        assert not any(m == "DELETE" for m, _, _ in _Target.seen)

    def test_each_request_is_sent_once(self, target, tmp_path):
        sla = _sla(target, scenarios=[{"name": "a", "mix": {"/api/db": {"weight": 50}, "/api/orders": {"weight": 50}}},
                                      {"name": "b", "mix": {"/api/db": {"weight": 50}, "/api/orders": {"weight": 50}}}])
        _checks(sla, tmp_path)
        assert sorted(p for _, p, _ in _Target.seen) == ["/api/db", "/api/orders?size=50"]

    def test_no_base_url_warns_without_probing(self, tmp_path):
        [check] = _checks(_sla(""), tmp_path)
        assert not check.ok and not check.blocking and "target_base_url" in check.detail


# -- 49.3 ---------------------------------------------------------------------


def test_preflight_checks_includes_it(target):
    checks = preflight_checks(TargetProfile.named("spring-boot"), _sla(target, "/api/missing"),
                              "config/slo.yaml", probe_timeout_s=1.0, locustfile=LOCUSTFILE)
    assert any(c.name == "GET /api/missing answers" for c in checks)


class TestTestConnectionChecksWhatIsOnScreen:
    @pytest.fixture
    def client(self, tmp_path, target, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "t")
        root = tmp_path / "root"
        shutil.copytree(REPO / "config", root / "config")
        shutil.copytree(REPO / "locust", root / "locust")
        sla = root / "config" / "slo.yaml"
        text = sla.read_text(encoding="utf-8")
        saved_url = Sla.load(sla).target_base_url
        sla.write_text(text.replace(saved_url, target), encoding="utf-8")
        from crucible.ui import perf_ui
        from crucible.ui.perf_ui import PerfContext

        app = FastAPI()
        app.include_router(perf_ui.router)
        app.state.perf_context = PerfContext(root=root, state_dir=tmp_path / "state")
        return TestClient(app), sla

    @staticmethod
    def _body(**over):
        body = {"endpoint": "/api/db", "p99_ms": 120, "error_rate_pct": 1.0, "users": 10,
                "config_file": "src/main/resources/application.properties", "base_branch": "main",
                "restart_command": "./mvnw spring-boot:run", "health_url": "http://localhost:8080/actuator/health",
                "mode": "Semi-Automated",
                "scenarios": [{"name": "s1", "users": 10, "duration_s": 60, "ramp_s": 5}]}
        body.update(over)
        return body

    def test_an_unsaved_request_on_screen_is_probed_and_nothing_is_written(self, client):
        tc, sla = client
        before = sla.read_bytes()
        checks = tc.post("/v1/perf/wizard/check", json=self._body(requests=[{"endpoint": "/api/missing"}])).json()
        row = next(c for c in checks["checks"] if c["name"] == "GET /api/missing answers")
        assert row["ok"] is False and row["blocking"] is True
        assert sla.read_bytes() == before

    def test_a_write_on_screen_needs_the_control_token(self, client):
        tc, _ = client
        body = self._body(requests=[{"endpoint": "/api/users", "method": "POST", "body": "{}"}])
        assert tc.post("/v1/perf/wizard/check", json=body).status_code in (401, 403)
        assert not any(m == "POST" for m, _, _ in _Target.seen)
        response = tc.post("/v1/perf/wizard/check", json=body, headers={"Authorization": "Bearer t"})
        row = next(c for c in response.json()["checks"] if c["name"] == "POST /api/users answers")
        assert row["ok"] is True, row
        assert ("POST", "/api/users", "{}") in _Target.seen

    def test_a_logged_in_dashboard_session_needs_no_token(self, client, monkeypatch):
        """Operator, 1 November 2026: the token is needed only when the session is
        not authenticated. Same rule as every other write (assertion 44.7)."""
        import base64

        monkeypatch.setenv("CRUCIBLE_UI_USER", "ops")
        monkeypatch.setenv("CRUCIBLE_UI_PASSWORD", "pw")
        monkeypatch.setenv("CRUCIBLE_UI_LOGIN_GRANTS_CONTROL", "1")
        tc, _ = client
        login = "Basic " + base64.b64encode(b"ops:pw").decode()
        body = self._body(requests=[{"endpoint": "/api/users", "method": "POST", "body": "{}"}])
        response = tc.post("/v1/perf/wizard/check", json=body, headers={"Authorization": login, "X-Crucible-UI": "1"})
        assert response.status_code == 200, response.text
        assert ("POST", "/api/users", "{}") in _Target.seen

    def test_without_a_form_the_saved_sla_is_checked(self, client):
        tc, _ = client
        names = [c["name"] for c in tc.post("/v1/perf/wizard/check").json()["checks"]]
        assert "GET /api/db answers" in names
