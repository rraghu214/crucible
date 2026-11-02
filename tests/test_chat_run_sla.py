"""The chat judges a run by that run's own SLA, and shows "<=" as "<=". Group 50.

DRAFTED by Claude Code, 1 November 2026. REVIEWED AND APPROVED by the operator, 2 November 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 50 for the questions to check.

Trial, 1 November 2026: asked about run2-05 (SLA p99 <= 300 ms, measured 230 ms),
the chat said it "met the SLA of &lt;= 120 ms (achieving 230 ms)". Two defects:
the only SLA the model saw was today's slo.yaml, and the reply was escaped twice.
"""

from __future__ import annotations

from pathlib import Path

from crucible.ui.perf_ui import PerfContext, _campaign_detail, _chat_facts, _safe_reply

REPO = Path(__file__).resolve().parents[1]


def _run(p99_ms=300.0):
    return {"run_id": "run2-05", "scenario": "s", "stopped_reason": "SLA met",
            "sla": {"name": "perflab-gc", "endpoint": "/api/churn", "p99_ms": p99_ms, "error_rate_pct": 1.0},
            "baseline": {"load": {"p99_ms": 2500.0}, "sla_met": False}, "experiments": []}


# -- 50.1 ---------------------------------------------------------------------


def test_the_run_detail_carries_the_runs_own_sla():
    first = _campaign_detail(_run(300.0))[0]
    assert "This run's own SLA" in first and "p99 <= 300.0 ms" in first and "/api/churn" in first


def test_each_experiment_carries_whether_it_met_the_sla_as_recorded():
    """Operator, 1 November 2026: give the model what the manifest holds; compute
    nothing in code. A deliberately inconsistent record proves it is passed through,
    not recomputed (230 ms against a 300 ms SLA would otherwise read True)."""
    run = _run(300.0)
    run["experiments"] = [{"experiment": 1, "verdict": "IMPROVED", "kept": True, "predicted_p99_ms": 250.0,
                           "proposal": {"cause_family": "gc_pressure", "changes": []},
                           "before": {"load": {"p99_ms": 2500.0}}, "after": {"load": {"p99_ms": 230.0}},
                           "sla_met_before": False, "sla_met_after": False}]
    line = next(x for x in _campaign_detail(run) if x.startswith("Experiment 1:"))
    assert "SLA met (recorded): before False, after False" in line


def test_the_current_sla_is_labelled_as_for_new_runs_only(tmp_path):
    """The current slo.yaml (120 ms here) must not read as the SLA of every past run."""
    facts = _chat_facts(PerfContext(root=REPO, results_dir=str(tmp_path), state_dir=tmp_path / "state"))
    line = next(x for x in facts.splitlines() if "p99 <=" in x)
    assert line.startswith("- Current SLA, for new runs only")


# -- 50.2 ---------------------------------------------------------------------


def test_an_entity_the_model_wrote_is_shown_once_not_twice():
    assert _safe_reply("met the SLA of &lt;= 300 ms") == "met the SLA of &lt;= 300 ms"
    assert _safe_reply("met the SLA of <= 300 ms") == "met the SLA of &lt;= 300 ms"


def test_unescaping_first_opens_no_way_to_inject_markup():
    """An escaped script tag from the model is still escaped on the way out."""
    out = _safe_reply("&lt;script&gt;alert(1)&lt;/script&gt; <img src=x onerror=1> <strong>ok</strong>")
    assert "<script" not in out and "<img" not in out and "<strong>ok</strong>" in out
