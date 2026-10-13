"""UI jobs: the real ``crucible`` CLI, run detached, with its output and exit code kept.

These spawn real processes. Each command chosen is fast and touches only a
temporary state directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from crucible.ui import actions as A
from crucible.ui import jobs
from crucible.ui.jobs import FINISHED, UNKNOWN, get_job, list_jobs, read_output, start_job

REPO = Path(__file__).resolve().parent.parent


def _wait(state: Path, job_id: str, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = get_job(state, job_id)
        if job is not None and job.status == FINISHED:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {read_output(get_job(state, job_id))}")


def test_a_job_runs_the_cli_and_keeps_its_output_and_exit_code(tmp_path):
    state = tmp_path / "state"
    job = start_job(state, REPO, A.status_args(state_dir=str(state)), "status")

    done = _wait(state, job.id)

    assert done.exit_code == 0
    output = read_output(done)
    assert output.startswith("$ crucible status")
    assert "deploy-branch locks" in output
    assert [j.id for j in list_jobs(state)] == [job.id]


def test_a_refusal_is_recorded_as_a_non_zero_exit_not_hidden(tmp_path):
    state = tmp_path / "state"
    job = start_job(state, REPO, A.diff_args(run_a="no-such-a", run_b="no-such-b",
                                             journal=str(tmp_path / "empty")), "diff")
    assert _wait(state, job.id).exit_code != 0


def test_an_argument_error_is_recorded_with_the_parsers_message(tmp_path):
    state = tmp_path / "state"
    job = start_job(state, REPO, ["approve", "r1"], "bad approve")

    done = _wait(state, job.id)

    assert done.exit_code == 2
    assert "--experiment" in read_output(done)


def test_approving_from_the_ui_copies_the_parked_values(tmp_path):
    """The UI's approval is `crucible approve`, so the decision's params come
    from the parked request and nowhere else (assertion 13.2)."""
    state = tmp_path / "state"
    run_dir = state / "approvals" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "001.request.json").write_text(json.dumps({
        "run_id": "r1", "experiment": 1, "summary": "pool 2 -> 20",
        "params": {"spring.datasource.hikari.maximum-pool-size": 20},
    }), encoding="utf-8")

    job = start_job(state, REPO, A.approve_args(run_id="r1", experiment=1, responder="raghu",
                                                state_dir=str(state)), "approve")

    assert _wait(state, job.id).exit_code == 0
    decision = json.loads((run_dir / "001.decision.json").read_text(encoding="utf-8"))
    assert decision["params"] == {"spring.datasource.hikari.maximum-pool-size": 20}
    assert decision["responder"] == "raghu"


def test_a_job_this_process_did_not_start_and_cannot_check_is_unknown(tmp_path, monkeypatch):
    """Not running, not finished: nobody observed it end (DESIGN.md 4.2's rule)."""
    state = tmp_path / "state"
    directory = state / jobs.JOBS_SUBDIR / "20260101-000000-abcdef"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({
        "id": directory.name, "label": "run", "args": ["run"], "started_at_epoch_s": 1.0, "pid": None,
    }), encoding="utf-8")

    assert get_job(state, directory.name).status == UNKNOWN


@pytest.mark.parametrize("job_id", ["../x", "a/b", "a\\b", ".hidden", ""])
def test_a_job_id_cannot_reach_outside_the_jobs_directory(tmp_path, job_id):
    assert get_job(tmp_path, job_id) is None
