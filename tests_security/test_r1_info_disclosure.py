"""R1 (outsider): information disclosure without a credential.

DRAFTED by Claude Code, 25 October 2026. NOT YET REVIEWED by the operator.
Finding: docs/THREAT_MODEL.md §6.A A2.

These assert that the OpenAPI surface and the on-disk SLA are readable with no
token. A guarded deployment would gate the schema and the config reads.

Review question for the operator:
  - Which of these are intended to be public? `/healthz` and `/readyz` are
    liveness and may stay open (A2 proposes trimming their bodies); `/docs`,
    `/openapi.json` and `/v1/perf/slo.yaml` hand an outsider the full route map
    and the SLA. Should `/docs` be off unless CRUCIBLE_DEV=1?
"""
from __future__ import annotations

import pytest

OPEN_READS = [
    "/openapi.json",   # full machine-readable route map
    "/docs",           # Swagger UI
]


@pytest.mark.parametrize("path", OPEN_READS)
def test_openapi_surface_is_public(unauth_app, path):
    """DEMONSTRATES: the API schema / docs are served to an unauthenticated caller."""
    response = unauth_app.get(path)
    assert response.status_code == 200, (
        f"{path} returned {response.status_code}; if docs were disabled in deployed mode, "
        "this finding is addressed -- re-verify and update."
    )


def test_healthz_reveals_the_gateway_url(unauth_app):
    """DEMONSTRATES: /healthz hands an outsider the gateway base URL (A2).

    Not a secret by itself, but it is infrastructure the report need not leak.
    """
    response = unauth_app.get("/healthz")
    assert response.status_code == 200
    assert "glc_base_url" in response.json(), (
        "the health body no longer carries glc_base_url; if it was trimmed, update A2"
    )
