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
7. a half-configured login (one variable set) is warned about at startup.
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
