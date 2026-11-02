"""The Active page's loop card follows the run. Group 55.

DRAFTED by Claude Code, 2 November 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 55 for the questions to check.

The card was a static diagram with one live dot. The campaign now prints one
`[campaign] <event>` line per stage, so the server reads the current step from the
job's log and the page draws it. What is asserted:

1. each log line moves the step the way the loop actually runs, checked against the
   lines a real run printed (run-1793596989174, 2 November 2026);
2. a second experiment starts its steps again, and keeps the baseline;
3. an Advisory run stops after the guard check, a refusal stops at the guard, a
   declined approval stops at approval;
4. other lines in the log cannot move it;
5. the active-campaign route returns the stage read from the running job's log;
6. the page has a node for every step, draws the stage, and clears it when no run is
   active;
7. the Pause and Abort dialogs say what the campaign does: it acts before the next
   experiment starts, and the experiment in progress finishes first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.ui import jobs, perf_ui
from crucible.ui.loopstage import NODES, loop_stage
from crucible.ui.perf_ui import PerfContext

REPO = Path(__file__).resolve().parents[1]
PAGE = (REPO / "crucible" / "ui" / "client" / "crucible-ui.html").read_text(encoding="utf-8")
POOL = "spring.datasource.hikari.maximum-pool-size"

# The event lines run-1793596989174 printed, in order, abridged only where a field is long.
REAL = [
    "[gateway] gateway is warm (0.2s)",
    '[campaign] started: sla={"name":"perflab-db-latency"}, profile=spring-boot',
    "[campaign] measuring: window=baseline",
    "[campaign] baseline_done: p99_ms=2500.0, sla_met=False",
    "[campaign] experiment_started: experiment=1, p99_before=2500.0",
    f"[campaign] proposed: experiment=1, summary=pool '2' -> 50, params={{\"{POOL}\":50}}",
    "[approval] experiment 1 proposal ready — run: crucible approve run-9 --experiment 1 --as <your name>",
    "[approval] waiting for operator decision …",
    "[approval] experiment 1: approved by ui:operator (approved with matching parameters)",
    "[campaign] approval: experiment=1, approved=True, reason=approved with matching parameters",
    "[campaign] applying: experiment=1, cause=connection_pool_exhaustion, commit=8296552",
    "[campaign] deployed: experiment=1, commit=8296552, target_running=8296552, verified=True",
    "[campaign] in_force: experiment=1, property_read_back=True",
    "[campaign] measuring: experiment=1, commit=8296552",
    "[campaign] verdict: experiment=1, verdict=IMPROVED, kept=True, p99_after=69.0",
    "[campaign] done: experiments=1, stopped_reason=SLA met after experiment 1: p99 69.0 ms",
]


def _at(line_containing: str, log: list[str] = REAL) -> dict:
    """The stage right after the first line that contains the text."""
    index = next(i for i, line in enumerate(log) if line_containing in line)
    return loop_stage("\n".join(log[: index + 1]))


# -- 55.1 ---------------------------------------------------------------------


class TestEachLineMovesTheStep:
    @pytest.mark.parametrize("after, current, done", [
        ("measuring: window=baseline", "baseline", []),
        ("baseline_done", None, ["baseline"]),
        ("experiment_started", "diagnose", ["baseline", "recall"]),
        ("proposed", "approve", ["baseline", "recall", "diagnose", "guard"]),
        ("approval: experiment=1", "apply", ["baseline", "recall", "diagnose", "guard", "approve"]),
        ("applying", "deploy", ["baseline", "recall", "diagnose", "guard", "approve", "apply"]),
        ("deployed", "verify", ["baseline", "recall", "diagnose", "guard", "approve", "apply", "deploy"]),
        ("in_force", "remeasure", ["baseline", "recall", "diagnose", "guard", "approve", "apply", "deploy", "verify"]),
        ("measuring: experiment", "remeasure",
         ["baseline", "recall", "diagnose", "guard", "approve", "apply", "deploy", "verify"]),
        ("verdict", None,
         ["baseline", "recall", "diagnose", "guard", "approve", "apply", "deploy", "verify", "remeasure", "verdict"]),
        ("[campaign] done:", None, list(NODES)),
    ])
    def test_the_run_of_2_november(self, after, current, done):
        stage = _at(after)
        assert stage["current"] == current and stage["done"] == done

    def test_no_deployer_means_measuring_implies_the_steps_before_it(self):
        """A local rehearsal prints no deployed or in_force line; measuring still means they happened."""
        log = [x for x in REAL if "deployed:" not in x and "in_force:" not in x]
        stage = _at("measuring: experiment", log)
        assert stage["current"] == "remeasure" and {"apply", "deploy", "verify"} <= set(stage["done"])


# -- 55.2 ---------------------------------------------------------------------


class TestAnotherExperiment:
    def test_the_second_experiment_starts_its_steps_again_and_keeps_the_baseline(self):
        log = REAL[:-1] + ["[campaign] experiment_started: experiment=2, p99_before=69.0"]
        stage = loop_stage("\n".join(log))
        assert stage["current"] == "diagnose" and stage["done"] == ["baseline", "recall"]

    def test_the_next_scenario_of_a_series_starts_from_nothing(self):
        stage = loop_stage("\n".join(REAL + ['[campaign] started: sla={"name":"b"}, profile=spring-boot']))
        assert stage == {"done": [], "current": None}


# -- 55.3 ---------------------------------------------------------------------


class TestWhereARunStops:
    def test_advisory_stops_after_the_guard_check(self):
        log = REAL[:5] + ["[campaign] advisory: experiment=1, alternatives=3", "[campaign] done: experiments=1"]
        assert loop_stage("\n".join(log[:6])) == {"done": ["baseline", "recall", "diagnose", "guard"], "current": None}

    def test_a_refusal_stops_at_the_guard(self):
        log = REAL[:5] + ["[campaign] guard_refusal: experiment=1, reason=not an allowed property"]
        assert loop_stage("\n".join(log)) == {"done": ["baseline", "recall", "diagnose"], "current": "guard"}

    def test_a_declined_approval_stops_at_the_gate(self):
        log = REAL[:6] + ["[campaign] approval: experiment=1, approved=False, reason=no"]
        stage = loop_stage("\n".join(log))
        assert stage["current"] is None and "approve" in stage["done"] and "apply" not in stage["done"]

    def test_a_pause_leaves_no_step_in_progress(self):
        log = REAL[:10] + ["[campaign] paused: reason=cost cap"]
        assert loop_stage("\n".join(log))["current"] is None


# -- 55.4 ---------------------------------------------------------------------


class TestOnlyCampaignEventsCount:
    def test_an_empty_log_is_no_stage(self):
        assert loop_stage("") == {"done": [], "current": None}

    def test_other_lines_cannot_move_it(self):
        noise = ["[campaign] verdict is a word in a sentence", "verdict: x", "[approval] applying: x",
                 "  [campaign] ??", "applying", "[gateway] deployed: fake"]
        assert loop_stage("\n".join(REAL[:6] + noise)) == loop_stage("\n".join(REAL[:6]))


# -- 55.5 ---------------------------------------------------------------------


def test_the_active_route_returns_the_stage_read_from_the_running_jobs_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", "tok")
    state = tmp_path / "state"
    lock = state / "locks" / "perftest_sandbox.lock"
    lock.mkdir(parents=True)
    (lock / "holder.json").write_text(json.dumps({"run_id": "run-9", "since_epoch_s": 1.0}))
    job = state / "ui-jobs" / "20261102-000000-abcdef"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": job.name, "label": "semi-automated run-9", "args": [],
                                              "started_at_epoch_s": 1.0, "pid": 1}))
    (job / "output.log").write_text("\n".join(REAL[:12]) + "\n", encoding="utf-8")
    # A started job is in the live-process table, still running. (Liveness by pid is
    # not asked on Windows, where os.kill(pid, 0) would terminate the process.)
    monkeypatch.setitem(jobs._LIVE, job.name, SimpleNamespace(poll=lambda: None))
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, results_dir=str(tmp_path), state_dir=state)

    data = TestClient(app).get("/v1/perf/campaign/active").json()

    assert data["stage"] == {"done": ["baseline", "recall", "diagnose", "guard", "approve", "apply", "deploy"],
                             "current": "verify"}


# -- 55.6 ---------------------------------------------------------------------


class TestThePageDrawsIt:
    def test_every_step_has_a_node_the_stage_can_name(self):
        for step in NODES:
            assert f'id="loop-{step}"' in PAGE, step
        assert re.search(r"const LOOP_NODES=\[(.*?)\]", PAGE).group(1).replace("'", "").split(",") == list(NODES)

    def test_the_poll_draws_the_stage_and_clears_it_when_nothing_runs(self):
        assert "_renderLoop(data.stage||null)" in PAGE and "_renderLoop(null)" in PAGE

    def test_the_states_it_sets_have_styles(self):
        for state in ("done", "active"):
            assert f".pgraph-dot.{state}" in PAGE and f".pgraph-label.{state}" in PAGE


# -- 55.7 ---------------------------------------------------------------------


class TestTheDialogsSayWhatTheCampaignDoes:
    @pytest.mark.parametrize("function", ["confirmPause", "confirmAbort"])
    def test_they_say_it_acts_before_the_next_experiment_and_the_current_one_finishes(self, function):
        source = PAGE[PAGE.index(f"async function {function}("):]
        text = re.search(r"confirm\('(.*?)'\)\) return", source[:source.index("\n}\n")], re.S).group(1)
        assert "before the next experiment starts" in text and "in progress finishes first" in text
        assert "load generator stops" not in text

    def test_the_campaign_really_checks_only_at_the_start_of_an_experiment(self):
        """The wording is true because the code is: both markers are read in one place, at the top
        of the experiment loop. If this fails, the dialogs above must be revisited."""
        source = (REPO / "crucible" / "perf" / "campaign.py").read_text(encoding="utf-8")
        assert len(re.findall(r"\babort_requested\(self\.state_dir", source)) == 1
        assert len(re.findall(r"\bpause_requested\(self\.state_dir", source)) == 1
        assert "aborted by operator before experiment" in source and "paused by operator before experiment" in source
