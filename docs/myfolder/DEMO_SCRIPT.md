# Crucible — End-to-End Demo Script

*Updated 25 October 2026 for the Part B changes and the chat that discusses runs.
Practise it once end to end (Part P) before recording.*

---

## How to use these documents together

| Document | When to open | Purpose |
|---|---|---|
| **[crucible-product-doc.html](crucible-product-doc.html)** | **Share with trainer/audience the day before** | Pre-demo product brief: problem, architecture, five principles, NFR table, memory taxonomy, design concepts. |
| **[DemoExecutionGuide.md](DemoExecutionGuide.md)** | 30 min before recording, and keep it open | Backstage manual: Box A/B state, resets, troubleshooting. |
| **[COMPLETION_CHECKLIST.md](../COMPLETION_CHECKLIST.md) step 9** | The day before | The one-time dry run. **9g** covers everything changed on 24–25 October. |
| **This file** | Practice run, then recording | Part 0 = prep, Part P = practice run, Steps 1–16 = what to say and click. |

---

## Part 0 — Prep commands (run before practice AND before recording)

> Run these on **Box B** over SSH. Use `nohup`, not tmux. Always source `.env` first.

### 0.1 Pull latest code (first time after 25 October: follow checklist 9g)

```bash
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" ubuntu@150.136.143.227

cd ~/crucible
git status --short
# Launch rewrites config/slo.yaml and the profile. If either is listed as modified, stash it first:
git stash push config/slo.yaml config/profiles/spring-boot.yaml
git pull origin capstone/perf-agent
uv sync --group load

# One time only, after the 24 October changes (checklist 9g):
rm -f ~/.crucible/memory.faiss ~/.crucible/memory.faiss.ids.json
rm -f ~/.crucible/memory.db ~/.crucible/memory.db.faiss ~/.crucible/memory.db.faiss.ids.json

# Restart the UI server
pkill -f "crucible serve"
set -a; . ./.env; set +a
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
nohup uv run crucible serve > ~/crucible-serve.log 2>&1 &
curl -s http://127.0.0.1:8113/healthz        # {"ok":true,...}
```

> If Box B runs the UI as the `crucible-ui` systemd service instead, use
> `sudo systemctl restart crucible-ui` and `systemctl status crucible-ui`, not
> the nohup lines. Don't run both.

### 0.2 Verify Box A is reachable and pool=2

```bash
curl -s http://10.0.0.79:8080/actuator/health \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# Expected: UP

curl -s http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# Expected: 2.0 (the broken state the demo fixes). If 20.0, see 0.3
```

### 0.3 Reset Box A to pool=2 (only if needed)

```bash
cd ~/perf-lab
git checkout perftest_sandbox
nano src/main/resources/application.properties
# set: spring.datasource.hikari.maximum-pool-size=2
git add src/main/resources/application.properties
git commit -m "reset: pool=2 for demo baseline"
git push origin perftest_sandbox
# Wait ~90 s for the post-receive hook, then re-check 0.2
```

### 0.4 Warm up the GLC v5 gateway

```bash
curl -s -X POST https://glc-v5-rraghu214.onrender.com/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"ping"}]}' | head -c 200; echo
# Burns off the Render cold start (up to 60 s). If it took >30 s, run it again.
```

### 0.5 Knowledge document: upload it from the UI

Upload it from the wizard (Step 2 of the script), not with curl: that is the
path being demonstrated. To have it ready before recording, upload it during
the practice run. The store keeps it, and identical text is not re-embedded.

Check on Box B:
```bash
ls -lh ~/.crucible/knowledge/          # PERF_LAB_PRODUCT.md, the original as uploaded
ls -lh ~/.crucible/knowledge-index/    # knowledge.sqlite + knowledge.sqlite.faiss, both non-zero
```
*(The agent API door, `POST /v1/agent/documents`, feeds the same store and
needs the control token. See `docs/KNOWLEDGE_STORE.md`.)*

### 0.6 Confirm the UI endpoints

```bash
curl -s http://127.0.0.1:8113/v1/perf/jobs | python3 -m json.tool | head
curl -s http://127.0.0.1:8113/v1/perf/campaign/active | python3 -m json.tool
# Expected: {"active": false, ...}
curl -s http://127.0.0.1:8113/v1/perf/runs | python3 -m json.tool | head -20
# The run ids on disk: note two of them for Step 13
ls ~/crucible/results/                  # manifests: <run>.json, ceiling probes <run>.ceiling.json
```

### 0.7 Open the tunnel and browser

```powershell
# Windows laptop. Keep this terminal open throughout
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" `
    -L 9113:127.0.0.1:8113 `
    ubuntu@150.136.143.227
# Locust runs --headless: there is no Locust web UI and no port 8089 to forward.
```

Open **http://localhost:9113/dashboard**, then:
1. **Settings → Control token** → paste `CRUCIBLE_CONTROL_TOKEN` (`grep CRUCIBLE_CONTROL_TOKEN ~/crucible/.env`) → Save
2. **Results** → a real run loads (chart and experiment table)
3. **Active** → TR-004 demo view (no live banner) when nothing is running

---

## Part P — Practice run (do this once, a day before recording)

Run the whole flow once for real, so that recording only replays things you
have already seen work. Times are approximate.

| # | Do | Pass when |
|---|---|---|
| P1 | Part 0 complete | healthz ok, pool=2, gateway warm, token saved |
| P2 | Wizard → Target & Repo → **Knowledge Docs**: drop `PERF_LAB_PRODUCT.md` → **Upload & index** | Chip goes queued → indexing… → chat says "indexed — N chunks" (a few minutes). The ✨ buttons stay disabled while indexing |
| P3 | Live AI **ON**, ask: *How many requests must Micrometer meters be initialised within, and how long may PerfLab take to start?* | Reply says **10 requests** and **60 seconds**, with a "Sources: PERF_LAB_PRODUCT.md" line |
| P4 | Goal & SLA → **✨ AI suggestion** | Fills p99/error budget from the model; noise stays read-only at 2.08% |
| P5 | Scenarios → choose **Advisory** | Scenarios fill automatically; edit one (users / duration / ramp) |
| P6 | Budget | Rows follow mode, experiments and scenarios; a wall-clock cap below the baselines shows ⚠ |
| P7 | Review | Preflight list includes "load profile drives the SLA endpoint ✓"; the diff box shows exactly what Launch writes |
| P8 | **🚀 Launch** (Advisory) | Jobs shows `crucible run --advisory --all-scenarios`; finishes with verdict NOT_MEASURED and up to 3 ranked alternatives, each with a diff; nothing pushed to `perftest_sandbox` |
| P9 | Chat (Live AI ON): *why did you rank #1 above #2?* | Names the run, quotes cause, `from -> to` change and baseline evidence (pending connections), calls 140 ms a *prediction* |
| P10 | Back to wizard → **Semi-Automated** → Launch | Active tab shows the live overlay within ~5 s; the approval gate shows the exact diff |
| P11 | While it runs: raise p99 in the wizard → **💾 Save changes** | The campaign pauses at the next experiment; Resume records an `amendments` entry |
| P12 | **Approve** in the UI | Gate → "Change applied — measuring now"; after the window, verdict + kept/reverted |
| P13 | Results → ↺ | Real Before/After chart for this run |
| P14 | Chat: *why was experiment 1 kept? <run-id>* (paste the real id) | Quotes the MEASURED before → after and the verdict reason |
| P15 | Chat: *which runs do I have?* and *tell me about zz-run-77* | Lists the runs on disk (ceiling probes included); says zz-run-77 is **not on disk** |
| P16 | History & Diff | Both runs appear |

If any line fails, stop and fix it, or drop that step from the recording.
Don't record a step you haven't seen pass.

---

## Pre-flight checklist (confirm before pressing Record)

```
Pre-read : crucible-product-doc.html sent to trainer
Box B    : crucible serve running on 8113 (healthz ok)
Tunnel   : 9113 forwarded (no 8089: Locust is headless)
Box A    : pool_size = 2.0 confirmed
GLC v5   : warmed (0.4)
Knowledge: PERF_LAB_PRODUCT.md listed as indexed; knowledge-index/ non-empty (0.5)
Token    : saved in Settings
Results  : real KPIs + chart visible
Active   : TR-004 demo view (no live overlay)
Run ids  : two real run ids noted for Step 13 (0.6)
Practice : Part P passed
```

---

## The story in one sentence

*"I deployed a Spring Boot service, pointed Crucible at it, and it found, fixed, and proved a connection-pool bottleneck, with me approving the one change and every number measured."*

---

## Step 1 — Problem statement + architecture (2 min) [EXPLAIN]

**Say:**
"Performance engineering today is manual: load test, open Grafana, form a guess,
apply a change, re-test. That loop takes hours, needs three tools, and falls
apart when the person who knows the tools isn't the person who owns the service.

Crucible closes that loop. It runs its own experiments, diagnoses from the
telemetry it can actually reach, proposes a bounded change, waits for your
approval, applies it, and measures again. Every claim is a measurement."

**Show:** `localhost:9113/dashboard`, the overview.

**Say:**
"Three pieces. Box A is the Spring Boot service on Oracle Cloud, PerfLab, with
Postgres, Redis, Prometheus and Jaeger. Box B, this machine, runs Crucible and
the Locust load generator. GLC v5, hosted on Render, is the LLM gateway.
Crucible never holds model keys; the gateway rotates them."

---

## Step 2 — Knowledge RAG: upload and index (2 min) [LIVE]

**Navigate to:** New Test Run → **1 Target & Repo** → **Knowledge Docs**.

**Say:**
"Before a campaign, I can give Crucible the service's own documents: architecture,
runbooks, post-mortems. Markdown or text."

**Action:** drop `PERF_LAB_PRODUCT.md` on the zone. It becomes a chip reading
"ready to upload". Click **Upload & index**.

**Say:**
"Dropping only lists it. Upload sends it. The server keeps the original in
`~/.crucible/knowledge/`, queues it, and returns at once. One background worker
indexes it, so the server never blocks. Each chunk is embedded with Gemini
embedding-001 at 768 dimensions through the gateway, into its own FAISS index in
`knowledge-index/`. The same store feeds the chat, the AI suggestions and the
agent API.

The embedder is pinned, like the model. Two different embedding models at 768
dimensions produce incomparable vectors, so changing the embedder means
re-indexing, never querying across the two. We keep the originals for exactly
that reason."

*(If it was already uploaded in practice, the chip reports "unchanged since
last upload". Say "identical text is never re-embedded" and move on.)*

**Show on Box B (optional):**
```bash
ls -lh ~/.crucible/knowledge/ ~/.crucible/knowledge-index/
```

---

## Step 3 — Grounded chat (1.5 min) [LIVE]

**Toggle Live AI ON.** Type:
`How many requests must Micrometer meters be initialised within, and how long may PerfLab take to start?`

→ The reply says 10 requests and 60 seconds, with **Sources: PERF_LAB_PRODUCT.md**.

**Say:**
"That answer exists only in the document I just uploaded. Every chat turn searches
the knowledge store and cites the file it used. Its SLA and profile facts are read
from the YAML files on disk, not from a prompt someone wrote once. The reply is
HTML-escaped, because uploaded text reaches the prompt and this browser holds the
control token."

*(Leave Live AI ON. The rest of the demo uses it. If the gateway is slow, see
Emergency fallbacks.)*

---

## Step 4 — Input data + request templates (1 min) [UI]

Wizard step **3 Scenarios**, then the **Input Data & Request Templates** card.

**Action:** upload a small CSV (e.g. `sizes.csv` with a `size` column). It shows
its row count and columns. Add a request row: `/api/orders`, query `size=${size}`,
then map `size` → `sizes.csv` / `size`.

**Say:**
"Realistic load needs realistic inputs. A request is a path on the target, never
another host. Each `${var}` must be mapped to an uploaded file and column, and
that is checked before anything runs. Rows are cycled. Templated endpoints are
judged one endpoint at a time."

*(Remove the request row again before launching, unless you want the
demo run to use it.)*

---

## Step 5 — Goal & SLA (1 min) [LIVE]

Wizard step **2 Goal & SLA**. It opens with `slo.yaml`'s real values: `/api/db`,
120 ms, 50 users.

Click **✨ AI suggestion**. The model proposes the SLA from the goal and the
documents. Every value is bounds-checked before it fills the form.

**Say:**
"The noise floor, 2.08%, is read-only. It was measured on this box by running the
same load three times with nothing changed. A change smaller than that is
inconclusive and is reverted, because it can't be told apart from noise."

---

## Step 6 — Mode, scenarios, hooks, budget, review (2.5 min) [LIVE]

Wizard step **3 Scenarios**. Click each **Test mode** card:

- **Advisory**: "AI diagnoses and suggests up to three ranked changes, each with
  its exact diff. Nothing is applied. No repository or deploy pipeline needed."
  Scenarios fill automatically.
- **Semi-Automated**: "You approve each diff. The approval is bound to the exact
  change: you can't approve one thing and deploy another."
- **Fully Automated**: "No approval prompts, but the agent's authority is the
  property list and bounds in `profile.yaml`. The guardrails make this safe, not
  trust in the model. It still stops after three misses in a row."
- **Ceiling Probe**: "A different question: how much headroom do I have? Load
  rises in steps up to bounds I set, until the SLA is first missed. Only an
  operator can start it, because it changes the load profile, which the agent is
  forbidden to touch."

Point at the scenario rows (users, duration, **ramp**, **Mix**):
"Ramp sets Locust's spawn rate. A mix such as `/api/db=60, /api/orders=40` runs
several endpoints and judges each against its own target. A change that speeds
one endpoint while slowing another is WORSE, unless I tick **Accept trade-offs**
and the slower one is still within its target."

Point at **Hooks**: "before_each, after_each, on_abort: Python files, run
isolated with an allowlisted environment. Never the control token. A failing hook
blocks the experiment."

Click **Next: Budget**.
"The table is computed from the mode, the experiment count and the scenarios.
Wall clock is the real budget, not dollars. The caps pause the run, they never
throw work away. I resume with a higher cap or abort to get the report. If a cap
can't even fit the baselines, it warns me here."

Set mode back to **Semi-Automated** → **Next: Review**.
"This is the exact diff Launch will write to `slo.yaml` and the target profile,
and the preflight checks, including that the load script actually drives the SLA
endpoint. That is what `crucible plan` prints, from the same source."

---

## Step 7 — Guardrails + sandbox structure (2 min) [UI]

**Navigate to:** Guardrails tab.

"Two lists. The core protections cannot be disabled."

Policy row: "The SLA is Policy memory. The agent has no write permission for it,
ever. Two independent locks: a protected path in the file guard, and the Policy
memory kind. Both would have to fail. The operator can still change it, mid-run
too, and every such edit is recorded on the manifest as an amendment."

Sandbox row: "Crucible writes to exactly one branch, `perftest_sandbox`, on the
PerfLab repo, never its base branch. Each experiment is one commit of one file,
pushed to that branch. The post-receive hook on Box A rebuilds."

Allowed properties: "SKILL.md tells the model how this runtime fails. It goes
into the prompt and grants nothing. `profile.yaml` grants authority. A SKILL.md
saying 'raise the pool to 10,000' has zero effect."

---

## Step 8 — Preflight (1.5 min) [LIVE]

**Navigate to:** Preflight → **Run checks** (read-only).

"Preflight exercises the loop once, cheaply, before an eight-experiment campaign.
With the apply probe it deploys a no-op change, reads the property back from the
target, reverts, and reads it back again: the same steps every experiment runs.
If the key, the hook or the config path is wrong, it fails in five minutes, not
forty."

*(The apply probe restarts Box A. The UI asks for confirmation naming the target.
Run it in practice, not necessarily on camera.)*

---

## Step 9 — Live campaign: launch, approve, edit (3 min) [LIVE]

**Launch** from Review (Semi-Automated), or have one already running from just
before recording. First proposal in ~7 minutes.

**Navigate to:** Active. A green ● LIVE overlay appears within 5 s.

"The baseline measured p99 above the 120 ms SLA. The pool is 2 of 2, saturated,
with 43 requests waiting at peak."

PDMA graph: "Perception: the collector read HikariCP metrics, converted
Micrometer's seconds to milliseconds before the model saw them, and sampled the
pending gauge during load, so we have the peak, not the post-run zero. Memory:
the journal was searched, so a disproven hypothesis isn't proposed again."

Approval gate: "The proposal is pool 2 → 20, and this exact diff is what I
approve. Then Crucible commits that one file, pushes, waits until the target
itself reports the new value, and only then runs a fresh measurement window."

**Click Approve.**

**Optional, mid-run edit (30 s):** "I can change the goal mid-run." Raise p99 in
the wizard → **💾 Save changes**. "It is taken at the next experiment boundary,
recorded as an amendment, and in Semi-Automated it pauses for me to confirm."

"Every button has a CLI twin, so headless operation in CI works the same way."

---

## Step 10 — Load generator: headless Locust (1 min) [EXPLAIN]

"Crucible drives Locust headless: no web UI, no port. It is a subprocess that
sends requests at the scenario's users and spawn rate, then writes its CSV.
A web UI would compete with the load generator for Box B's single CPU.

The diagnosis uses server-side metrics, Actuator's `hikaricp.connections.pending`
and the server's p99, not just Locust's client view. They measure different
things."

---

## Step 11 — Results (1.5 min) [LIVE]

**Navigate to:** Results → ↺.

"This comes from the manifest file on Box B, not hardcoded data."

Before/After chart: "Two measured windows, identical load. The only difference
is the pool size."

Trials: "The agent predicted 140 ms. The prediction is tracked as calibration,
but the verdict uses only the measurement."

**Show on Box B:**
```bash
ls ~/crucible/results/
python3 -m json.tool ~/crucible/results/<run-id>.json | head -40
```

"Every result is a JSON file. It records the commit that was running, the model
that actually served the diagnosis, the environment, the collector version, each
approval, the caps and any mid-run edits. Reproducible by construction."

---

## Step 12 — Datadog + CI/CD (1 min) [EXPLAIN]

"Three metrics backends: Actuator direct from the JVM, Prometheus via PromQL,
and Datadog. In Datadog you'd see `hikaricp.connections.pending` spike during
the pool=2 baseline and flatten after the fix: the same numbers the agent used.

The deploy interface is the sandbox branch. Here a post-receive hook builds and
restarts. In Jenkins or GitHub Actions, the same push would trigger the pipeline."

---

## Step 13 — History + chat about runs (2.5 min) [LIVE]

**Navigate to:** History & Diff. The run table is live from Box B.

Live AI ON. Type, with a **real run id** from 0.6:
`Tell me about <advisory-run-id>: why did you rank #1 above #2?`

→ The answer names the run, the cause, the change (`2 -> 20`) and the evidence
(43 pending), and calls the predicted p99 a prediction.

Then: `Why was experiment 1 kept in <semi-auto-run-id>?`
→ It quotes the measured before → after and the verdict reason.

**Say:**
"The chat reads the run files on disk. Measured runs, Advisory runs and ceiling
probes. It picks the run I name, even with a typo, or the one on screen, and
says which. If I name a run that doesn't exist, it says so, rather than answering
about a different one. And it never presents a prediction as a result."

*(Optional: `which runs do I have?` lists them.)*

**Do not ask about TR-002/TR-003.** Those are the static demo rows. The chat
will correctly say they are not on disk. The **Diff** card is also static demo
content. Show it as "what a comparison looks like", or compare two real rows.

"Comparability is automatic. Two runs on different models or environments are
flagged, not silently compared."

**Ceiling probe:** "A ceiling result is a pair: the last level that met the SLA
and the first that didn't, both measured. It describes the service, so it is
never shown as a verdict on a change."

---

## Step 14 — Benchmarks + the LUCKY quadrant (2 min) [UI]

**Navigate to:** Benchmarks (static content from the real fixture runs).

"Diagnosis is replayed against captured snapshots: two seconds and a fraction of
a cent each, against fifteen minutes for a live campaign. So I can run hundreds
of diagnosis tests without one load test."

Scoring grid: "Six dimensions. The LUCKY quadrant matters most: wrong diagnosis,
fix worked anyway. Counting that as success fails the next look-alike fixture.
Scoring diagnosis separately from outcome is what catches it."

**On evaluation:**
"Every test assertion is drafted with its reasoning and reviewed by me before
it's committed, more than 40 groups in `docs/CRUCIBLE_TEST_ASSERTIONS.md`. The
review asks whether the reasoning matches the failure it tests for."

---

## Step 15 — Memory taxonomy + backend files (2 min) [EXPLAIN]

"Seven memory kinds, each with its own lifetime and write authority.

**Working**: current hypothesis, in-flight snapshot. Dies with the run.
**Episode**: experiment manifests and the journal.
**Fact**: confirmed operational knowledge.
**Playbook**: cause signatures that worked, service-scoped until a human promotes them.
**Policy**: the SLA. The agent can never write it.
**Audit**: every refusal and approval. Append-only.
**Document chunk**: the knowledge corpus, in its own store."

**Show on Box B:**
```bash
ls ~/.crucible/
# memory.sqlite        the agent's memory, plus each campaign's SLA Policy record and audit entries
# memory.sqlite.faiss  vectors for facts/episodes (Policy and Audit are never embedded)
# knowledge/           uploaded originals
# knowledge-index/     the document store (knowledge.sqlite + its own .faiss)
sqlite3 ~/.crucible/memory.sqlite \
  "SELECT created_at, kind, substr(text,1,80) FROM records WHERE kind IN ('policy','audit') ORDER BY created_at DESC LIMIT 5;"
```

"Choosing the memory kind is a security decision. An agent that can overwrite
its SLA is not safe to run autonomously. Here it is a structural constraint,
not a rule someone has to remember."

---

## Step 16 — CLI parity (1 min) [EXPLAIN]

"Every button calls the same function as its CLI command: same guard, same
approval binding. The UI is not a shortcut."

```bash
crucible plan
crucible run --all-scenarios --advisory
crucible run --cost-cap 0.05 --wall-clock-cap 2
crucible status <run_id>
crucible approve <run_id> --experiment 1 --as raghu
crucible resume <run_id> --cost-cap 1
crucible report --run <run_id>
crucible diff <run_id_a> <run_id_b>
crucible bench --replay
```

---

## Closing line (30 sec)

"The loop is complete: instrument, load, diagnose, propose, approve, deploy,
verify, record. Every result is a file on disk, and nothing is claimed that
wasn't measured."

---

## Total estimated recording time: ~29 minutes

| Section | Time |
|---|---|
| Intro + architecture | 2 min |
| Knowledge upload + index | 2 min |
| Grounded chat | 1.5 min |
| Input data + templates | 1 min |
| Goal & SLA | 1 min |
| Mode, scenarios, hooks, budget, review | 2.5 min |
| Guardrails | 2 min |
| Preflight | 1.5 min |
| Live campaign + approval (+ edit) | 3 min |
| Headless Locust | 1 min |
| Results + manifest | 1.5 min |
| Datadog + CI/CD | 1 min |
| History + chat about runs | 2.5 min |
| Benchmarks | 2 min |
| Memory taxonomy | 2 min |
| CLI parity | 1 min |
| Closing | 0.5 min |
| **Total** | **~29 min** |

---

## LIVE vs SCRIPTED — quick reference

| Screen / Feature | Status | Notes |
|---|---|---|
| Wizard values | **LIVE** | `GET /v1/perf/wizard` reads `slo.yaml` and the profile |
| Knowledge upload + index | **LIVE** | `POST /v1/perf/knowledge/documents` (token); background indexing |
| ✨ AI suggestions | **LIVE** | `POST /v1/perf/suggest` (token); disabled while a document is indexing |
| Input data / hooks upload | **LIVE** | `/v1/perf/load-data`, `/v1/perf/hooks` (token) |
| Budget table | **LIVE** | `POST /v1/perf/wizard/budget`, computed from mode + scenarios |
| Review diff + checks | **LIVE** | `/v1/perf/wizard/preview`, `/v1/perf/wizard/check` |
| Save changes | **LIVE** | `POST /v1/perf/wizard/save` (token); allowed mid-run |
| Launch | **LIVE** | `POST /v1/perf/wizard/launch` (token); refused while a campaign is running or paused |
| Chat, Live AI ON | **LIVE** | `/v1/perf/chat`: knowledge excerpts, facts from disk, run under discussion |
| Chat, Live AI OFF | **SCRIPTED** | Keyword-matched canned answers |
| Jobs tab | **LIVE** | `/v1/perf/jobs`; badge polls every 10 s |
| Preflight / Ceiling launch | **LIVE** | `/v1/perf/jobs/preflight`, `/v1/perf/jobs/ceiling` (token) |
| Approve / Reject | **LIVE** | `/v1/perf/runs/{id}/experiments/{n}/decision` |
| Pause / Resume / Abort | **LIVE** | `/v1/perf/campaign/{id}/pause|resume|abort` (token) |
| Active tab, live campaign | **LIVE** | Polls `/v1/perf/campaign/active` every 5 s |
| Active tab, no campaign | **DEMO** | TR-004 pre-loaded; approval animates locally |
| Results tab | **LIVE** | `/v1/perf/runs/{id}/report` + Chart.js |
| History table | **LIVE** | `/v1/perf/screens/17` |
| History Diff card, ceiling card, Playbooks | **SCRIPTED** | Static TR-002/TR-003/CP-001 content |
| Overview KPIs, Benchmarks, Guardrails | **SCRIPTED** | Static, matching real runs and `profile.yaml` |
| Locust | **N/A** | `--headless`; no web UI |
| Datadog dashboard | **LIVE** | app.datadoghq.com |

---

## Emergency fallbacks

| Problem | Fallback |
|---|---|
| GLC v5 cold start (>30 s) | Re-run 0.4; or toggle Live AI OFF for that question (scripted answers) |
| ✨ buttons greyed out | A document is still indexing: wait for "indexed — N chunks" |
| Upload chip says `failed:` | Usually the embed cooldown 503. Click Upload & index again; identical text isn't re-embedded |
| Chat answer has no Sources line | Retrieval found nothing: check `~/.crucible/knowledge-index/` is non-empty |
| Chat says a run is "not on disk" | The id is wrong or a static demo id (TR-…): use an id from `ls ~/crucible/results/` |
| Launch refused (409) | A campaign is running or paused: finish it, or `crucible abort <run>` |
| Launch refused: cap overrun | The cap can't fit the baselines: raise it, or confirm the overrun in the dialog |
| Run paused at a cap | `crucible resume <run> --cost-cap <higher>` or abort, then `crucible report --run <run>` |
| Box B unreachable | History shows "Could not load history": explain the API call; static tabs still work |
| Approval gate already dismissed | Refresh: the demo screen resets on load |
| Write action returns 401 | Token not saved: Settings → Control token → Save |
| Write action returns 503 | `CRUCIBLE_CONTROL_TOKEN` missing from Box B `.env`: add it, restart serve (0.1) |
| Active tab shows no live overlay | `curl 127.0.0.1:8113/v1/perf/campaign/active` on Box B; the state file may not exist yet |
| Results chart absent | Chart.js CDN blocked: reload; the KPI strip still shows numbers |
