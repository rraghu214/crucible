"""Campaigns keep the SLA lock in the agent's memory.sqlite, not a separate memory.db. Group 42.

DRAFTED by Claude Code, 24 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 42 for the questions to check.

Operator decision, 24 October 2026: drop memory.db and reuse memory.sqlite. The
campaign's only record there is the SLA as Policy memory (the second lock,
§4.4). What is asserted:

1. the campaign opens the agent's store, with the agent's embedder;
2. writing and reading the SLA makes no embedding call and never touches the
   agent's vector index;
3. the SLA record never appears as evidence in the agent's searches;
4. an agent principal still cannot write it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crucible.core.memory import MemoryKind, MemoryRecord, MemoryScope, MemoryStore, Principal, SourceRef
from crucible.core.memory.embeddings import DeterministicEmbedder
from crucible.core.memory.store import PermissionDenied
from crucible.perf.campaign import Sla
from crucible.perf.policy import SlaPolicy, publish_sla, recall_sla

REPO = Path(__file__).resolve().parents[1]
SCOPE = MemoryScope("default", run_id="run-1")


class _NoCallsEmbedder(DeterministicEmbedder):
    """The agent's embedder for this test: same fingerprint, but calling it fails."""

    calls = 0

    def embed_document(self, text):
        _NoCallsEmbedder.calls += 1
        raise AssertionError("the SLA path must not embed anything")

    embed_query = embed_document


def _agent_store_with_a_fact(tmp_path) -> Path:
    path = tmp_path / "memory.sqlite"
    store = MemoryStore(path, embedder=DeterministicEmbedder())
    store.write(MemoryRecord(MemoryKind.FACT, MemoryScope("default"), "The gateway quota is 120 requests per second.",
                             [SourceRef("chat://u/2", "user")], Principal("gateway", "gateway")))
    store.close()
    return path


def test_build_campaign_opens_the_agents_store_with_the_agents_embedder(tmp_path, monkeypatch):
    from crucible.knowledge import default_embedder
    from crucible.perf.commands import build_campaign

    monkeypatch.chdir(REPO)
    (tmp_path / "state").mkdir()
    campaign = build_campaign(
        profile_name="spring-boot", sla_path="config/slo.yaml", scenario_name="s", users=5, warmup_s=1,
        measure_s=1, max_experiments=1, run_id="t", state_dir=tmp_path / "state", workspace="",
        approve_mode="file", approval_timeout_s=1, model="m", provider="p")
    assert Path(campaign.memory_store.path).name == "memory.sqlite"
    assert campaign.memory_store.embedder.fingerprint == default_embedder().fingerprint
    assert not (tmp_path / "state" / "memory.db").exists()


def test_the_sla_is_written_and_read_without_an_embedding_call_or_touching_the_index(tmp_path):
    path = _agent_store_with_a_fact(tmp_path)
    ids_before = (tmp_path / "memory.sqlite.faiss.ids.json").read_text(encoding="utf-8")
    # Same fingerprint as the agent's embedder, so nothing is seen as stale.
    store = MemoryStore(path, embedder=_NoCallsEmbedder())
    sla = Sla.load(REPO / "config" / "slo.yaml")
    publish_sla(store, SlaPolicy.from_sla(sla), SCOPE, Principal("campaign:run-1", "system"))
    found = recall_sla(store, SCOPE)
    store.close()
    assert _NoCallsEmbedder.calls == 0
    assert [r.metadata["sla_name"] for r in found] == [sla.name]
    assert (tmp_path / "memory.sqlite.faiss.ids.json").read_text(encoding="utf-8") == ids_before


def test_the_sla_never_appears_as_evidence_in_the_agents_search(tmp_path):
    path = _agent_store_with_a_fact(tmp_path)
    store = MemoryStore(path, embedder=DeterministicEmbedder())
    publish_sla(store, SlaPolicy.from_sla(Sla.load(REPO / "config" / "slo.yaml")), SCOPE,
                Principal("campaign:run-1", "system"))
    hits = store.recall("what is the p99 SLA objective", MemoryScope("default", run_id="run-1"), limit=10)
    store.close()
    assert hits and all(h.kind not in (MemoryKind.POLICY, MemoryKind.AUDIT) for h in hits)


def test_an_agent_still_cannot_write_the_sla_into_the_shared_store(tmp_path):
    store = MemoryStore(_agent_store_with_a_fact(tmp_path), embedder=DeterministicEmbedder())
    with pytest.raises((PermissionDenied, PermissionError)):
        publish_sla(store, SlaPolicy.from_sla(Sla.load(REPO / "config" / "slo.yaml")), SCOPE,
                    Principal("agent:diagnoser", "agent"))
    store.close()
