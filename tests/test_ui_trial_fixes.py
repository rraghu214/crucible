"""Fixes from the trial run of 31 October 2026. Group 45.

DRAFTED by Claude Code, 1 November 2026. REVIEWED AND APPROVED by the operator, 1 November 2026.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 45 for the questions to check.

The trial showed numbers nobody measured: a run that changed nothing displayed
"1206 -> 58 ms, 95.2%", and Active showed a made-up campaign below a real one.
It also found a safety check reporting a false failure, a diff full of lines
that change nothing, and two status comparisons that could never match. What is
asserted:

1. preflight reports the SLA protected when it is (the configured path, not an
   absolute one);
2. the wizard leaves a value it would not change exactly as written;
3. a running job is found by the Active endpoint;
4. the report carries model spend, and "no calls" is not "$0";
5. the run list says what kind of run each is, and keeps ceiling probes apart;
6. the dashboard ships no sample run and sends a decision for the experiment
   the server says is pending.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.ui import jobs, perf_ui
from crucible.ui.perf_ui import PerfContext, build_screen
from crucible.ui.wizard import set_value

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "crucible" / "ui" / "client" / "crucible-ui.html"
POOL = "spring.datasource.hikari.maximum-pool-size"


def _client(tmp_path: Path, *runs: dict, root: Path = REPO) -> TestClient:
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    for run in runs:
        name = f"{run['run_id']}.ceiling.json" if run.get("kind") == "ceiling" else f"{run['run_id']}.json"
        (results / name).write_text(json.dumps(run), encoding="utf-8")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=root, results_dir=str(results), state_dir=tmp_path / "state")
    return TestClient(app)


def _diagnosis(model: str, tokens_in: int = 1000, tokens_out: int = 200) -> dict:
    return {"served_by_model": model, "input_tokens": tokens_in, "output_tokens": tokens_out}


def _advisory(run_id: str = "adv-1") -> dict:
    return {
        "run_id": run_id, "started_at_epoch_s": 2.0,
        "sla": {"endpoint": "/api/db", "environment_name": "box-a"},
        "baseline": {"load": {"p99_ms": 2406.0}, "sla_met": False},
        "experiments": [{
            "experiment": 1, "verdict": "NOT_MEASURED", "diagnosis": _diagnosis("gemini-3.5-flash-lite"),
            "advisory": {"alternatives": [
                {"rank": 1, "changes": [{"prop": POOL, "to": 20}]},
                {"rank": 2, "diagnosis": _diagnosis("no-such-model")},
            ]},
        }],
    }


def _ceiling(run_id: str = "ceil-1") -> dict:
    return {"kind": "ceiling", "run_id": run_id, "started_at_epoch_s": 3.0,
            "last_passing_users": 100, "first_failing_users": 150, "steps": []}


# -- 45.1 ---------------------------------------------------------------------


class TestPreflightProtection:
    @pytest.fixture
    def offline(self, monkeypatch):
        from crucible.perf import commands

        stub = commands.PreflightCheck
        monkeypatch.setattr(commands, "_check_metrics", lambda *a, **k: stub("metrics reachable", False, "stub",
                                                                             blocking=False))
        monkeypatch.setattr(commands, "_check_version_endpoint",
                            lambda *a, **k: stub("target reports its commit", False, "stub", blocking=False))

    def test_the_preflight_screen_reports_the_sla_protected(self, tmp_path, offline):
        """It failed with the absolute path: the profile's pattern is relative."""
        ctx = PerfContext(root=REPO, results_dir=str(tmp_path), state_dir=tmp_path / "state")
        rows = next(c for c in build_screen(12, ctx, {"run": "1"})["surface"]["dataModel"].values()
                    if isinstance(c, list) and c and isinstance(c[0], dict) and "check" in c[0])
        sla_row = next(r for r in rows if r["check"] == "config/slo.yaml is protected")
        assert sla_row["result"] == "pass", sla_row

    def test_test_connection_reports_the_sla_protected(self, tmp_path, offline):
        checks = _client(tmp_path).post("/v1/perf/wizard/check").json()["checks"]
        sla = next(c for c in checks if c["name"] == "config/slo.yaml is protected")
        assert sla["ok"] is True, sla

    @pytest.mark.parametrize("given", ["config/slo.yaml", "./config/slo.yaml", str(REPO / "config" / "slo.yaml")])
    def test_relative_and_absolute_paths_inside_the_repo_both_pass(self, offline, given):
        """Operator, 1 November 2026: check relative paths too."""
        assert self._check(given, root=REPO).ok is True

    @pytest.mark.parametrize("given", ["elsewhere/slo.yaml", "/somewhere/else/slo.yaml"])
    def test_a_path_outside_the_protected_patterns_still_fails(self, offline, given):
        """The fix must not make the check pass by construction."""
        assert self._check(given, root=REPO).ok is False

    @staticmethod
    def _check(sla_path, *, root):
        from crucible.perf.campaign import Sla
        from crucible.perf.commands import preflight_checks
        from crucible.perf.profile import TargetProfile

        checks = preflight_checks(TargetProfile.named("spring-boot"), Sla.load(REPO / "config" / "slo.yaml"),
                                  sla_path, probe_timeout_s=0.1, root=root)
        return next(c for c in checks if c.name.endswith(" is protected") and "locust" not in c.name)


# -- 45.2 ---------------------------------------------------------------------


class TestUnchangedValuesAreLeftAlone:
    def test_an_equal_value_is_not_respelled(self):
        text = "objective:\n  endpoint: /api/db\n  error_rate_pct: 1.0  # budget\n"
        assert set_value(text, ["objective", "endpoint"], "/api/db") == text
        assert set_value(text, ["objective", "error_rate_pct"], 1) == text

    def test_a_changed_value_is_still_written(self):
        text = "objective:\n  p99_ms: 120\n"
        assert set_value(text, ["objective", "p99_ms"], 150) == "objective:\n  p99_ms: 150\n"

    def test_true_is_not_equal_to_one(self):
        """bool is an int in Python; YAML true and 1 are different values."""
        assert set_value("a:\n  b: 1\n", ["a", "b"], True) == "a:\n  b: true\n"

    def test_the_review_diff_shows_only_what_changes(self, tmp_path):
        from crucible.ui.wizard import WizardForm, current, render

        shutil.copytree(REPO / "config", tmp_path / "config")
        shutil.copytree(REPO / "locust", tmp_path / "locust")
        paths = (tmp_path / "config" / "slo.yaml", tmp_path / "config" / "profiles" / "spring-boot.yaml",
                 tmp_path / "locust" / "locustfile.py")
        now = current(*paths)
        form = WizardForm(endpoint=now["endpoint"], p99_ms=now["p99_ms"], error_rate_pct=now["error_rate_pct"],
                          users=now["users"], config_file=now["config_file"], base_branch=now["base_branch"],
                          restart_command=now["restart_command"], health_url=now["health_url"])
        rendered = render(form, *paths)
        for diff in (rendered["sla_diff"], rendered["profile_diff"]):
            changed = [ln for ln in diff.splitlines()
                       if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
            assert all(ln.startswith("+# wizard:") or ln.startswith("-# wizard:") for ln in changed), changed


# -- 45.3 ---------------------------------------------------------------------


def test_the_active_endpoint_finds_a_running_job(tmp_path, monkeypatch):
    """jobs.RUNNING is "running"; the endpoint compared it with "RUNNING"."""
    state = tmp_path / "state"
    directory = jobs.jobs_root(state) / "20261031-000000-abc123"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({"id": directory.name, "label": "advisory run-x",
                                                    "args": ["run"], "started_at_epoch_s": 1.0, "pid": 1}),
                                        encoding="utf-8")

    class Alive:
        def poll(self):
            return None

    monkeypatch.setitem(jobs._LIVE, directory.name, Alive())
    body = _client(tmp_path).get("/v1/perf/campaign/active").json()
    assert body["active"] is False
    assert body["running_job"] and body["running_job"]["id"] == directory.name


# -- 45.4 ---------------------------------------------------------------------


class TestSpend:
    def test_the_report_prices_every_call_on_the_manifest(self, tmp_path):
        spend = _client(tmp_path, _advisory()).get("/v1/perf/runs/adv-1/report").json()["spend"]
        assert spend["calls"] == 2  # the diagnosis and one alternative with its own call
        assert spend["unpriced_calls"] == 1
        assert spend["models_without_price"] == ["no-such-model"], "named as it is, not called an estimate"
        assert spend["total"] > 0 and spend["currency"] == "USD"

    def test_it_matches_the_scorer(self, tmp_path):
        from crucible.economics.pricing import Pricing
        from crucible.perf.scorer import _load_pricing_yaml, score_cost

        spend = _client(tmp_path, _advisory()).get("/v1/perf/runs/adv-1/report").json()["spend"]
        assert spend["total"] == pytest.approx(
            score_cost(_advisory(), Pricing.from_mapping(_load_pricing_yaml())).total)

    def test_a_run_with_no_model_call_is_not_zero_dollars(self, tmp_path):
        run = {"run_id": "met-1", "started_at_epoch_s": 1.0, "baseline": {"load": {"p99_ms": 50.0},
                                                                          "sla_met": True}, "experiments": []}
        spend = _client(tmp_path, run).get("/v1/perf/runs/met-1/report").json()["spend"]
        assert spend["total"] is None and spend["calls"] == 0


# -- 45.5 ---------------------------------------------------------------------


def test_the_run_list_says_what_each_run_is(tmp_path):
    body = _client(tmp_path, _advisory(), _ceiling()).get("/v1/perf/runs").json()
    (row,) = body["runs"]
    assert row["kind"] == "advisory"
    assert row["baseline_p99"] == 2406.0
    assert row["p99_after"] is None, "nothing applied, so there is no after"
    assert row["endpoint"] == "/api/db"
    assert [c["run_id"] for c in body["ceilings"]] == ["ceil-1"]
    assert body["ceilings"][0]["last_passing_users"] == 100


# -- 45.6 ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def page() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


class TestTheDashboardShowsOnlyWhatIsOnDisk:
    @pytest.mark.parametrize("sample", ["TR-001", "TR-002", "TR-003", "TR-004", "CP-001", "1206 ms",
                                        "95.2%", "35/37", "94.6%", "RecoAPI", "steady_50u · Trial",
                                        "pool=20 hypothesis", "Commit 4f2a1c3"])
    def test_no_sample_run_is_written_into_the_page(self, page, sample):
        assert sample not in page

    def test_a_decision_goes_to_the_experiment_the_server_names(self, page):
        """Approve read #pendingExpNum, which never existed, so it always sent 1."""
        assert "pendingExpNum" not in page
        assert re.search(r"_pendingExperiment=ap\.experiment", page)

    def test_job_status_is_compared_without_regard_to_case(self, page):
        assert "s=String(s||'').toUpperCase();" in page

    @pytest.mark.parametrize("element", [
        "res-p99-before", "res-p99-after", "res-improvement", "res-spend",
        "ov-runs", "ov-best", "ov-base", "ov-ceil",
        "hist-kpi-total", "hist-kpi-improved", "hist-kpi-best-gain", "hist-kpi-ceil",
    ])
    def test_every_figure_starts_empty_and_is_filled_from_an_api_response(self, page, element):
        """Operator, 1 November 2026: figures come from an API response wherever possible.
        A figure written into the markup survives whenever the response has nothing to say."""
        match = re.search(rf'id="{element}"[^>]*>([^<]*)<', page)
        assert match, element
        assert match.group(1).strip() in ("", "—"), (element, match.group(1))

    @pytest.mark.parametrize("element", ["bench-surface", "guard-profile-surface", "lib-cards", "ov-recent"])
    def test_whole_panels_are_rendered_from_the_server(self, page, element):
        """Benchmarks (screen 18), the profile (screen 5), Library and recent runs hold only a placeholder."""
        start = page.index(f'id="{element}"')
        body = page[start:page.index("</div>", start)] if element != "ov-recent" else page[start:page.index("</tbody>", start)]
        assert re.sub(r"<[^>]+>", "", body.split(">", 1)[1]).strip() in ("Loading…", "Loading the target profile…")


def test_live_ai_is_the_default_and_scripted_mode_reads_the_run_files(page):
    """Operator, 1 November 2026: Live AI by default; scripted is the fallback, kept for the demo.
    Scripted answers about a run come from its report, not from written-in figures."""
    assert re.search(r'id="liveAiToggle" checked', page)
    scripted = page[page.index("async function _scriptedRunReply"):page.index("function onLiveToggle")]
    assert "/v1/perf/runs/" in scripted and "/report" in scripted
