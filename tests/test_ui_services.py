"""Library and Overview name the service, not the runtime adapter. Group 47.

DRAFTED by Claude Code, 1 November 2026, while the operator was away. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 47 for the questions to check.

Operator, 1 November 2026: "include the service name properly. perf-lab, as well
the sample python one we had". The Library card read "spring-boot target": the
profile's name is the runtime adapter. What is asserted:

1. each profile declares its service, display only;
2. /v1/perf/services lists every profile by service, the active one first, and
   counts runs from the manifests' own profile field;
3. a profile with no deploy pipeline (the FastAPI sample) is "not live", so the
   page shows it as a profile, never as a service with runs it never had.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.perf.profile import TargetProfile
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext

REPO = Path(__file__).resolve().parents[1]


def _services(tmp_path: Path, *profiles_of_runs: str) -> list[dict]:
    results = tmp_path / "results"
    results.mkdir()
    for i, profile in enumerate(profiles_of_runs):
        (results / f"r{i}.json").write_text(json.dumps({"run_id": f"r{i}", "profile": profile,
                                                        "started_at_epoch_s": float(i), "experiments": []}),
                                            encoding="utf-8")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, results_dir=str(results), state_dir=tmp_path / "state")
    return TestClient(app).get("/v1/perf/services").json()["services"]


def test_each_profile_names_its_service():
    assert TargetProfile.named("spring-boot").service == "perf-lab"
    assert TargetProfile.named("fastapi").service == "perf-lab-py"


def test_every_profile_is_listed_by_service_with_the_active_one_first(tmp_path):
    services = _services(tmp_path, "spring-boot", "spring-boot", "fastapi-not-a-profile")
    assert [s["service"] for s in services] == ["perf-lab", "perf-lab-py"]
    perf_lab, py = services
    assert perf_lab["active"] and perf_lab["runs"] == 2
    assert perf_lab["target"] == "http://10.0.0.79:8080" and perf_lab["endpoint"] == "/api/db"
    assert py["runs"] == 0


def test_a_profile_with_no_pipeline_is_not_a_live_service(tmp_path):
    perf_lab, py = _services(tmp_path)
    assert perf_lab["live"] is True and perf_lab["deploy"] == "perftest -> perftest_sandbox"
    assert py["live"] is False and py["deploy"] == "manual"
    assert py["target"] == "" and py["environment"] == ""


def test_the_library_page_draws_its_cards_from_the_endpoint():
    page = (REPO / "crucible" / "ui" / "client" / "crucible-ui.html").read_text(encoding="utf-8")
    library = page[page.index("async function loadLibrary"):page.index("function _chatContextRun")]
    assert "/v1/perf/services" in library and "profile only" in library
