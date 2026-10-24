"""Knowledge documents uploaded from the dashboard (DESIGN.md §14, Knowledge RAG).

Dropping a file in the browser sends nothing. Clicking "Upload & index" sends
it; the server saves the original and queues it, and the upload call returns at
once. Indexing then runs on a single background worker, because it makes one
gateway embed per chunk under a cooldown and takes minutes. Run inside the
upload call, it would block the server's event loop for that whole time.

Originals live in `<state>/knowledge/`, and the index in `<state>/knowledge-index/`.
Keeping the originals means the corpus can be re-indexed when the embedder
changes (DESIGN.md §14). The index is not the agent runtime's memory.sqlite:
`MemoryStore` embeds inside its write transaction, so a long index would hold
that database's write lock for minutes, and every store in one directory shares
a single memory.faiss sidecar.

Every operation opens its own store connection: SQLite connections may not
cross threads.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.memory import MemoryKind, MemoryScope, MemoryStore
from ..core.memory.chunking import prepare_markdown, semantic_chunks
from ..core.memory.embeddings import Embedder, GlcEmbedder, OllamaNomicEmbedder

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
    chunks: int | None = None
    unchanged: bool = False
    error: str | None = None


def default_embedder() -> Embedder:
    glc_url = os.getenv("GLC_BASE_URL", "").strip().rstrip("/")
    return GlcEmbedder(glc_url) if glc_url else OllamaNomicEmbedder()


def check_upload(filename: str, text: str) -> None:
    if not filename or Path(filename).name != filename or len(filename) > 200:
        raise UploadRefused("filename must be a plain file name")
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise UploadRefused(f"only {', '.join(ALLOWED_SUFFIXES)} files can be indexed")
    if "\x00" in text:
        raise UploadRefused("file is not text")
    if not text.strip():
        raise UploadRefused("file is empty")
    if len(text) > MAX_CHARS:
        raise UploadRefused(f"file is larger than {MAX_CHARS:,} characters")


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
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._worker: threading.Thread | None = None

    def _store(self) -> MemoryStore:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        return MemoryStore(self.db_path, embedder=self.embedder)

    def _save_original(self, filename: str, text: str) -> None:
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        partial = self.docs_dir / f".{filename}.partial"
        partial.write_text(text, encoding="utf-8", newline="")
        partial.replace(self.docs_dir / filename)

    def submit(self, filename: str, text: str, author: str) -> Upload:
        check_upload(filename, text)
        self._save_original(filename, text)
        upload = Upload(id=uuid.uuid4().hex[:12], filename=filename, status="queued", queued_at=time.time())
        with self._lock:
            self._uploads[upload.id] = upload
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="knowledge-indexer", daemon=True)
                self._worker.start()
        self._queue.put((upload.id, text, author))
        return upload

    def _drain(self) -> None:
        while True:
            upload_id, text, author = self._queue.get()
            try:
                self._index(upload_id, text, author)
            finally:
                self._queue.task_done()

    def _index(self, upload_id: str, text: str, author: str) -> None:
        upload = self._uploads[upload_id]
        upload.status = "indexing"
        try:
            store = self._store()
            try:
                prepared, preprocessing = prepare_markdown(text)
                chunks = semantic_chunks(prepared, store.embedder, preprocess=False)
                ingested = store.ingest_document(
                    source_text=text, prepared_text=prepared, chunks=chunks,
                    source_uri=SOURCE_SCHEME + upload.filename, scope=SCOPE,
                    source_author=author, preprocessing=preprocessing,
                )
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - reported on the upload, never swallowed
            upload.status, upload.error = "failed", f"{type(exc).__name__}: {exc}"
            return
        upload.chunks = len(ingested["record_ids"])
        upload.unchanged = bool(ingested["idempotent"])
        upload.status = "indexed"

    def wait_idle(self) -> None:
        self._queue.join()

    def uploads(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(u) for u in sorted(self._uploads.values(), key=lambda u: u.queued_at)]

    def documents(self) -> list[dict[str, object]]:
        if not self.db_path.exists():
            return []
        store = self._store()
        try:
            rows = store.active_documents(SCOPE.tenant_id)
        finally:
            store.close()
        for row in rows:
            uri = str(row["source_uri"])
            row["filename"] = uri.removeprefix(SOURCE_SCHEME)
        return rows

    def search(self, query: str, limit: int = 4) -> list[dict[str, object]]:
        if not self.db_path.exists():
            return []
        store = self._store()
        try:
            hits = store.recall(query, SCOPE, kinds=[MemoryKind.DOCUMENT_CHUNK], limit=limit)
        finally:
            store.close()
        return [{"text": hit.text,
                 "source": hit.sources[0].uri.removeprefix(SOURCE_SCHEME) if hit.sources else "",
                 "heading": hit.metadata.get("heading")} for hit in hits]
