"""R1 (outsider, no credentials): state-changing / money-spending routes must
carry a control-token dependency.

Drafted by Claude Code, 25 October 2026; reviewed and approved by the operator,
28 October 2026.  Finding: docs/THREAT_MODEL.md §6.A A1 and §6.B leak 9.

The fix (28 October 2026): ``require_control`` added to /v1/action,
/v1/perf/chat, /v1/agent/facts and /v1/agent/memory/search.  All four routes
moved from the original UNAUTH_WRITE list to GUARDED below.

The assertion is at the ROUTING layer (FastAPI dependency graph), not live HTTP,
so it makes no network call and cannot accidentally trigger a gateway/embedder
call.  A later edit that silently drops the guard fails this test -- the same
pattern as tests/test_control_plane_auth.py.
"""
from __future__ import annotations

import pytest

# Routes that DO carry the control token -- pinned so a regression is caught.
# /v1/action, /v1/perf/chat, /v1/agent/facts, /v1/agent/memory/search were
# UNAUTH on 25 Oct and guarded on 28 Oct (commit that adds this note).
# /v1/agent/documents was guarded earlier (knowledge-store merge).
GUARDED = [
    ("POST", "/v1/action"),
    ("POST", "/v1/perf/chat"),
    ("POST", "/v1/agent/facts"),
    ("POST", "/v1/agent/memory/search"),
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


@pytest.mark.parametrize(("method", "path"), GUARDED)
def test_guarded_route_keeps_its_control_dependency(method, path):
    """REGRESSION GUARD: every listed route must keep require_control.
    A later edit that silently drops the guard fails here."""
    deps = _auth_dependencies(_find_route(method, path))
    assert "require_control" in deps, (
        f"{method} {path} lost its control-token guard -- this is a regression."
    )
