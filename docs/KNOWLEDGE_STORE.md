# The knowledge store: two doors, one room

How documents (runbooks, architecture notes, incident write-ups, product/NFR
references) get into Crucible, where they live, and who reads them.
Decided by the operator on 24 October 2026. Rationale: DESIGN.md §14.

## The picture

```
 Wizard "Upload & index"                S17 agent / other API callers
          │                                        │
          ▼                                        ▼
 POST /v1/perf/knowledge/documents        POST /v1/agent/documents
   (file name, .md/.txt)                    (any source URI + scope)
          │                                        │
          └───────────────────┬────────────────────┘
                              ▼
               ONE indexing queue, ONE worker
               (background; the server stays responsive)
                              ▼
               ┌────────────────────────────────┐
               │ ONE document store             │
               │ originals: ~/.crucible/knowledge/        │
               │ index:     ~/.crucible/knowledge-index/  │
               └───────────────┬────────────────┘
                               ▼
        Live AI chat · AI suggestions (SLA, scenarios) · S17 agent answers
```

There are two **doors** because there are two kinds of caller: the dashboard,
which uploads files, and the agent API, which indexes by URI within a tenant
scope. There is one **room**: whichever door a document comes through, every
reader sees it.

## Why one store

Until 24 October 2026 each door led to its own store. A document uploaded in the
wizard was invisible to the S17 agent, and a document given to the agent API was
invisible to the chat. In practice the agent store on Box B held a second copy
of `PERF_LAB_PRODUCT.md`. Two copies drift apart, the same lesson AGENTS.md
records for `CRUCIBLE_TEST_ASSERTIONS.md`.

## What lives where (under `$CRUCIBLE_STATE_DIR`, default `~/.crucible`)

| Path | Contents | Written by |
|---|---|---|
| `knowledge/` | Every original document, exactly as received. Uploads keep their file name; API documents are named `<hash>-<name>` from their URI. | both doors |
| `knowledge-index/knowledge.sqlite` | Document versions and their chunks, each with a 768-dimension vector | the one worker |
| `knowledge-index/knowledge.sqlite.faiss` | The vector index for fast search | the one worker |
| `memory.sqlite` (+ `memory.sqlite.faiss`) | The S17 agent's **own** memory: facts it was told, episodes, playbooks, audit. Also each campaign's SLA as a Policy record, the second lock that stops the agent changing its own goal (§4.4). Policy and audit records are never embedded or indexed. **No documents.** | agent runtime, campaigns |

So there are two vector indexes: one for documents, one for the agent's facts
and episodes. Each is named after its database. Before 24 October every store
shared one `memory.faiss` and overwrote the others' index every time one was
opened, and campaigns kept the SLA in a separate `memory.db`, now retired.

## The two doors

### Dashboard: `POST /v1/perf/knowledge/documents`
- Body: `{"filename": "PERF_LAB_PRODUCT.md", "text": "..."}`. Only `.md` and
  `.txt`, at most 500,000 characters, a plain file name (no path).
- Needs the control token. Returns **202** with an upload id and status
  `queued`.
- Status: `GET /v1/perf/knowledge/documents` lists the indexed documents and
  this server's uploads with their status.

### Agent API: `POST /v1/agent/documents`
- Body: `{"tenant_id": "...", "source_uri": "file://capacity.md", "text": "...",
  "project_id"?, "user_id"?, "agent_id"?, "source_author"?}`.
- **Needs the control token** (new on 24 October 2026; it was open before). Indexed
  text reaches the operator's chat and the AI suggestions.
- Returns **202** `{"id", "status": "queued", "source_uri", "poll"}`. This
  **changed** on 24 October 2026: it used to block until indexed and return 200
  with the chunk manifest.
- Status: `GET /v1/agent/documents/{id}` → `queued | indexing | indexed |
  failed`, the chunk count, or the error. `GET /v1/agent/documents?tenant_id=…`
  lists a tenant's documents.
- The agent's own "index a file" skill uses `index_now`. It goes through the
  same queue and waits for its own item, so it still gets the full manifest.

## Behaviour you can rely on

- **Dropping a file sends nothing.** Clicking **Upload & index** sends it.
- **Nothing should use a document before it is indexed**, and nothing does:
  - the file's chip reads "queued", then "indexing…";
  - the chat announces "indexed — N chunks" when it's ready;
  - the AI-suggestion buttons are disabled until every pending upload has
    finished, and the server refuses a suggestion with 409 meanwhile.
- **Why in the background:** one document makes one gateway embedding call per
  chunk, under the gateway's cooldown, so it takes minutes. Indexing inside the
  request would freeze the whole server: no refreshes, no live campaign view,
  no approvals.
- **One worker, one writer.** Both doors and the agent skill share one queue. A
  second writer would hit SQLite's lock, because the store embeds inside its
  write transaction.
- **Re-uploading a file** with the same name and identical text is reported as
  `unchanged`. New text becomes version 2, and the old text stops being found.
- **A failed index** shows `failed: <error>` on the chip or status, and is never
  listed as indexed. The usual cause is a gateway 503 after its retries.
- **Scope:** dashboard uploads are in tenant `default`. API documents keep the
  caller's scope, and a narrower scope never leaks to a wider one.

## Who reads it

- **Live AI chat** (`POST /v1/perf/chat`): the four best-matching chunks go into
  every prompt, and the reply lists them as "Sources: file › heading".
- **AI suggestions** (`POST /v1/perf/suggest`): the same search, keyed on your
  goal and endpoint.
- **S17 agent**: `recall_evidence` merges its own facts, episodes and playbooks
  with document chunks from this store.

## Upgrading an existing box (Box B)

- **Old document chunks** in `memory.sqlite` (the hand-indexed copy of
  `PERF_LAB_PRODUCT.md`) are no longer read. Upload the file once through the
  wizard (checklist step 9c) and it lands in the shared store.
- **The old `~/.crucible/memory.faiss`** and `memory.faiss.ids.json` are no
  longer read by anything. Each store rebuilds its own index from the vectors
  saved in its database, so nothing is re-embedded. You can delete the old
  files.
- **The old `~/.crucible/memory.db`** (and any `memory.db.faiss*`) is retired:
  campaigns keep the SLA lock in `memory.sqlite` now. Delete them
  (operator decision, 24 October 2026).
- **Any script that calls `/v1/agent/documents`** must now send the control
  token and poll for the result.
