"""Optional site login for a dashboard published beyond localhost. Group 44.

DRAFTED by Claude Code, 31 October 2026. REVIEWED AND APPROVED by the operator, 31 October 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 44 for the questions to check.

Operator decision, 31 October 2026: until submission the dashboard is public,
behind an HTTPS tunnel, so the instructor can try it. The browser's own Basic
Auth prompt gates the whole site. What is asserted:

1. with neither variable set, nothing changes (local use, the test suite);
2. with both set, every route needs the credentials, except /healthz;
3. the challenge makes the browser show its login prompt;
4. wrong user, wrong password and malformed headers are all refused;
5. a valid control or completion token also passes, because the dashboard's
   writes carry the token in the one Authorization header a request has;
6. the login grants no write: a write still needs the control token;
7. a half-configured login (one variable set) is warned about at startup;
8. with CRUCIBLE_UI_LOGIN_GRANTS_CONTROL=1 the login stands in for the control
   token, only on requests carrying X-Crucible-UI: 1 (CSRF-safe; 44.7).
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from crucible.auth import site_login_ok
from tests.conftest import COMPLETION_TOKEN, CONTROL_TOKEN


def _basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def client():
    from crucible.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_UI_USER", "instructor")
    monkeypatch.setenv("CRUCIBLE_UI_PASSWORD", "s3cret:with-colon")


def test_off_by_default_nothing_changes(client, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_UI_USER", raising=False)
    monkeypatch.delenv("CRUCIBLE_UI_PASSWORD", raising=False)
    assert client.get("/dashboard").status_code == 200


def test_one_variable_alone_does_not_switch_it_on(client, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_UI_USER", "instructor")
    monkeypatch.delenv("CRUCIBLE_UI_PASSWORD", raising=False)
    assert client.get("/dashboard").status_code == 200


@pytest.mark.usefixtures("login_on")
class TestWhenOn:
    @pytest.mark.parametrize("path", ["/dashboard", "/v1/perf/runs", "/v1/perf/wizard", "/v1/agent/documents"])
    def test_every_route_needs_the_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")

    def test_healthz_stays_open(self, client):
        assert client.get("/healthz").status_code == 200

    def test_the_right_credentials_pass_even_with_a_colon_in_the_password(self, client):
        assert client.get("/dashboard", headers=_basic("instructor", "s3cret:with-colon")).status_code == 200

    @pytest.mark.parametrize("headers", [
        _basic("instructor", "wrong"), _basic("someone", "s3cret:with-colon"), _basic("", ""),
        {"Authorization": "Basic not-base64!!"}, {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Digest x"},
    ])
    def test_anything_else_is_refused(self, client, headers):
        assert client.get("/dashboard", headers=headers).status_code == 401

    @pytest.mark.parametrize("token", [CONTROL_TOKEN, COMPLETION_TOKEN])
    def test_a_valid_token_passes_the_login(self, client, token):
        assert client.get("/v1/perf/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    def test_the_login_grants_no_write(self, client):
        response = client.post("/v1/perf/chat", json={"message": "hi"}, headers=_basic("instructor", "s3cret:with-colon"))
        assert response.status_code == 401
        assert "control plane" in response.text


def test_the_check_is_false_without_configuration(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_UI_USER", raising=False)
    monkeypatch.delenv("CRUCIBLE_UI_PASSWORD", raising=False)
    assert not site_login_ok(_basic("", "")["Authorization"])


@pytest.mark.parametrize("user, password, missing", [
    ("instructor", "", "CRUCIBLE_UI_PASSWORD"), ("", "pw", "CRUCIBLE_UI_USER"), ("", "", ""), ("instructor", "pw", ""),
])
def test_half_configured_names_the_missing_variable(monkeypatch, user, password, missing):
    from crucible.auth import site_login_half_configured

    monkeypatch.setenv("CRUCIBLE_UI_USER", user)
    monkeypatch.setenv("CRUCIBLE_UI_PASSWORD", password)
    assert site_login_half_configured() == missing


def test_startup_warns_when_half_configured(monkeypatch, caplog):
    from crucible.main import app

    monkeypatch.setenv("CRUCIBLE_UI_USER", "instructor")
    monkeypatch.delenv("CRUCIBLE_UI_PASSWORD", raising=False)
    with caplog.at_level("WARNING", logger="crucible"), TestClient(app):
        pass
    assert "site login is OFF: CRUCIBLE_UI_PASSWORD is not set" in caplog.text


UI = {"X-Crucible-UI": "1"}
LOGIN = _basic("instructor", "s3cret:with-colon")


@pytest.mark.usefixtures("login_on")
class TestLoginGrantsControl:
    """CRUCIBLE_UI_LOGIN_GRANTS_CONTROL=1: the dashboard needs no control token (44.7)."""

    @pytest.fixture(autouse=True)
    def _grant(self, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_UI_LOGIN_GRANTS_CONTROL", "1")

    def _write(self, client, headers):
        # /v1/perf/chat is a control-gated write; the gateway is never reached
        # when the gate refuses, and is stubbed when it lets the request through.
        return client.post("/v1/perf/chat", json={"message": "hi"}, headers=headers)

    def test_login_plus_ui_header_passes_the_control_gate(self, client, monkeypatch):
        async def fake_chat(self, *, prompt, system, request=None):
            return {"text": "ok"}

        monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
        assert self._write(client, {**LOGIN, **UI}).status_code == 200

    def test_login_without_the_ui_header_is_refused(self, client):
        # What a cross-site (CSRF) request riding on the browser's remembered
        # login looks like: it cannot add a custom header.
        assert self._write(client, LOGIN).status_code == 401

    @pytest.mark.parametrize("value", ["0", "true", ""])
    def test_only_the_exact_header_value_counts(self, client, value):
        assert self._write(client, {**LOGIN, "X-Crucible-UI": value}).status_code == 401

    def test_wrong_login_plus_ui_header_is_refused(self, client):
        assert self._write(client, {**_basic("instructor", "wrong"), **UI}).status_code == 401

    def test_the_token_still_works(self, client, monkeypatch):
        async def fake_chat(self, *, prompt, system, request=None):
            return {"text": "ok"}

        monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
        assert self._write(client, {"Authorization": f"Bearer {CONTROL_TOKEN}"}).status_code == 200


def test_the_grant_needs_the_site_login_on(client, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_UI_LOGIN_GRANTS_CONTROL", "1")
    monkeypatch.delenv("CRUCIBLE_UI_USER", raising=False)
    monkeypatch.delenv("CRUCIBLE_UI_PASSWORD", raising=False)
    response = client.post("/v1/perf/chat", json={"message": "hi"}, headers={**_basic("", ""), **UI})
    assert response.status_code == 401


@pytest.mark.usefixtures("login_on")
def test_without_the_grant_the_login_still_grants_no_write(client, monkeypatch):
    monkeypatch.delenv("CRUCIBLE_UI_LOGIN_GRANTS_CONTROL", raising=False)
    assert client.post("/v1/perf/chat", json={"message": "hi"}, headers={**LOGIN, **UI}).status_code == 401


def test_the_dashboard_sends_the_ui_header_on_every_request():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "crucible/ui/client/crucible-ui.html").read_text(encoding="utf-8")
    assert "'X-Crucible-UI':'1'" in page.split("function getAuthHeaders(){", 1)[1].split("}", 1)[0]


def test_no_cors_middleware_while_the_login_can_grant_control():
    """The X-Crucible-UI check is CSRF-proof only because no cross-origin request
    may send custom headers. Adding CORS middleware must revisit 44.7 first."""
    from starlette.middleware.cors import CORSMiddleware

    from crucible.main import app

    assert not any(m.cls is CORSMiddleware for m in app.user_middleware)
