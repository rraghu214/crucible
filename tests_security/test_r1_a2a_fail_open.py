"""R1 (outsider): the A2A JSON-RPC transport authenticates only when keys are
configured; with none set it serves anyone.

DRAFTED by Claude Code, 25 October 2026. NOT YET REVIEWED by the operator.
Finding: docs/THREAT_MODEL.md §6.A A1 (the /a2a fail-open clause).

Context: crucible/core/a2a/tests/test_hardening.py already proves the ARMED
case -- with keys set, an unauthenticated message/send returns 401. This test
proves the complementary FAIL-OPEN case the finding is about: with no keys set,
the same transport does NOT reject the caller.

``tasks/get`` for a non-existent id is used so no task handler and no gateway
call runs: the assertion is purely about the auth gate, and the test makes no
network call.

The contrast with auth.py is the point: the HTTP control plane 503s when its
token is unset (fail closed); the A2A transport instead serves everyone when its
keys are unset (fail open). Proposed fix: make A2A fail closed too.

Review question for the operator:
  - Should an unconfigured A2A transport be open (demo convenience) or closed
    (same contract as the control plane)? A2A is inherited from S17 and unused
    by perf campaigns -- decision D2 is whether to disable it in the perf
    deployment entirely.
"""
from __future__ import annotations

import httpx

from crucible.core.a2a.server import A2ADemoServer


def _card() -> dict:
    return {
        "name": "sec", "description": "fail-open fixture", "version": "1.0.0",
        "supportedInterfaces": [
            {"url": "http://a2a.test/1.0", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        ],
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "echo", "name": "Echo", "description": "Echo", "tags": ["test"]}],
    }


_TASKS_GET = {"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {"id": "nope"}}


async def test_a2a_serves_unauthenticated_when_no_keys_are_set():
    """DEMONSTRATES fail-open: no keys configured -> an unauthenticated request is
    not rejected with 401.
    """
    server = A2ADemoServer(_card(), bearer_tokens=set(), api_keys=set())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                 base_url="http://a2a.test") as http:
        response = await http.post("/a2a", json=_TASKS_GET)
    assert response.status_code != 401, (
        "A2A rejected an unauthenticated call with no keys configured; if it now "
        "fails closed, this finding is addressed -- update A1."
    )


async def test_a2a_rejects_unauthenticated_when_a_key_is_set():
    """The gate works when armed (a key set, none supplied -> 401). This is the
    behaviour the no-key case should also have.
    """
    server = A2ADemoServer(_card(), bearer_tokens={"the-key"}, api_keys=set())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                 base_url="http://a2a.test") as http:
        response = await http.post("/a2a", json=_TASKS_GET)
    assert response.status_code == 401
