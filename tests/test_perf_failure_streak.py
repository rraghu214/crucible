"""Three measured failures in a row stop the campaign. Assertion 37.7.

DRAFTED by Claude Code, 25 October 2026, on the operator's instruction; REVIEWED AND APPROVED 25 October 2026 ("yes have
this guardrail"). See `docs/CRUCIBLE_TEST_ASSERTIONS.md` 37.7.

Until now only three GUARD REFUSALS in a row stopped the loop. A change that was
applied, measured and turned out worse or inconclusive was reverted and the
agent simply tried again, up to the experiment limit. What is asserted:

1. three measured experiments in a row that were not kept stop the campaign;
2. a kept experiment resets the count;
3. guard refusals in between neither count nor reset it;
4. the streak is read from the recorded experiments, so it survives a resume.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from crucible.perf.campaign import IMPROVED, INCONCLUSIVE, WORSE, Sla, measured_failure_streak
from crucible.perf.profile import TargetProfile
from tests.test_perf_campaign import PROPERTIES, SLA_YAML, _campaign


def _m(verdict="", kept=False, refused=False):
    return SimpleNamespace(verdict=verdict, kept=kept, refused_by_guard=refused)


@pytest.mark.parametrize("experiments, streak", [
    ([_m(INCONCLUSIVE), _m(WORSE), _m(INCONCLUSIVE)], 3),
    ([_m(INCONCLUSIVE), _m(IMPROVED, kept=True), _m(WORSE)], 1),
    ([_m(WORSE), _m(refused=True), _m(INCONCLUSIVE)], 2),
    ([_m(IMPROVED, kept=True)], 0),
    ([], 0),
])
def test_the_streak_counts_only_trailing_measured_failures(experiments, streak):
    assert measured_failure_streak(experiments) == streak


@pytest.fixture
def parts(tmp_path):
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(SLA_YAML, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    profile = TargetProfile.from_mapping({
        "name": "spring-boot", "runtime": "jvm", "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {"spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100}},
        "protected_paths": ["config/slo.yaml"], "deploy": {"branch": "perftest_sandbox"},
    })
    return profile, Sla.load(sla_path), workspace


def test_three_measured_failures_in_a_row_stop_the_campaign(parts):
    campaign = _campaign(*parts, p99s=[300.0] + [299.0] * 6, max_experiments=6)
    result = asyncio.run(campaign.run())
    assert len(result.experiments) == 3
    assert all(m.verdict == INCONCLUSIVE and not m.kept for m in result.experiments)
    assert "3 measured experiments in a row were not kept" in result.stopped_reason
    assert not result.aborted and not result.paused


def test_a_kept_experiment_resets_the_count(parts):
    # 300 -> 299 (inconclusive) -> 200 (improved, kept) -> then three inconclusive.
    campaign = _campaign(*parts, p99s=[300.0, 299.0, 200.0, 199.0, 199.5, 199.2, 199.1], max_experiments=6)
    result = asyncio.run(campaign.run())
    assert [m.kept for m in result.experiments][:2] == [False, True]
    assert len(result.experiments) == 5
    assert "in a row were not kept" in result.stopped_reason
