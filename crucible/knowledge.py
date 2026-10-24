"""The one knowledge document store (DESIGN.md §14, Knowledge RAG).

Two doors, one room. Documents arrive by two routes and land in the same place:

- the dashboard's upload, ``POST /v1/perf/knowledge/documents`` -> ``submit``;
- the agent API, ``POST /v1/agent/documents`` -> ``submit_uri``, and the
  agent's own index skill -> ``index_now``.

The Live AI chat, the wizard's AI suggestions and the S17 agent's answers all
search this one store. Until 24 October 2026 the agent kept its own copy in
memory.sqlite, so a document indexed through one door was invisible to whatever
read the other (see docs/KNOWLEDGE_STORE.md).

Dropping a file in the browser sends nothing. Clicking "Upload & index" sends it;
the server saves the original and queues it, and the call returns at once.
Indexing then runs on ONE background worker, because it makes one gateway embed
per chunk under a cooldown and takes minutes: run inside the request it would
freeze the server for that whole time. One worker also means one writer, since
`MemoryStore` embeds inside its write transaction and a second writer would hit
SQLite's lock.

Originals live in `<state>/knowledge/`, the index in `<state>/knowledge-index/`.
Keeping the originals means the corpus can be re-indexed when the embedder
changes. Every operation opens its own store connection: SQLite connections may
not cross threads.
"""
from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .core.memory import MemoryKind, MemoryScope, MemoryStore
from .core.memory.chunking import prepare_markdown, semantic_chunks
from .core.memory.embeddings import Embedder, GlcEmbedder, OllamaNomicEmbedder

ALLOWED_SUFFIXES = (".md", ".txt")
MAX_CHARS = 500_000
SCOPE = MemoryScope("default")
SOURCE_SCHEME = "upload://"
# Under the state directory ($CRUCIBLE_STATE_DIR, default ~/.crucible). The root
# is deployment configuration; the layout inside it is the code's contract.
KNOWLEDGE_DIR = "knowledge"
KNOWLEDGE_INDEX_DIR = "knowledge-index"


class UploadRefused(ValueError):
    pass


@dataclass
class Upload:
    id: str
    filename: str
    status: str  # queued | indexing | indexed | failed
    queued_at: float
    source_uri: str = ""
    tenant_id: str = "default"
    chunks: int | None = None
    unchanged: bool = False
    error: str | None = None


def default_embedder() -> Embedder:
    glc_url = os.getenv("GLC_BASE_URL", "").strip().rstrip("/")
    return GlcEmbedder(glc_url) if glc_url else OllamaNomicEmbedder()


def _check_text(text: str) -> None:
    if "\x00" in text:
        raise UploadRefused("file is not text")
    if not text.strip():
        raise UploadRefused("file is empty")
    if len(text) > MAX_CHARS:
        raise UploadRefused(f"file is larger than {MAX_CHARS:,} characters")


def check_upload(filename: str, text: str) -> None:
    if not filename or Path(filename).name != filename or len(filename) > 200:
        raise UploadRefused("filename must be a plain file name")
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise UploadRefused(f"only {', '.join(ALLOWED_SUFFIXES)} files can be indexed")
    _check_text(text)


def _original_name(source_uri: str) -> str:
    """A stable, safe file name for a document that arrived by URI rather than upload."""
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", source_uri.rstrip("/").rsplit("/", 1)[-1])[:80] or "document"
    return f"{hashlib.sha256(source_uri.encode()).hexdigest()[:12]}-{tail}"


class KnowledgeBase:
    @classmethod
    def under(cls, state_dir: Path, embedder: Embedder) -> KnowledgeBase:
        return cls(Path(state_dir) / KNOWLEDGE_DIR, Path(state_dir) / KNOWLEDGE_INDEX_DIR, embedder)

    def __init__(self, docs_dir: Path, index_dir: Path, embedder: Embedder):
        self.docs_dir = Path(docs_dir)
        self.index_dir = Path(index_dir)
        self.db_path = self.index_dir / "knowledge.sqlite"
        self.embedder = embedder
        self._uploads: dict[str, Upload] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str, str, MemoryScope]] = queue.Queue()
        self._worker: threading.Thread | None = None

    def _store(self) -> MemoryStore:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        return MemoryStore(self.db_path, embedder=self.embedder)

    def _save_original(self, name: str, text: str) -> None:
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        partial = self.docs_dir / f".{name}.partial"
        partial.write_text(text, encoding="utf-8", newline="")
        partial.replace(self.docs_dir / name)

    # -- the doors -------------------------------------------------------

    def submit(self, filename: str, text: str, author: str) -> Upload:
        """The dashboard's door: a plain file name, indexed for everyone."""
        check_upload(filename, text)
        self._save_original(filename, text)
        return self._enqueue(filename, SOURCE_SCHEME + filename, text, author, SCOPE)

    def submit_uri(self, source_uri: str, text: str, author: str, scope: MemoryScope) -> Upload:
        """The agent API's door: any source URI, in the caller's scope."""
        if not source_uri or len(source_uri) > 2000:
            raise UploadRefused("source_uri is required")
        _check_text(text)
        self._save_original(_original_name(source_uri), text)
        return self._enqueue(source_uri, source_uri, text, author, scope)

    def index_now(self, source_uri: str, text: str, author: str, scope: MemoryScope,
                  timeout_s: float = 3600.0) -> dict[str, Any]:
        """For the agent's index skill, which needs the result: queue, then wait for it.

        It still goes through the queue, never straight to the store, so there is
        only ever one writer.
        """
        upload = self.submit_uri(source_uri, text, author, scope)
        if not self._done[upload.id].wait(timeout_s):
            raise TimeoutError(f"indexing {source_uri} did not finish within {timeout_s:.0f} s")
        if upload.status == "failed":
            raise RuntimeError(upload.error or "indexing failed")
        return self._results[upload.id]

    # -- the one queue ---------------------------------------------------

    def _enqueue(self, name: str, source_uri: str, text: str, author: str, scope: MemoryScope) -> Upload:
        upload = Upload(id=uuid.uuid4().hex[:12], filename=name, status="queued", queued_at=time.time(),
                        source_uri=source_uri, tenant_id=scope.tenant_id)
        with self._lock:
            self._uploads[upload.id] = upload
            self._done[upload.id] = threading.Event()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="knowledge-indexer", daemon=True)
                self._worker.start()
        self._queue.put((upload.id, text, author, scope))
        return upload

    def _drain(self) -> None:
        while True:
            upload_id, text, author, scope = self._queue.get()
            try:
                self._index(upload_id, text, author, scope)
            finally:
                self._done[upload_id].set()
                self._queue.task_done()

    def _index(self, upload_id: str, text: str, author: str, scope: MemoryScope) -> None:
        upload = self._uploads[upload_id]
        upload.status = "indexing"
        try:
            store = self._store()
            try:
                prepared, preprocessing = prepare_markdown(text)
                chunks = semantic_chunks(prepared, store.embedder, preprocess=False)
                ingested = store.ingest_document(
                    source_text=text, prepared_text=prepared, chunks=chunks,
                    source_uri=upload.source_uri, scope=scope,
                    source_author=author, preprocessing=preprocessing,
                )
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - reported on the upload, never swallowed
            upload.status, upload.error = "failed", f"{type(exc).__name__}: {exc}"
            return
        upload.chunks = len(ingested["record_ids"])
        upload.unchanged = bool(ingested["idempotent"])
        self._results[upload_id] = {
            "source_uri": upload.source_uri, "chunks": upload.chunks, "record_ids": ingested["record_ids"],
            "document_id": ingested["document_id"], "document_version": ingested["version"],
            "idempotent": ingested["idempotent"], "preprocessing": preprocessing,
            "provenance": {"source_sha256": ingested["source_sha256"], "source_author": author},
            "manifest": [{"id": rid, "ordinal": c.ordinal, "heading": c.heading, "text": c.text,
                          "segmentation": c.segmentation} for rid, c in zip(ingested["record_ids"], chunks)],
        }
        upload.status = "indexed"

    def wait_idle(self) -> None:
        self._queue.join()

    # -- reading ---------------------------------------------------------

    def upload(self, upload_id: str) -> dict[str, object] | None:
        with self._lock:
            found = self._uploads.get(upload_id)
            return asdict(found) if found else None

    def uploads(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(u) for u in sorted(self._uploads.values(), key=lambda u: u.queued_at)]

    def pending(self) -> int:
        with self._lock:
            return sum(u.status in ("queued", "indexing") for u in self._uploads.values())

    def documents(self, tenant_id: str = SCOPE.tenant_id) -> list[dict[str, object]]:
        if not self.db_path.exists():
            return []
        store = self._store()
        try:
            rows = store.active_documents(tenant_id)
        finally:
            store.close()
        for row in rows:
            row["filename"] = str(row["source_uri"]).removeprefix(SOURCE_SCHEME)
        return rows

    def recall(self, query: str, scope: MemoryScope = SCOPE, limit: int = 4) -> list[Any]:
        """Document chunks as memory records, for callers that merge them with other memory."""
        if not self.db_path.exists():
            return []
        store = self._store()
        try:
            return store.recall(query, scope, kinds=[MemoryKind.DOCUMENT_CHUNK], limit=limit)
        finally:
            store.close()

    def search(self, query: str, limit: int = 4, scope: MemoryScope = SCOPE) -> list[dict[str, object]]:
        return [{"text": hit.text,
                 "source": hit.sources[0].uri.removeprefix(SOURCE_SCHEME) if hit.sources else "",
                 "heading": hit.metadata.get("heading")} for hit in self.recall(query, scope, limit)]
