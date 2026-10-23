"""The load generator drives the SLA's endpoint and nothing else. Group 31.

DRAFTED by Claude Code, 23 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 31 for the questions to check.

Until now the tag was `"db" if endpoint.endswith("/db") else ()`: an SLA on any
other endpoint drove all ten tasks at once and judged that blend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crucible.perf.loadprofile import LoadProfileError, locust_tasks, tag_for_endpoint

REPO = Path(__file__).resolve().parents[1]
LOCUSTFILE = REPO / "locust" / "locustfile.py"


@pytest.mark.parametrize("endpoint, tag", [
    ("/api/db", "db"), ("/api/orders", "orders"), ("/api/catalog", "catalog"),
    ("/api/downstream", "downstream"), ("/api/payload", "payload"), ("/api/slow", "slow"),
])
def test_each_endpoint_selects_only_its_own_tag(endpoint, tag):
    assert tag_for_endpoint(endpoint, LOCUSTFILE) == tag


def test_every_task_has_a_distinct_tag_and_endpoint():
    tasks = locust_tasks(LOCUSTFILE)
    assert len(tasks) == len({t["tag"] for t in tasks}) == len({t["endpoint"] for t in tasks})
    assert all(t["tag"] and t["endpoint"] for t in tasks)


@pytest.mark.parametrize("endpoint", ["/transfer", "api/db", "/api/dbx", ""])
def test_an_endpoint_no_task_drives_is_refused_and_names_what_is_driven(endpoint):
    with pytest.raises(LoadProfileError, match="/api/db"):
        tag_for_endpoint(endpoint, LOCUSTFILE)


def test_a_trailing_slash_is_ignored():
    assert tag_for_endpoint("/api/db/", LOCUSTFILE) == "db"


def test_preflight_fails_blocking_on_an_undriven_endpoint(tmp_path, monkeypatch):
    from crucible.perf.campaign import Sla
    from crucible.perf.commands import preflight_checks
    from crucible.perf.profile import TargetProfile

    monkeypatch.chdir(REPO)
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text((REPO / "config" / "slo.yaml").read_text(encoding="utf-8")
                        .replace("endpoint: /api/db", "endpoint: /transfer"), encoding="utf-8")
    checks = preflight_checks(TargetProfile.named("spring-boot"), Sla.load(sla_path), str(sla_path), probe_timeout_s=0.1)
    check = next(c for c in checks if c.name == "load profile drives the SLA endpoint")
    assert not check.ok and check.blocking and "/transfer" in check.detail


def test_a_second_task_on_the_same_endpoint_is_refused(tmp_path):
    src = LOCUSTFILE.read_text(encoding="utf-8").replace('@tag("fast")', '@tag("fast")', 1)
    src = src.replace('self._get("/api/fast")', 'self._get("/api/db")', 1)
    path = tmp_path / "locustfile.py"
    path.write_text(src, encoding="utf-8")
    with pytest.raises(LoadProfileError, match="exactly one tag"):
        tag_for_endpoint("/api/db", path)


def test_a_campaign_on_a_non_db_endpoint_is_tagged_not_blended(tmp_path, monkeypatch):
    from crucible.perf.commands import build_campaign

    monkeypatch.chdir(REPO)
    (tmp_path / "state").mkdir()
    sla = tmp_path / "slo.yaml"
    sla.write_text((REPO / "config" / "slo.yaml").read_text(encoding="utf-8")
                   .replace("endpoint: /api/db", "endpoint: /api/orders"), encoding="utf-8")
    campaign = build_campaign(
        profile_name="spring-boot", sla_path=str(sla), scenario_name="s", users=5, warmup_s=1, measure_s=1,
        max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="", approve_mode="file",
        approval_timeout_s=1, model="m", provider="p",
    )
    assert campaign.scenario.tags == ("orders",)


def test_a_campaign_on_an_undriven_endpoint_is_refused(tmp_path, monkeypatch):
    from crucible.perf.commands import cmd_run

    monkeypatch.chdir(REPO)
    sla = tmp_path / "slo.yaml"
    sla.write_text((REPO / "config" / "slo.yaml").read_text(encoding="utf-8")
                   .replace("endpoint: /api/db", "endpoint: /transfer"), encoding="utf-8")
    code = cmd_run(profile_name="spring-boot", sla_path=str(sla), scenario_name="s", users=5, warmup_s=1,
                   measure_s=1, max_experiments=1, run_id="t", state_dir=str(tmp_path / "state"), workspace="",
                   approve_mode="file", approval_timeout_s=1, model="m", provider="p")
    from crucible.perf.commands import REFUSED

    assert code == REFUSED
