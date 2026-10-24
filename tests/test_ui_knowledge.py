"""Knowledge documents uploaded from the dashboard. Group 29.

DRAFTED by Claude Code, 23 October 2026. REVIEWED AND APPROVED by the operator, 23 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 29 for the questions to check.

The wizard's drop zone used to keep file names in the browser and upload
nothing, while the review step reported them as "uploaded". What is asserted:

1. clicking Upload & index sends the document; the upload call saves the
   original apart from the index, queues it and returns at once, and indexing
   runs in the background;
2. only text the chunker can read is accepted, and a refusal says why;
3. writing to the knowledge base needs the control token, failing closed;
4. a failed index is reported on the upload, never shown as indexed;
5. re-uploading replaces the document, and the old text stops being found;
6. the chat searches the documents, cites what it used, takes its facts from
   the SLA on disk rather than a hardcoded prompt, and cannot be made to
   render script.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.core.memory.embeddings import DeterministicEmbedder
from crucible.knowledge import KnowledgeBase
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext

TOKEN = "test-control-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DOC = """# PerfLab product notes

PerfLab exposes /api/db, which reads one row from Postgres through a HikariCP
pool. Under fifty users with a pool of two connections the pool saturates and
requests queue for a connection, which is the connection_pool_exhaustion family.

# Downstream

/api/downstream calls httpbin with a configurable delay. Its latency is set by
perflab.downstream.timeout-ms and has nothing to do with the database pool.
"""


class _BlockingEmbedder(DeterministicEmbedder):
    """Holds every embed until released, so a test can see the request return first."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def embed_document(self, text: str) -> list[float]:
        self.release.wait(timeout=10)
        return super().embed_document(text)


class _FailingEmbedder(DeterministicEmbedder):
    def embed_document(self, text: str) -> list[float]:
        raise RuntimeError("gateway 503 after retries")


def _client(tmp_path, embedder=None) -> tuple[TestClient, KnowledgeBase]:
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(state_dir=tmp_path / "state")
    kb = KnowledgeBase.under(tmp_path, embedder or DeterministicEmbedder())
    app.state.knowledge = kb
    return TestClient(app), kb


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", TOKEN)


def _upload(client, filename="PERF_LAB_PRODUCT.md", text=DOC, headers=AUTH):
    return client.post("/v1/perf/knowledge/documents", json={"filename": filename, "text": text}, headers=headers)


class TestIndexedInTheBackground:
    def test_the_request_returns_before_indexing_finishes(self, tmp_path):
        embedder = _BlockingEmbedder()
        client, kb = _client(tmp_path, embedder)
        started = time.monotonic()
        response = _upload(client)
        assert response.status_code == 202
        assert time.monotonic() - started < 5
        assert response.json()["status"] in {"queued", "indexing"}
        embedder.release.set()
        kb.wait_idle()
        assert kb.uploads()[0]["status"] == "indexed"

    def test_the_original_is_kept_byte_for_byte_apart_from_the_index(self, tmp_path):
        client, kb = _client(tmp_path)
        text = DOC.replace("\n", "\r\n")
        _upload(client, text=text)
        kb.wait_idle()
        original = tmp_path / "knowledge" / "PERF_LAB_PRODUCT.md"
        assert original.read_bytes() == text.encode("utf-8")
        assert sorted(p.name for p in (tmp_path / "knowledge").iterdir()) == ["PERF_LAB_PRODUCT.md"]
        assert (tmp_path / "knowledge-index" / "knowledge.sqlite").exists()

    def test_a_refused_upload_saves_no_original(self, tmp_path):
        client, _ = _client(tmp_path)
        _upload(client, filename="design.pdf")
        assert not (tmp_path / "knowledge").exists()

    def test_an_indexed_document_is_listed_with_its_chunk_count(self, tmp_path):
        client, kb = _client(tmp_path)
        _upload(client)
        kb.wait_idle()
        listed = client.get("/v1/perf/knowledge/documents").json()
        assert [d["filename"] for d in listed["documents"]] == ["PERF_LAB_PRODUCT.md"]
        assert listed["documents"][0]["chunks"] == listed["uploads"][0]["chunks"] >= 1

    def test_an_indexed_document_is_found_by_search(self, tmp_path):
        client, kb = _client(tmp_path)
        _upload(client)
        kb.wait_idle()
        hits = kb.search("which pool does /api/db use")
        assert hits and hits[0]["source"] == "PERF_LAB_PRODUCT.md"
        assert "HikariCP" in " ".join(h["text"] for h in hits)


class TestOnlyReadableTextIsAccepted:
    @pytest.mark.parametrize("filename", ["design.pdf", "notes.docx", "../escape.md", "dir/notes.md"])
    def test_a_refused_file_is_never_queued(self, tmp_path, filename):
        client, kb = _client(tmp_path)
        response = _upload(client, filename=filename)
        assert response.status_code == 422
        assert kb.uploads() == []

    def test_an_empty_file_is_refused_with_its_reason(self, tmp_path):
        client, _ = _client(tmp_path)
        response = _upload(client, text="   \n")
        assert response.status_code == 422
        assert "empty" in response.json()["detail"]


class TestWritingNeedsTheControlToken:
    def test_no_token_configured_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CRUCIBLE_CONTROL_TOKEN")
        client, kb = _client(tmp_path)
        assert _upload(client).status_code == 503
        assert kb.uploads() == []

    def test_a_wrong_token_is_refused(self, tmp_path):
        client, kb = _client(tmp_path)
        assert _upload(client, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert kb.uploads() == []


class TestAFailureIsReportedNotHidden:
    def test_a_failed_index_is_failed_with_its_error_and_lists_nothing(self, tmp_path):
        client, kb = _client(tmp_path, _FailingEmbedder())
        _upload(client)
        kb.wait_idle()
        upload = kb.uploads()[0]
        assert upload["status"] == "failed"
        assert "gateway 503" in upload["error"]
        assert kb.documents() == []


class TestTheChatIsGrounded:
    @pytest.fixture
    def gateway(self, monkeypatch):
        calls: list[dict] = []
        reply = {"text": "The pool is HikariCP."}

        async def fake_chat(self, *, prompt, system, request=None):
            calls.append({"prompt": prompt, "system": system})
            return dict(reply)

        monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
        return calls, reply

    def _chat(self, client, message="which pool does /api/db use"):
        return client.post("/v1/perf/chat", json={"message": message, "screen": "new-test"})

    def test_the_prompt_carries_the_matching_excerpt_and_the_reply_cites_it(self, tmp_path, gateway):
        calls, _ = gateway
        client, kb = _client(tmp_path)
        _upload(client)
        kb.wait_idle()
        body = self._chat(client).json()
        assert "reads one row from Postgres" in calls[0]["prompt"]
        assert {s["source"] for s in body["sources"]} == {"PERF_LAB_PRODUCT.md"}

    def test_with_nothing_indexed_it_still_answers_and_cites_nothing(self, tmp_path, gateway):
        calls, _ = gateway
        client, _ = _client(tmp_path)
        body = self._chat(client).json()
        assert body["sources"] == []
        assert "no uploaded document matched" in calls[0]["prompt"]

    def test_the_sla_in_the_prompt_is_the_one_on_disk(self, tmp_path, gateway):
        calls, _ = gateway
        repo = Path(__file__).resolve().parents[1]
        sla = tmp_path / "slo.yaml"
        sla.write_text((repo / "config" / "slo.yaml").read_text(encoding="utf-8").replace("p99_ms: 120", "p99_ms: 175"),
                       encoding="utf-8")
        client, _ = _client(tmp_path)
        client.app.state.perf_context = PerfContext(root=repo, sla_path=str(sla), results_dir=str(tmp_path / "r"),
                                                    state_dir=tmp_path / "state")
        self._chat(client)
        assert "/api/db p99 <= 175 ms" in calls[0]["prompt"]
        assert "120 ms" not in calls[0]["prompt"]

    def test_markup_in_a_reply_is_escaped_except_the_allowed_tags(self, tmp_path, gateway):
        _, reply = gateway
        reply["text"] = '<strong>ok</strong><img src=x onerror="alert(1)"><script>steal()</script>'
        client, _ = _client(tmp_path)
        out = self._chat(client).json()["reply"]
        assert out.startswith("<strong>ok</strong>")
        assert "<img" not in out and "<script" not in out
        assert "&lt;script&gt;" in out


class TestReuploadReplaces:
    def test_the_same_text_again_is_unchanged(self, tmp_path):
        client, kb = _client(tmp_path)
        _upload(client)
        _upload(client)
        kb.wait_idle()
        first, second = kb.uploads()
        assert not first["unchanged"] and second["unchanged"]
        assert [d["version"] for d in kb.documents()] == [1]

    def test_new_text_supersedes_and_the_old_text_is_no_longer_found(self, tmp_path):
        client, kb = _client(tmp_path)
        _upload(client)
        kb.wait_idle()
        _upload(client, text="# Replaced\n\nPerfLab now reads from Redis only; the HikariCP pool is gone.")
        kb.wait_idle()
        assert [d["version"] for d in kb.documents()] == [2]
        texts = " ".join(h["text"] for h in kb.search("httpbin downstream delay", limit=8))
        assert "httpbin" not in texts
