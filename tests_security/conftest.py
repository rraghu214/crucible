"""Fixtures for the security reproduction suite.

Drafted by Claude Code, 25 October 2026; reviewed and approved by the operator, 28 October 2026.

Why this lives in ``tests_security/`` and not ``tests/``: ``pyproject.toml`` sets
``testpaths = ["tests", "crucible/core"]``, so a plain ``uv run pytest -q`` does
NOT collect this directory. The existing suite is therefore unaffected. Run this
suite deliberately with ``uv run pytest tests_security``.

What these tests are: each one DEMONSTRATES that a finding in
docs/THREAT_MODEL.md still succeeds against the code as it stands. They assert
the *insecure* behaviour on purpose -- that promotes a finding from "feasible"
(argued) to "demonstrated" (ran), S12 §15. When a fix lands, the test flips to
assert that the control refused, and names the control.

None of these tests makes a network call. The app is built in-process; any model
or gateway call is stubbed. Running them spends no quota and touches no box.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def unauth_app(monkeypatch, tmp_path):
    """The real app, control plane fully CONFIGURED (token set), but the client
    sends no Authorization header -- the honest shape of an R1 outsider: the
    deployment is complete; the attacker just has no credential.
    """
    monkeypatch.setenv("CRUCIBLE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CRUCIBLE_A2A_GRPC_ENABLED", "0")
    monkeypatch.setenv("CRUCIBLE_SANDBOX_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "a-real-token-the-attacker-does-not-have")
    monkeypatch.setenv("CRUCIBLE_COMPLETION_TOKEN", "a-real-completion-token")
    (tmp_path / "sandbox").mkdir(parents=True, exist_ok=True)

    from crucible.main import app

    with TestClient(app) as client:  # no default Authorization header
        yield client
