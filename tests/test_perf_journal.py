"""Journal RAG assertions -- new group, week 3 (late). DESIGN.md §14, §4.3, §7.

REVIEW NEEDED: not yet reviewed by the operator. See
`docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 25.

§14 gives this component one job -- past manifests, queried at diagnosis, so the
agent does not re-propose a hypothesis an earlier campaign already disproved --
and one instruction about how: *filter on structured fields first, use vectors
for narrative only*, because journals are full of exact tokens like
`hikaricp.connections.pending` and dense retrieval is weak on those.

So these assert on an exact-token filter, and there is no embedder anywhere in
them. Three groups carry the weight:

- **The filter is on fields, never on similarity.** Profile and scenario in
  particular: a finding about a FastAPI target says nothing about a JVM one, and
  feeding it across would have the agent eliminate a cause on evidence from a
  different runtime -- the §4.3 failure arriving through the history rather than
  through the snapshot.
- **Prior findings are advisory; this campaign's ruled-out list is not.** They
  render separately, and a stale one says how old it is rather than being hidden
  or being presented as current.
- **The vector rule is encoded before anything depends on it.** Matching
  dimensionality is not the same vector space, and the failure mode is silent.
"""

import json
import time

import pytest

from crucible.perf.collector import COLLECTOR_VERSION
from crucible.perf.journal import (
    DEFAULT_LIMIT,
    STALE_AFTER_DAYS,
    JournalIndex,
    PriorFinding,
    journal_summary,
    narrative_retrieval_refusal,
    render_for_prompt,
)
from crucible.perf.verdicts import (
    ABORTED,
    DISPROVING_VERDICTS,
    IMPROVED,
    INCONCLUSIVE,
    NOT_MEASURED,
    WORSE,
)

NOW = 1_759_000_000.0
DAY = 86_400.0


def manifest(
    run_id="run-1",
    *,
    profile="spring-boot",
    scenario="steady",
    experiments=(),
    started=NOW,
):
    return {
        "run_id": run_id,
        "profile": profile,
        "scenario": scenario,
        "collector_version": COLLECTOR_VERSION,
        "started_at_epoch_s": started,
        "experiments": list(experiments),
    }


def experiment(
    n=1,
    *,
    cause="connection_pool_exhaustion",
    prop="spring.datasource.hikari.maximum-pool-size",
    value=20,
    verdict=IMPROVED,
    kept=True,
    started=NOW,
    collector=None,
    commit="",
    refused=False,
    reason="p99 fell 40%",
):
    return {
        "experiment": n,
        "cause_family": cause,
        "proposal": {"changes": [{"prop": prop, "value": value}]} if prop else {},
        "verdict": verdict,
        "verdict_reason": reason,
        "kept": kept,
        "started_at_epoch_s": started,
        "collector_version": collector or COLLECTOR_VERSION,
        "deployed_commit": commit,
        "refused_by_guard": refused,
    }


def write_journal(directory, *manifests):
    for i, m in enumerate(manifests):
        (directory / f"{m['run_id']}-{i}.json").write_text(json.dumps(m), encoding="utf-8")
    return JournalIndex.load(directory)


# ---------------------------------------------------------------------------
# 25.1 Loading: tolerant of everything except silence about what failed
# ---------------------------------------------------------------------------


def test_a_missing_journal_directory_is_an_empty_history_not_an_error(tmp_path):
    """The first campaign on a new target has no history. Refusing would be absurd."""
    index = JournalIndex.load(tmp_path / "nothing-here")
    assert index.findings == []
    assert index.unreadable == []


def test_one_corrupt_manifest_does_not_cost_the_whole_history(tmp_path):
    (tmp_path / "good.json").write_text(
        json.dumps(manifest(experiments=[experiment()])), encoding="utf-8"
    )
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    index = JournalIndex.load(tmp_path)
    assert len(index.findings) == 1
    assert len(index.unreadable) == 1
    assert "bad.json" in index.unreadable[0][0]


def test_a_non_manifest_in_the_results_directory_is_skipped_silently(tmp_path):
    """A replay result or a score file is not a malformed manifest -- it is not one."""
    (tmp_path / "replay.json").write_text(json.dumps({"cases": [], "task_set": "v1"}), encoding="utf-8")
    index = JournalIndex.load(tmp_path)
    assert index.findings == []
    assert index.unreadable == []


def test_an_experiment_that_tried_nothing_carries_no_lesson(tmp_path):
    index = write_journal(
        tmp_path, manifest(experiments=[{"experiment": 1, "verdict": NOT_MEASURED}])
    )
    assert index.findings == []


# ---------------------------------------------------------------------------
# 25.2 The filter is on exact tokens, never on similarity
# ---------------------------------------------------------------------------


def test_findings_never_cross_from_another_profile(tmp_path):
    """§4.3 through the history: a FastAPI finding says nothing about a JVM target."""
    index = write_journal(
        tmp_path,
        manifest("run-jvm", profile="spring-boot", experiments=[experiment()]),
        manifest("run-py", profile="fastapi", experiments=[experiment(cause="pool")]),
    )
    found = index.prior_findings(profile="spring-boot")
    assert [f.run_id for f in found] == ["run-jvm"]


def test_findings_never_cross_from_another_scenario(tmp_path):
    index = write_journal(
        tmp_path,
        manifest("run-a", scenario="steady", experiments=[experiment()]),
        manifest("run-b", scenario="burst", experiments=[experiment()]),
    )
    assert [f.run_id for f in index.prior_findings(scenario="burst")] == ["run-b"]


def test_a_property_filter_matches_the_exact_token(tmp_path):
    """The reason this is not a vector search: every term is an identifier."""
    index = write_journal(
        tmp_path,
        manifest("run-pool", experiments=[experiment(prop="spring.datasource.hikari.maximum-pool-size")]),
        manifest("run-threads", experiments=[experiment(prop="server.tomcat.threads.max")]),
    )
    found = index.prior_findings(properties=["server.tomcat.threads.max"])
    assert [f.run_id for f in found] == ["run-threads"]


def test_the_current_campaign_is_kept_out_of_its_own_history(tmp_path):
    """The campaign already feeds its own experiments back through ruled_out."""
    index = write_journal(tmp_path, manifest("run-now", experiments=[experiment()]))
    assert index.prior_findings(exclude_run_id="run-now") == []
    assert len(index.prior_findings()) == 1


def test_disproving_only_excludes_what_was_never_measured(tmp_path):
    """NOT_MEASURED and ABORTED say nobody found out, not that the change was wrong."""
    index = write_journal(
        tmp_path,
        manifest(
            experiments=[
                experiment(1, verdict=WORSE, kept=False),
                experiment(2, verdict=NOT_MEASURED, kept=False),
                experiment(3, verdict=ABORTED, kept=False),
                experiment(4, verdict=INCONCLUSIVE, kept=False),
            ]
        ),
    )
    verdicts = {f.verdict for f in index.prior_findings(disproving_only=True)}
    assert verdicts == {WORSE, INCONCLUSIVE}
    assert set(DISPROVING_VERDICTS) == {WORSE, INCONCLUSIVE}


def test_not_measured_is_not_treated_as_disproved():
    """Treating 'we never measured it' as 'it was disproved' is the K3 attempt-1 failure."""
    assert NOT_MEASURED not in DISPROVING_VERDICTS
    assert ABORTED not in DISPROVING_VERDICTS


def test_findings_come_back_most_recent_first_and_capped(tmp_path):
    """The journal grows without bound; the prompt does not."""
    index = write_journal(
        tmp_path,
        manifest(
            experiments=[
                experiment(n, started=NOW - n * DAY, verdict=WORSE) for n in range(1, 20)
            ]
        ),
    )
    found = index.prior_findings(limit=DEFAULT_LIMIT)
    assert len(found) == DEFAULT_LIMIT
    assert found[0].at_epoch_s > found[-1].at_epoch_s


def test_properties_tried_answers_the_question_an_operator_asks_first(tmp_path):
    index = write_journal(
        tmp_path,
        manifest(
            experiments=[
                experiment(1, prop="a.b", verdict=WORSE),
                experiment(2, prop="a.b", verdict=IMPROVED),
                experiment(3, prop="c.d", verdict=INCONCLUSIVE),
            ]
        ),
    )
    tried = index.properties_tried()
    assert sorted(tried) == ["a.b", "c.d"]
    assert sorted(tried["a.b"]) == sorted([WORSE, IMPROVED])


# ---------------------------------------------------------------------------
# 25.3 Rendering: evidence with provenance, not orders
# ---------------------------------------------------------------------------


def test_an_empty_history_renders_as_nothing_not_as_nothing_was_found():
    """'No history' and 'history was unreadable' are different claims; the caller knows which."""
    assert render_for_prompt([]) == ""


def test_a_rendered_finding_carries_its_verdict_and_why(tmp_path):
    index = write_journal(
        tmp_path,
        manifest(experiments=[experiment(verdict=WORSE, reason="p99 rose 18%", kept=False)]),
    )
    text = render_for_prompt(index.prior_findings(), now=NOW)
    assert "WORSE" in text
    assert "p99 rose 18%" in text
    # Evidence, not a prohibition -- the agent is told to weigh it.
    assert "evidence" in text.lower()
    assert "not a prohibition" in text.lower()


def test_a_stale_finding_says_how_old_it_is_rather_than_being_hidden(tmp_path):
    """A change disproved three weeks ago was measured against a different target."""
    old = NOW - (STALE_AFTER_DAYS + 5) * DAY
    index = write_journal(tmp_path, manifest(experiments=[experiment(started=old, verdict=WORSE)]))
    text = render_for_prompt(index.prior_findings(), now=NOW)
    assert "days ago" in text
    assert "may no longer describe it" in text


def test_a_finding_from_a_different_collector_says_which_half_still_holds(tmp_path):
    """That it was TRIED survives a collector change. That it measured 93 ms does not."""
    index = write_journal(
        tmp_path, manifest(experiments=[experiment(collector="0.9.0", verdict=WORSE)])
    )
    text = render_for_prompt(index.prior_findings(), now=NOW)
    assert "0.9.0" in text and COLLECTOR_VERSION in text
    assert "TRIED still holds" in text


def test_a_recent_finding_is_not_labelled_stale(tmp_path):
    index = write_journal(
        tmp_path, manifest(experiments=[experiment(started=NOW - 2 * DAY, verdict=WORSE)])
    )
    text = render_for_prompt(index.prior_findings(), now=NOW)
    assert "may no longer describe it" not in text


def test_the_commit_is_rendered_so_a_reader_can_place_the_finding(tmp_path):
    index = write_journal(
        tmp_path, manifest(experiments=[experiment(commit="abc123def456789", verdict=WORSE)])
    )
    text = render_for_prompt(index.prior_findings(), now=NOW)
    assert "abc123def456" in text


# ---------------------------------------------------------------------------
# 25.4 The narrative seam: §14's vector rule, encoded before anything needs it
# ---------------------------------------------------------------------------


def test_retrieval_across_two_embedders_is_refused_and_names_the_reindex():
    """Matching dimensionality is NOT the same vector space; the failure is silent."""
    refusal = narrative_retrieval_refusal("gemini-embedding-001@768", "nomic-embed-text@768")
    assert refusal is not None
    assert "nomic-embed-text@768" in refusal
    assert "same dimensionality" in refusal


def test_an_unlabelled_vector_cannot_be_shown_comparable_with_anything():
    assert narrative_retrieval_refusal("", "gemini-embedding-001@768") is not None
    assert narrative_retrieval_refusal("gemini-embedding-001@768", "") is not None


def test_a_matching_embedder_id_is_permitted():
    assert narrative_retrieval_refusal("gemini-embedding-001@768", "gemini-embedding-001@768") is None


# ---------------------------------------------------------------------------
# 25.5 The summary a report header reads
# ---------------------------------------------------------------------------


def test_the_summary_counts_without_a_model_or_a_prompt(tmp_path):
    index = write_journal(
        tmp_path,
        manifest("run-a", experiments=[experiment(1, verdict=IMPROVED), experiment(2, verdict=WORSE)]),
        manifest("run-b", experiments=[experiment(1, verdict=INCONCLUSIVE)]),
    )
    summary = journal_summary(index)
    assert summary["findings"] == 3
    assert summary["campaigns"] == 2
    assert summary["verdicts"] == {IMPROVED: 1, INCONCLUSIVE: 1, WORSE: 1}


def test_the_summary_counts_stale_collector_findings_separately(tmp_path):
    index = write_journal(
        tmp_path,
        manifest(experiments=[experiment(1, collector="0.9.0"), experiment(2)]),
    )
    assert journal_summary(index)["stale_collector"] == 1


def test_guard_refusals_are_carried_because_they_are_evidence_about_the_agent(tmp_path):
    index = write_journal(tmp_path, manifest(experiments=[experiment(refused=True)]))
    assert journal_summary(index)["guard_refusals"] == 1


# ---------------------------------------------------------------------------
# 25.6 Age arithmetic
# ---------------------------------------------------------------------------


def test_a_finding_with_no_timestamp_reports_no_age_rather_than_a_wrong_one():
    assert PriorFinding(run_id="r", experiment=1, cause_family="x").age_days(NOW) == 0.0


def test_age_is_measured_in_days_from_the_recorded_timestamp():
    finding = PriorFinding(run_id="r", experiment=1, cause_family="x", at_epoch_s=NOW - 3 * DAY)
    assert finding.age_days(NOW) == pytest.approx(3.0)


def test_a_clock_that_went_backwards_reports_zero_not_a_negative_age():
    finding = PriorFinding(run_id="r", experiment=1, cause_family="x", at_epoch_s=NOW + DAY)
    assert finding.age_days(NOW) == 0.0


# ---------------------------------------------------------------------------
# 25.7 The campaign actually consults it
# ---------------------------------------------------------------------------


def test_the_journal_reaches_the_diagnosis_prompt():
    """Wiring, asserted end to end: §14 is worth nothing if the prompt never sees it."""
    from crucible.perf.diagnosis import build_prompt

    finding = PriorFinding(
        run_id="run-old",
        experiment=2,
        cause_family="connection_pool_exhaustion",
        changes={"spring.datasource.hikari.maximum-pool-size": 40},
        verdict=WORSE,
        verdict_reason="p99 rose 18%",
        at_epoch_s=time.time(),
    )
    prompt = build_prompt(
        {"collector_version": COLLECTOR_VERSION},
        {"p99_ms": 120},
        prior_findings=render_for_prompt([finding]),
    )
    assert "maximum-pool-size" in prompt
    assert "p99 rose 18%" in prompt


def test_this_campaigns_ruled_out_and_the_journal_render_separately():
    """One is a fact about now and reads as an instruction; the other carries a date."""
    from crucible.perf.diagnosis import build_prompt

    finding = PriorFinding(
        run_id="run-old", experiment=1, cause_family="gc_pressure",
        changes={"a.b": 1}, verdict=WORSE, at_epoch_s=time.time(),
    )
    prompt = build_prompt(
        {"collector_version": COLLECTOR_VERSION},
        {"p99_ms": 120},
        ruled_out=("pool size 40 -> WORSE",),
        prior_findings=render_for_prompt([finding]),
    )
    assert "do not propose\nthese again" in prompt or "do not propose these again" in prompt
    assert "not a prohibition" in prompt
    assert prompt.index("do not propose") < prompt.index("not a prohibition")
