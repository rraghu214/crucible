"""The NiceGUI pages, driven in-process by NiceGUI's simulated user.

These check what a person sees and what their clicks start, not the CLI behind
each action (test_ui_actions.py and test_ui_jobs.py cover that). Each test gets
its own workspace: a copy of config/ and an empty state directory.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from nicegui.testing import User

from crucible.ui import nicegui_app
from crucible.ui.jobs import list_jobs

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.nicegui_main_file("tests/ui_main.py")


@pytest.fixture(autouse=True)
def _keep_the_crucible_package():
    """NiceGUI's reset pops a page module's parent packages from sys.modules
    (`crucible.ui`, `crucible`) but leaves every other `crucible.*` submodule.
    Later tests then import a fresh `crucible` that lacks those attributes
    (`crucible.gateway` went missing in test_perf_replay). Put them back."""
    kept = {name: sys.modules[name] for name in ("crucible", "crucible.ui", "crucible.ui.nicegui_app")
            if name in sys.modules}
    yield
    sys.modules.update(kept)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "root"
    shutil.copytree(REPO / "config", root / "config")
    (root / "results").mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("CRUCIBLE_ROOT", str(root))
    monkeypatch.setenv("CRUCIBLE_STATE_DIR", str(state))
    monkeypatch.delenv("CRUCIBLE_UI_TOKEN", raising=False)
    return root, state


def _park(state: Path, run_id: str = "r1", experiment: int = 1) -> Path:
    run_dir = state / "approvals" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{experiment:03d}.request.json").write_text(json.dumps({
        "run_id": run_id, "experiment": experiment, "summary": "raise the pool",
        "params": {"spring.datasource.hikari.maximum-pool-size": 20},
        "cause_family": "connection_pool_exhaustion", "reasoning": "pending peaked at 43",
    }), encoding="utf-8")
    return run_dir


@pytest.mark.parametrize(
    "path,heading",
    [("/", "Home"), ("/new", "New campaign"), ("/campaign", "Campaign"), ("/history", "History"),
     ("/benchmark", "Benchmark"), ("/jobs", "Jobs"), ("/settings", "Settings")],
)
async def test_every_page_renders_on_an_empty_workspace(user: User, workspace, path, heading):
    await user.open(path)
    await user.should_see(heading)


async def test_a_parked_approval_shows_what_would_be_applied(user: User, workspace):
    _, state = workspace
    _park(state)

    await user.open("/campaign")

    await user.should_see("AWAITING APPROVAL")
    await user.should_see("connection_pool_exhaustion")
    await user.should_see("pending peaked at 43")


async def test_approving_in_the_browser_writes_the_parked_values(user: User, workspace):
    """The whole path: card -> dialog -> `crucible approve` job -> decision file."""
    _, state = workspace
    run_dir = _park(state)

    await user.open("/campaign")
    user.find(marker="approve-r1-1").click()
    await user.should_see("You are about to approve exactly")
    user.find(marker="responder").type("raghu")
    user.find(marker="confirm").click()

    decision_file = run_dir / "001.decision.json"
    for _ in range(300):
        if decision_file.exists():
            break
        await asyncio.sleep(0.1)
    decision = json.loads(decision_file.read_text(encoding="utf-8"))
    assert decision["params"] == {"spring.datasource.hikari.maximum-pool-size": 20}
    assert decision["responder"] == "raghu"
    assert decision["action"] == "approve"


async def test_starting_a_campaign_asks_first_and_cancel_starts_nothing(user: User, workspace):
    _, state = workspace

    await user.open("/new")
    user.find(marker="start-campaign").click()

    await user.should_see("Confirm: campaign")
    await user.should_see("oracle-ashburn-boxa")
    user.find(marker="cancel").click()
    await asyncio.sleep(0.2)
    assert list_jobs(state) == []


async def test_the_abort_dialog_names_the_target_it_may_roll_back(user: User, workspace):
    """Abort can redeploy the last good commit (DESIGN.md 19.9), so its one dialog
    names the target and environment. One dialog, not two: it is the brake."""
    _, state = workspace
    lock = state / "locks" / "perftest_sandbox.lock"
    lock.mkdir(parents=True)
    (lock / "holder.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")

    await user.open("/campaign")
    user.find(marker="abort-r1").click()

    await user.should_see("rolled back")
    await user.should_see("oracle-ashburn-boxa")
    assert list_jobs(state) == []


async def test_with_a_token_configured_nothing_runs_until_it_is_entered(user: User, workspace, monkeypatch):
    _, state = workspace
    monkeypatch.setenv("CRUCIBLE_UI_TOKEN", "s3cret")

    await user.open("/new")
    user.find(marker="plan").click()

    await user.should_see("Enter the control token")
    assert list_jobs(state) == []


async def test_the_benchmark_page_grades_like_the_scorer_and_does_not_count_errors(user: User, workspace):
    """A transport error is NOT_GRADED, never a wrong answer (assertion 22.3)."""
    root, _ = workspace
    correct = {"task_id": "T1", "task_class": "A", "fixture_id": "f",
               "ground_truth_cause_family": "connection_pool_exhaustion",
               "diagnosed_cause_family": "connection_pool_exhaustion",
               "proposed_properties": ["spring.datasource.hikari.maximum-pool-size"]}
    errored = {**correct, "task_id": "T1b", "diagnosed_cause_family": "", "proposed_properties": [],
               "error": "gateway timed out"}
    (root / "results" / "replay.json").write_text(json.dumps({"cases": [correct, errored]}))

    await user.open("/benchmark")

    await user.should_see("1/1 passed")
    await user.should_see("1 not graded")


def test_the_ui_refuses_a_network_host_without_a_token(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_UI_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="CRUCIBLE_UI_TOKEN"):
        nicegui_app.check_host("0.0.0.0")
    nicegui_app.check_host("127.0.0.1")
    monkeypatch.setenv("CRUCIBLE_UI_TOKEN", "s3cret")
    nicegui_app.check_host("0.0.0.0")
