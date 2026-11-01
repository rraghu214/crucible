"""The Active page shows the running job's log. Group 52.

DRAFTED by Claude Code, 2 November 2026. REVIEWED AND APPROVED by the operator, 2 November 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 52 for the questions to check.

Trial, 2 November 2026: the log card on Active stayed an empty grey bar for the
whole run. The job output endpoint returns plain text; Active parsed it as JSON,
the parse threw, and the error was swallowed. The Jobs page read it as text, so
the log was visible only there.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.ui import jobs, perf_ui
from crucible.ui.perf_ui import PerfContext

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "crucible" / "ui" / "client" / "crucible-ui.html"


def test_the_output_endpoint_returns_plain_text(tmp_path):
    state = tmp_path / "state"
    directory = jobs.jobs_root(state) / "20261102-000000-abcdef"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text('{"id": "20261102-000000-abcdef", "args": []}', encoding="utf-8")
    (directory / "output.log").write_text("line one\nline two\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, results_dir=str(tmp_path), state_dir=state)
    response = TestClient(app).get("/v1/perf/jobs/20261102-000000-abcdef/output")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert "line two" in response.text


def test_every_page_reads_the_job_output_as_text():
    """Both callers, Jobs and Active: a JSON parse of plain text throws, and Active swallowed it."""
    html = DASHBOARD.read_text(encoding="utf-8")
    calls = [m.end() for m in re.finditer(r"fetch\('/v1/perf/jobs/'\+[^)]*'/output[^)]*\)", html)]
    assert len(calls) >= 2, "expected the Jobs page and the Active page to fetch job output"
    for end in calls:
        following = html[end:end + 200]
        assert ".text()" in following and ".json()" not in following, following
