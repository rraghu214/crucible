"""R1 (outsider, no credentials): state-changing / money-spending routes that
carry no control-token dependency.

DRAFTED by Claude Code, 25 October 2026. NOT YET REVIEWED by the operator.
Finding: docs/THREAT_MODEL.md §6.A A1 and §6.B leak 9.

The finding is demonstrated at the ROUTING layer: the route's FastAPI dependency
graph contains no ``require_control``/``require_completion``. This is exact,
deterministic, and -- importantly under the operator's constraints -- makes NO
network call. (Driving these routes live would trigger an embedding/gateway call
on the fact-write and chat paths; the route-layer assertion avoids that while
proving the same thing: nothing on the path demands a credential.)

When a fix adds ``Depends(require_control)``, the route moves from UNAUTH_WRITE
to GUARDED and this test's parametrization flips -- mirroring
tests/test_control_plane_auth.py, whose "name the control" is ``require_control``.

Review questions for the operator:
  - Should each UNAUTH_WRITE route carry the control token? `/v1/perf/chat`
    spends gateway budget; `/v1/agent/facts` writes memory as Principal(
    "gateway"); `/v1/action` approves a parked node and resumes the run;
    `/v1/agent/memory/search` lets the caller name any tenant (INV-5).
  - For GUARDED, this pins the good state so a later edit that drops the guard
    is caught.
"""
from __future__ import annotations

import pytest

# Routes that change state or spend money and (route dump, 25 Oct 2026) have NO
# auth dependency today.
UNAUTH_WRITE = [
    ("POST", "/v1/action"),
    ("POST", "/v1/perf/chat"),
    ("POST", "/v1/agent/facts"),
    ("POST", "/v1/agent/memory/search"),
]

# Routes that DO carry the control token -- pinned so a regression is caught.
# POST /v1/agent/documents was UNAUTH when THREAT_MODEL.md was written (22 Oct)
# and has since been guarded (knowledge-store merge): a STATUS CHANGE.
GUARDED = [
    ("POST", "/v1/agent/documents"),
    ("POST", "/v1/perf/runs/{run_id}/experiments/{experiment}/decision"),
    ("POST", "/v1/perf/wizard/save"),
    ("POST", "/v1/perf/wizard/launch"),
]

AUTH_DEPS = {"require_control", "require_completion"}


def _auth_dependencies(route) -> set[str]:
    found: set[str] = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            found.add(getattr(sub.call, "__name__", str(sub.call)))
            walk(sub)

    walk(route.dependant)
    return found & AUTH_DEPS


def _find_route(method: str, path: str):
    from fastapi.routing import APIRoute

    from crucible.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                if r.path == path and method in r.methods:
                    yield r
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)

    matches = list(walk(app.routes))
    assert matches, f"no route {method} {path} found -- the path may have changed"
    return matches[0]


@pytest.mark.parametrize(("method", "path"), UNAUTH_WRITE)
def test_state_changing_route_has_no_auth_dependency(method, path):
    """DEMONSTRATES: the route serves without a credential (no auth dependency)."""
    deps = _auth_dependencies(_find_route(method, path))
    assert not deps, (
        f"{method} {path} now carries {deps}; if it was intentionally guarded, "
        "move it to GUARDED and update A1."
    )


@pytest.mark.parametrize(("method", "path"), GUARDED)
def test_guarded_route_keeps_its_control_dependency(method, path):
    """REGRESSION GUARD: these must keep require_control (documents is the one
    that changed from unauth since the threat model was written)."""
    deps = _auth_dependencies(_find_route(method, path))
    assert "require_control" in deps, (
        f"{method} {path} lost its control-token guard -- this is a regression."
    )
