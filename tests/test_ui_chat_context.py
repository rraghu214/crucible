"""The chat can discuss any run: measured, Advisory or ceiling probe. Group 43.

DRAFTED by Claude Code, 25 October 2026. REVIEWED AND APPROVED by the operator, 25 October 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 43 for the questions to check.

Until now the chat's facts listed each campaign's verdicts and nothing more, so
"why did experiment 2 get reverted?" or "why #2 and not #1?" got a generic
answer. Every chat turn now carries a "Runs" block read from the manifests on
disk. What is asserted:

1. an index of the runs on disk, of every type, newest first;
2. the run under discussion is the one named in the message, else the one on
   the screen, else the latest; the block says which and why;
3. a measured campaign carries, per experiment, the change, the prediction, the
   MEASURED before/after, verdict and reason, kept or reverted, per endpoint;
   per run, edits, caps, pause and abort;
4. an Advisory run carries its ranked alternatives, refusals with the change
   asked for, and states they were NOT applied or measured;
5. a ceiling probe carries its steps and the knee, and says it is no verdict;
6. the baseline evidence is bounded; with no run on disk the block says so;
7. the system prompt separates measured values from predictions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.core.memory.embeddings import DeterministicEmbedder
from crucible.knowledge import KnowledgeBase
from crucible.ui import perf_ui
from crucible.ui.perf_ui import _EVIDENCE_CHARS, PerfContext

REPO = Path(__file__).resolve().parents[1]
POOL = "spring.datasource.hikari.maximum-pool-size"
DIFF = f"--- a/application.properties\n+++ b/application.properties\n@@ -1 +1 @@\n-{POOL}=2\n+{POOL}=20\n"


def _baseline(snapshot=None):
    return {"load": {"p99_ms": 2406.0, "error_rate_pct": 0.0}, "sla_met": False,
            "snapshot": snapshot or {"hikari": {"pending_peak": 43, "acquire_mean_ms": 2406.0}}}


def _advisory(run_id, started, snapshot=None):
    return {
        "run_id": run_id, "scenario": "baseline-50", "started_at_epoch_s": started,
        "stopped_reason": "advisory: proposals recorded, none applied", "baseline": _baseline(snapshot),
        "ruled_out": ["gc (pause total 3 ms)"],
        "experiments": [{
            "experiment": 1, "verdict": "NOT_MEASURED",
            "proposal": {"cause_family": "connection_pool_exhaustion", "changes": [{"prop": POOL, "value": 20}]},
            "advisory": {"alternatives": [
                {"rank": 1, "cause_family": "connection_pool_exhaustion", "confidence": 0.8,
                 "predicted_p99_ms": 140.0, "reasoning": "43 waiters at peak against a pool of 2",
                 "changes": [{"prop": POOL, "from": "2", "to": 20}], "diff": DIFF},
                {"rank": 2, "cause_family": "thread_pool_exhaustion", "confidence": 0.3,
                 "predicted_p99_ms": 900.0, "reasoning": "tomcat busy threads near max",
                 "refused": "server.tomcat.threads.max is not an allowed property",
                 "diagnosis": {"proposal": {"changes": [{"prop": "server.tomcat.threads.max", "value": 400}]}}},
            ]},
        }],
    }


def _measured(run_id, started):
    def experiment(n, to, after, verdict, kept, reason, **extra):
        return {"experiment": n, "verdict": verdict, "verdict_reason": reason, "kept": kept,
                "predicted_p99_ms": 140.0,
                "proposal": {"cause_family": "connection_pool_exhaustion", "reasoning": f"try pool {to}",
                             "changes": [{"prop": POOL, "value": to}]},
                "before": {"load": {"p99_ms": 2406.0}}, "after": {"load": {"p99_ms": after}}, **extra}
    return {
        "run_id": run_id, "scenario": "baseline-50", "started_at_epoch_s": started,
        "stopped_reason": "cost cap reached", "baseline": _baseline(), "paused": True,
        "pause_reason": "cost cap $0.05 reached", "cap_hit": "cost",
        "caps": {"cost_usd": 0.05, "wall_clock_s": None},
        "amendments": [{"field": "p99_ms", "from": 120, "to": 200, "accepted": "resume"}],
        "experiments": [
            experiment(1, 4, 2500.0, "WORSE", False, "p99 rose 3.9%"),
            experiment(2, 20, 93.0, "IMPROVED", True, "p99 fell 96%",
                       per_endpoint={"/api/db": {"verdict": "IMPROVED", "before_p99_ms": 2406.0,
                                                 "after_p99_ms": 93.0, "target_p99_ms": 120.0}}),
            {"experiment": 3, "refused_by_guard": True, "verdict_reason": "pool 500 is above the max of 100",
             "proposal": {"changes": [{"prop": POOL, "value": 500}]}},
        ],
    }


def _ceiling(run_id, started):
    return {"kind": "ceiling", "run_id": run_id, "scenario_name": "baseline-50", "started_at_epoch_s": started,
            "min_users": 50, "max_users": 200, "step_size": 50, "stopped_reason": "SLA missed at 150 users",
            "last_passing_users": 100, "first_failing_users": 150,
            "steps": [{"users": 100, "load": {"p99_ms": 110.0, "error_rate_pct": 0.0}, "sla_met": True,
                       "sla_p99_ms": 120.0},
                      {"users": 150, "load": {"p99_ms": 480.0, "error_rate_pct": 0.2}, "sla_met": False,
                       "sla_p99_ms": 120.0}]}


def _client(tmp_path, *runs: dict) -> TestClient:
    results = tmp_path / "results"
    results.mkdir()
    for run in runs:
        name = f"{run['run_id']}.ceiling.json" if run.get("kind") == "ceiling" else f"{run['run_id']}.json"
        (results / name).write_text(json.dumps(run), encoding="utf-8")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, results_dir=str(results), state_dir=tmp_path / "state")
    app.state.knowledge = KnowledgeBase.under(tmp_path, DeterministicEmbedder())
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []

    async def fake_chat(self, *, prompt, system, request=None):
        seen.append({"prompt": prompt, "system": system})
        return {"text": "ok"}

    monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
    return seen


def _ask(client, message="why was that change kept?", **extra):
    response = client.post("/v1/perf/chat", json={"message": message, "screen": "history", **extra})
    assert response.status_code == 200, response.text


def _runs(prompt: str) -> str:
    return prompt.split("Runs:\n", 1)[1].split("\n\nKnowledge excerpts:", 1)[0]


def _under_discussion(prompt: str) -> str:
    return _runs(prompt).split("Run under discussion: ", 1)[1]


class TestWhichRun:
    @pytest.fixture
    def client(self, tmp_path):
        return _client(tmp_path, _measured("run-1", 1.0), _advisory("run-12", 2.0), _ceiling("ceil-1", 3.0))

    def test_the_index_lists_every_type_newest_first(self, client, calls):
        _ask(client)
        index = _runs(calls[0]["prompt"]).split("\n\n")[0].splitlines()[1:]
        assert [line.split(":")[0] for line in index] == ["- ceil-1", "- run-12", "- run-1"]
        assert "ceiling probe" in index[0] and "advisory" in index[1] and "measured campaign" in index[2]
        assert "1 kept" in index[2] and "paused" in index[2]

    def test_with_nothing_named_it_is_the_latest_and_says_why(self, client, calls):
        _ask(client)
        assert _under_discussion(calls[0]["prompt"]).startswith("ceil-1 (ceiling probe; the latest run")

    def test_the_screen_run_beats_the_latest(self, client, calls):
        _ask(client, run_id="run-1")
        assert _under_discussion(calls[0]["prompt"]).startswith("run-1 (measured campaign; the run on the operator's")

    def test_a_run_named_in_the_message_beats_the_screen(self, client, calls):
        _ask(client, message="Tell me about RUN-12, please", run_id="run-1")
        assert _under_discussion(calls[0]["prompt"]).startswith("run-12 (advisory; named in the message")

    def test_a_shorter_id_inside_a_longer_one_is_not_matched(self, client, calls):
        _ask(client, message="what happened in run-12?")
        assert _under_discussion(calls[0]["prompt"]).startswith("run-12 ")


def test_a_measured_campaign_carries_what_was_measured(tmp_path, calls):
    _ask(_client(tmp_path, _measured("run-1", 1.0)))
    block = _under_discussion(calls[0]["prompt"])
    assert (f"Experiment 1: cause connection_pool_exhaustion; change {POOL} ? -> 4; predicted p99 140.0 ms; "
            "MEASURED p99 2406.0 -> 2500.0 ms; verdict WORSE (p99 rose 3.9%); reverted") in block
    assert "MEASURED p99 2406.0 -> 93.0 ms; verdict IMPROVED (p99 fell 96%); kept" in block
    assert "per endpoint: /api/db IMPROVED 2406.0 -> 93.0 ms (target 120.0)" in block
    assert f"Experiment 3: REFUSED by the guard, not applied: {POOL} ? -> 500; pool 500 is above" in block
    assert "Paused: cost cap $0.05 reached" in block and "Cap reached: cost" in block
    assert 'Operator edit mid-run: {"field": "p99_ms", "from": 120, "to": 200' in block


def test_an_advisory_run_carries_its_alternatives_and_says_none_was_measured(tmp_path, calls):
    _ask(_client(tmp_path, _advisory("adv-1", 1.0)))
    block = _under_discussion(calls[0]["prompt"])
    assert "NOT applied or measured" in block
    assert f"#1 cause connection_pool_exhaustion; change {POOL} 2 -> 20; confidence 0.8; predicted p99 140.0 ms" in block
    assert "43 waiters at peak against a pool of 2" in block and f"+{POOL}=20" in block
    assert "#2 cause thread_pool_exhaustion; change server.tomcat.threads.max ? -> 400" in block
    assert "REFUSED by the guard: server.tomcat.threads.max is not an allowed property" in block


def test_a_ceiling_probe_carries_its_steps_and_knee_and_is_no_verdict(tmp_path, calls):
    _ask(_client(tmp_path, _ceiling("ceil-1", 1.0)))
    block = _under_discussion(calls[0]["prompt"])
    assert "150 users: p99 480.0 ms (limit 120.0), errors 0.2%, SLA met: False" in block
    assert "Knee: last level that met the SLA 100 users; first that did not 150 users" in block
    assert "not a verdict on any change" in block


def test_the_baseline_evidence_is_carried_and_bounded(tmp_path, calls):
    big = {"hikari": {"pending_peak": 43}, "padding": "x" * (_EVIDENCE_CHARS * 3)}
    _ask(_client(tmp_path, _advisory("adv-1", 1.0, snapshot=big)))
    block = _under_discussion(calls[0]["prompt"])
    assert '"pending_peak": 43' in block and "Ruled out: gc (pause total 3 ms)" in block
    evidence = next(line for line in block.splitlines() if line.startswith("Baseline evidence"))
    assert len(evidence) <= len("Baseline evidence (truncated): ") + _EVIDENCE_CHARS


def test_with_no_run_on_disk_it_says_so(tmp_path, calls):
    _ask(_client(tmp_path))
    assert _runs(calls[0]["prompt"]) == "(no run on disk yet)"


def test_the_system_prompt_separates_measured_from_predicted(tmp_path, calls):
    _ask(_client(tmp_path, _advisory("adv-1", 1.0)))
    system = calls[0]["system"]
    assert "Only MEASURED values are results" in system
    assert "every Advisory proposal, has NOT been applied or measured" in system
    assert "naming the run you are discussing" in system
    assert "never a verdict on a change" in system


class TestFuzzyAndMissing:
    @pytest.fixture
    def client(self, tmp_path):
        return _client(tmp_path, _measured("sb-pool-3", 1.0), _advisory("adv-1025", 2.0), _ceiling("ceil-1", 3.0))

    def test_a_near_miss_picks_the_closest_id_and_says_it_was_not_exact(self, client, calls):
        _ask(client, message="what happened in sb-pool3?")
        block = _under_discussion(calls[0]["prompt"])
        assert block.startswith("sb-pool-3 (measured campaign; closest match on disk to 'sb-pool3', which is not an exact id")

    def test_an_id_not_on_disk_is_called_out_explicitly(self, client, calls):
        _ask(client, message="why did zz-run-77 stop?")
        runs = _runs(calls[0]["prompt"])
        assert "NOT ON DISK: the message asks about 'zz-run-77'" in runs
        assert "the latest run (none was named)" in runs

    def test_an_id_not_on_disk_is_called_out_even_with_no_runs(self, tmp_path, calls):
        _ask(_client(tmp_path), message="tell me about zz-run-77")
        assert _runs(calls[0]["prompt"]).startswith("(no run on disk yet)\nNOT ON DISK: the message asks about 'zz-run-77'")

    def test_latest_of_a_kind_can_be_asked_for_in_words(self, client, calls):
        _ask(client, message="explain the latest advisory run")
        assert _under_discussion(calls[0]["prompt"]).startswith("adv-1025 (advisory; the latest advisory, as asked")

    def test_ordinary_hyphenated_words_are_not_taken_for_run_ids(self, client, calls):
        _ask(client, message="is semi-automated mode safe for pool-size changes?")
        assert "NOT ON DISK" not in _runs(calls[0]["prompt"])


def test_amendments_are_carried_whole(tmp_path, calls):
    run = _measured("run-1", 1.0)
    run["amendments"] = [{"field": "at_load.users", "from": 50, "to": 30, "note": "x" * 600}]
    _ask(_client(tmp_path, run))
    assert "x" * 600 in _under_discussion(calls[0]["prompt"])


def test_the_system_prompt_is_transparent_about_guard_refusals(tmp_path, calls):
    _ask(_client(tmp_path, _advisory("adv-1", 1.0)))
    system = calls[0]["system"]
    assert "quote the guard's reason exactly" in system
    assert "outside the bounds the target profile allows the agent" in system
