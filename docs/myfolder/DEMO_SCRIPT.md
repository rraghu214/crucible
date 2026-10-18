# Crucible — End-to-End Demo Script

---

## How to use these three documents together

| Document | When to open | Purpose |
|---|---|---|
| **[crucible-product-doc.html](crucible-product-doc.html)** | **Share with trainer/audience the day before** | Pre-demo product brief — problem, architecture, five principles, NFR table, memory taxonomy, design concepts. Trainer reads it before the session; you don't need to explain all of it on screen. |
| **[DemoExecutionGuide.md](DemoExecutionGuide.md)** | 30 min before recording + keep open during | Backstage operator manual — Box B prep, GLC v5 warmup, reset commands, emergency troubleshooting. |
| **This file** | During recording | The script — exactly what to say, what to click, what to expect at each step. Follow it top to bottom. |

---

## Part 0 — Prep commands (run before recording)

> Run these on **Box B** over SSH. Each step is checked; don't skip.

### 0.1 Pull latest code and restart the UI

```bash
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" ubuntu@150.136.143.227

cd ~/crucible
git pull origin capstone/perf-agent
uv sync
sudo systemctl restart crucible-ui
systemctl status crucible-ui           # must show: active (running)
```

### 0.2 Verify Box A is reachable and pool=2

```bash
# Health check
curl -s http://10.0.0.79:8080/actuator/health \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# Expected: UP

# Confirm pool size is 2 (the broken state the demo fixes)
curl -s http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# Expected: 2.0  — if 20.0, see §0.3
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
# Wait ~90 s for post-receive hook, then re-verify §0.2
```

### 0.4 Warm up GLC v5 gateway

```bash
# Burns off the Render cold start (up to 60 s on first call)
curl -s -X POST https://glc-v5-rraghu214.onrender.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"ping"}]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'][:60])"
# If this takes >30 s, wait and run it again — subsequent calls are fast
```

### 0.5 Pre-index a knowledge document for the RAG demo

```bash
# Upload and index the Spring Boot architecture doc so Step 3 chat reflects its content
# Option A — via CLI (add to a running crucible serve session):
curl -s -X POST http://127.0.0.1:8113/documents \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CRUCIBLE_CONTROL_TOKEN" \
  -d "{\"text\":\"$(cat docs/perf-lab-architecture.md | python3 -c \"import sys,json; print(json.dumps(sys.stdin.read()))\" | tr -d '\"')\",\"source_uri\":\"file://docs/perf-lab-architecture.md\",\"scope\":\"project\",\"source_author\":\"raghu\"}" \
  | python3 -m json.tool | grep -E "chunks|source_uri"
# Expected: "chunks": N  (number of semantic chunks indexed)

# Option B — if the above is tricky, upload via the New Test Run wizard in the UI
# Step 2 of this script shows this exact UI path
```

### 0.6 Confirm RAG index exists on Box B

```bash
ls -la ~/.crucible/memory/
# Should show: memory.db  memory.faiss  memory.faiss.ids.json
# memory.faiss is the FAISS vector index (gemini-embedding-001 at 768d via GLC v5)
```

### 0.7 Confirm SPA endpoints

```bash
curl -s http://127.0.0.1:8113/v1/perf/jobs | python3 -m json.tool
curl -s http://127.0.0.1:8113/v1/perf/campaign/active | python3 -m json.tool
# Expected: {"active": false, ...}
curl -s http://127.0.0.1:8113/v1/perf/runs/latest/report \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('run_id','(no run yet)'), d.get('outcome',''))"
```

### 0.8 Open tunnel and browser

```powershell
# Windows laptop — keep this terminal open throughout
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" `
    -L 9113:127.0.0.1:8113 `
    ubuntu@150.136.143.227
# 9113 = SPA demo UI
# Note: Crucible runs Locust with --headless; no Locust web UI on port 8089.
```

Open **http://localhost:9113/dashboard**, then:
1. **Settings → Control token** → paste `CRUCIBLE_CONTROL_TOKEN` → Save
2. Navigate to **Results** → confirm chart and real KPIs load
3. Navigate to **Active** → confirm TR-004 demo view (no live banner)

---

## Pre-flight checklist (confirm before pressing Record)

```
Pre-read : crucible-product-doc.html sent to trainer
Box B    : crucible-ui.service active (running) on port 8113
Tunnel   : 9113 (SPA) + 8089 (Locust) forwarded
Box A    : pool_size = 2.0 confirmed
GLC v5   : warmed (§0.4 — first call fast)
RAG index: memory.faiss present on Box B (§0.6)
Token    : saved in Settings
Results  : real KPIs + chart visible
Active   : TR-004 demo view (no live overlay)
```

---

## The story in one sentence

*"I deployed a Spring Boot service, pointed Crucible at it, and it found, fixed, and proved a connection-pool bottleneck — autonomously, with a full audit trail."*

---

## Step 1 — Problem statement + architecture (2 min) [EXPLAIN]

**Say:**  
"Performance engineering today is manual — load test, open Grafana, form a guess,
apply a change, re-test. That loop takes hours, requires three tooling contexts,
and falls apart when the person who knows the tool isn't the person who owns
the service.

Crucible closes that loop. It runs its own experiments, diagnoses from the
telemetry it can actually reach, proposes a bounded change, waits for your
approval, applies it, and measures again. Every claim is a measurement —
nothing is asserted."

**Show:** `localhost:9113/dashboard` — point at four KPIs.

**Say:**
"Three boxes. Box A is the Spring Boot service on Oracle Cloud — PerfLab, running
Postgres, Redis, Prometheus, and Jaeger alongside the app. Box B — this machine —
runs Crucible and the Locust load generator. GLC v5, hosted on Render, is the
LLM gateway. Crucible never holds model keys; the gateway owns key rotation.
The architecture brief I shared before this session covers this in detail."

---

## Step 2 — Knowledge RAG: upload + chunking (2 min) [LIVE]

**Navigate to:** New Test Run → Step 1 Target & Repo.

Point at the doc drop zone.

**Say:**
"Before a campaign starts, I can upload any architecture document, runbook, or
post-mortem here. Crucible chunks it semantically — at topic boundaries, not
fixed character counts. Each chunk is embedded using Gemini embedding-001 at
768 dimensions, called directly via the Gemini API — same keys already in
your `.env`, not via GLC v5. Those vectors are stored in a local FAISS index
on Box B.

When the agent diagnoses a failing run, it queries this index first. That means
it won't blame the application for an API-gateway throttle that a runbook
already described — it surfaces the relevant context from your documentation
into the diagnosis prompt."

**Action:** Drag the Spring Boot architecture doc onto the drop zone. Its name
appears as a chip. In the background, `POST /documents` fires against the API.

**Show on Box B (second terminal):**
```bash
ls -la ~/.crucible/memory/
# memory.faiss grows as chunks are indexed
```

**Say:**
"The index file on disk — FAISS with inner-product similarity, L2-normalised.
The embedder is pinned per campaign, same as the LLM model is pinned. Two
different embedding models at 768 dimensions produce incomparable vector spaces;
changing the embedder requires re-indexing the whole corpus, never querying
across two. The same principle that says a changed collector invalidates old
snapshots — it applies to memory too."

---

## Step 3 — Onboarding chat — curated questions (1.5 min) [SCRIPTED / LIVE]

The chat pane is open on the right.

**Say:**
"The chat is screen-aware — the system prompt changes depending on which tab
is open. With the knowledge doc indexed, questions about this service get
answers grounded in what I just uploaded, not generic responses."

**Type in chat (Scripted mode — instant):**  
`What SLA should I set for /api/db at 50 users?`

→ Scripted answer appears.

**Toggle Live AI ON.** Type:  
`What cause families does this Spring Boot runtime declare, and which one is most relevant at 50 concurrent users with a small pool?`

→ Real GLC v5 call. The system prompt carries the full memory taxonomy, the
uploaded doc context, and the screen hint. Wait for the response — it should
reference pool exhaustion as the most likely candidate given 50 users.

**Toggle back to Scripted** for the rest of the demo (avoids cold-start delays).

---

## Step 4 — users.csv + POST body template (1 min) [EXPLAIN]

Still on wizard Step 1.

**Say:**
"The load generator needs a user pool for realistic traffic. This CSV is not
chunked into the RAG — it's raw file data, row by row. Each column maps to a
variable in the Locust POST body template."

Point at the users.csv chip and the variable mappings (`{{username}}`, `{{password}}`).

**Say:**
"Authenticated user flows instead of synthetic anonymous load. The difference
matters when your auth middleware adds 40 ms per request — anonymous load
would mask it entirely."

---

## Step 5 — Goal & SLA + Scenarios (1.5 min) [UI]

Click **Next: Goal & SLA**.

Fill in: `Reduce /api/db p99 below 120 ms at 50 users`  
Click **✨ Let AI suggest SLA** — fills p99 = 120 ms, error budget = 1%, noise = 2.08%.

**Say:**
"The noise floor — 2.08% — was measured on this specific box by running the
same load three times with nothing changed. On a Windows laptop with co-located
Locust it was 14.3%. A change that moves p99 less than 2.08% is reported as
inconclusive and reverted, because it can't be distinguished from normal
run-to-run variation."

Click **Next: Scenarios**.

**Say:** "Two scenarios: steady_50u at normal load, burst_100u at 2× spike."

---

## Step 6 — Mode selection + Budget + YAML review (2 min) [LIVE]

Click **Next: Budget**.

Click each mode card:

- **Advisory** — "AI diagnoses and suggests. You apply manually. No deploy hook needed."  
  → Budget: ~2.5 h · ~3 LLM calls · ~$0.006

- **Semi-Automated** — "You approve each diff. AI deploys and measures. The approval
  is bound to the exact diff — you can't approve one thing and deploy another."  
  → Budget: ~3.5 h · ~8 calls · ~$0.016

- **Fully Automated** — "The mode that most directly reflects an idea Andrej Karpathy
  described for AI-driven research pipelines — an AI scientist that can propose
  experiments, run them unattended, and iterate on evidence. Crucible applies this
  to performance engineering: every experiment is bounded by `profile.yaml`, every
  result is a manifest file, and the watchdog runs arithmetic health checks every
  5 minutes. There are no approval prompts — but the agent's authority is bounded
  by the property list and ranges you set in advance. The guardrails are what make
  this safe, not trust in the model."  
  → Budget: ~5 h · ~14 calls · ~$0.028

- **Ceiling Probe** — "A different question: not 'what is broken?' but 'how much
  headroom do I have?' Load escalates in steps until the SLA is first missed.
  Only an operator can start this — the agent can never initiate it because it
  changes the load profile, which the agent is explicitly forbidden from touching."  
  → Budget: ~1.5 h · ~2 calls · ~$0.004

**Say:**  
"Wall clock is the real budget. Adding a third scenario costs cents but adds
40 minutes — that's what matters. Budget UI foregrounds time, not dollars."

Set back to **Semi-Automated** → click **Next: Review**.

**Show Review step.** Point at the full YAML summary.

**Say:**
"Every YAML value participating in this run is shown here before anything starts:
the SLA target, the noise threshold, the load profile, the allowed properties with
their current bounds, the sandbox branch, the deploy mode. This is what `crucible plan`
also prints on the command line — same data, same source."

---

## Step 7 — Guardrails + sandbox structure (2 min) [UI]

**Navigate to:** Guardrails tab.

**Say:**
"Two lists. The green 'Always enforced' rules cannot be toggled."

Point at Policy memory row:
"The SLA lives in Policy memory — the agent has no write permission for it, ever.
Two independent enforcements: a protected path in the file guard, and a Policy
memory kind. A single enforcement point can be bypassed if configuration moves;
two independent mechanisms both have to fail."

Point at sandbox branch row:
"Crucible writes to exactly one branch — `perftest_sandbox` on the PerfLab repo.
The workspace lives at `~/perf-lab` on Box B. Each experiment is one commit:
`git add` for one file, `git commit` for one file, `git push` to one branch.
The post-receive hook on Box A picks it up and rebuilds."

Point at allowed properties:
"SKILL.md tells the model how this runtime fails and what signals mean. It goes
into the model prompt and nowhere else — it never grants permission to change
anything. `profile.yaml` is what grants authority. They are deliberately separate
files, so a SKILL.md that says 'you may raise pool size to 10,000' has zero effect."

---

## Step 8 — Preflight (1.5 min) [EXPLAIN + CLI]

**Navigate to:** Preflight tab.

**Say:**
"Preflight exercises the full loop once, cheaply, before committing to an
8-trial campaign. It connects to Box A, deploys a no-op change, confirms the
property is live by reading `/actuator/env`, reverts, and confirms the revert
is live. These exact four steps — deploy, read-back, revert, read-back — run
before every experiment in a real campaign. Preflight runs them once so if
something is broken, it fails in 5 minutes, not 40."

```bash
crucible preflight --apply-probe
```

"If the SSH key is wrong, the post-receive hook is missing, or the config file
is in the wrong location — it fails here, not mid-campaign."

---

## Step 9 — Active campaign: PDMA loop + live overlay (3 min) [LIVE when running / DEMO when idle]

**Navigate to:** Active tab.

**Two modes:**
- **No live campaign:** TR-004 demo screen (pool=2, 43 pending threads).
- **Live campaign:** green ● LIVE overlay appears within 5 s, polling every 5 s.

> **To launch a real campaign** (start during a natural pause — it runs
> in the background, first results in ~7 minutes):
> ```bash
> # Box B — second SSH terminal
> cd ~/crucible && source .env
> uv run crucible run --run-id demo-live-1 --workspace ~/perf-lab \
>     --provider gemini --model gemini-2.5-flash --experiments 3
> ```
> Or click **New Test Run → Launch** in the SPA (token must be saved).

**What to say (demo mode):**

"The metric band shows live readings: p99 is 312 ms — above 120 ms SLA. Pool
active is 2 out of 2 — fully saturated. Pending: 43 threads waiting."

Point at the PDMA graph:
"The campaign follows a Perception → Memory → Decision → Action loop. Each
completed node is what just ran. The yellow node is where we're paused — the
approval gate. Perception already fired: the collector read HikariCP metrics
from `/actuator/metrics`, converted Micrometer seconds to milliseconds before
the model saw them, and sampled the `pending` gauge mid-run so we captured
peak value, not the post-run zero. Memory fired: the Journal RAG was queried —
'has this hypothesis been tried and disproven on this service?' — filtering on
structured fields before using vectors."

Point at the approval gate:
"The proposal: maximum-pool-size 2 → 20. The evidence: 43 waiters, 2406 ms max
acquire time, pool always at 100% utilisation. GC overhead 4% — healthy. CPU
28% — healthy. Only the pool is saturated.

The exact diff is what you approve. After approval, Crucible commits that one
file, pushes to `perftest_sandbox`, waits for `/actuator/env` to confirm the
property is live — not '/actuator/health' which proves the process started, but
the actual property value — then runs a fresh 300-second measurement window."

**Click Approve.** → gate animates to "Change applied — measuring now."

**Point at CLI twins:** "Every button has a CLI equivalent. Headless operation
in CI works identically."

---

## Step 10 — Load generator: headless Locust (1 min) [EXPLAIN]

**Say:**
"Crucible drives Locust headless — no browser UI, no web port open. The load
generator is a subprocess: Crucible spawns it with `--headless`, waits for the
measurement window, then reads the CSV it writes on exit.

This is intentional. A web UI means Locust holds an extra thread and HTTP
server, which contends with the load generator on a single OCPU. Crucible
owns the measurement; Locust is just a subprocess that sends requests and
writes numbers to disk.

The metrics you care about come from the server side — Actuator's
`hikaricp.connections.pending`, `http.server.requests` p99 — not from Locust's
client-side latency. Client and server measure different things: Locust sees
what left the load generator, Actuator sees what the JVM processed."

---

## Step 11 — Results: outcome + live report + chart (1.5 min) [LIVE]

**Navigate to:** Results tab (auto-loads from API on open).

**Say:**
"The Results tab fetches the latest run from the API — what you see is from the
actual manifest file on Box B, not hardcoded data."

Point at the Before/After bar chart:
"Before: p99 1206 ms at pool=2. After: 58 ms at pool=20. A 95.2% improvement.
The chart shows three percentiles: p50, p95, p99. This is not a prediction —
these are two measured windows, identical load, the only difference is the pool
size between them."

Point at trial breakdown:
"Pool active dropped from 2/2 to 4/20. Pending threads dropped from 43 to 0.
The agent's predicted p99 was 140 ms — it got 58. The prediction is tracked as
a calibration signal, but the verdict uses the measurement, not the prediction."

**Show on Box B (second terminal):**
```bash
ls ~/.crucible/results/
cat ~/.crucible/results/demo-live-1.json | python3 -m json.tool | head -40
```

**Say:**
"Every result is a JSON manifest on disk. Not a database row — a file. It
records the commit SHA that was running when measured, the model that ran
the diagnosis, the environment, the collector version, every approval with
a timestamp, and the exact evidence the agent cited. Reproducible by construction."

---

## Step 12 — Datadog metrics during/after the run (1 min) [EXPLAIN]

**Say (verbal — switch to Datadog tab in browser if available):**
"Crucible integrates three metrics backends: Actuator (direct from the JVM,
one instance), Prometheus/PromQL (service-wide histograms via PromQL), and
Datadog (via the Datadog adapter). For this demo Box A pushes metrics to
Datadog via `micrometer-registry-datadog`.

In the Datadog dashboard you'd see `hikaricp.connections.pending` spiking to
43 during the pool=2 baseline, then flat zero after the fix — the same numbers
the agent used for its diagnosis, pulled via the Datadog API. The agent and
the dashboard are reading the same source.

Crucible also integrates with the CI/CD pipeline: the deploy step pushes to
`perftest_sandbox`, the post-receive hook on Box A builds and restarts. In a
Jenkins or GitHub Actions setup that same push would trigger the pipeline — the
sandbox branch is the interface."

---

## Step 13 — History & Diff: live data + comparison (2 min) [LIVE]

**Navigate to:** History & Diff tab (live API fetch).

Point at run rows and outcome pills.

**Toggle Live AI ON.** Type:  
`What does the diff between TR-002 and TR-003 reveal, based on what you see on this screen?`

→ Wait for GLC v5 reply. The system prompt tells the model which screen is open.
The response should reference the specific run IDs visible in the table.

**Say:**
"The system prompt carries the screen context — which tab is open and the
campaign data loaded in it. That's what makes the answer specific rather than
generic. When I was on a different screen and asked the same question without
that context, the response was about connection pools in general rather than
about the specific runs shown. Context precision matters."

**Toggle Live AI OFF.** Show Compare: select two rows, click Diff.

**Say:**
"The comparability check is automatic. If two runs used different models or
different environments, the comparison is flagged rather than silently presented
as equivalent. Same model, same box, same scenario — only then does a number-
to-number comparison mean anything."

**Mention ceiling probe:**
"Ceiling probe is a fourth campaign type — not on the history table because it
never changes configuration. It escalates load in steps until the SLA is first
missed. The result is a pair: last passing level and first failing level, with
both measurements. At pool=20, Box A has so much headroom that a ceiling probe
finds the limit is the load generator itself, not the service."

---

## Step 14 — Benchmarks: harness depth + LUCKY quadrant (2 min) [UI]

**Navigate to:** Benchmarks tab.

"94.6% accuracy across 20 fixtures and 9 cause families. The replay split is
what makes this affordable: diagnosis against a captured snapshot takes 2 seconds
and $0.002. A live campaign takes 15 minutes. I can iterate hundreds of diagnosis
tests overnight against captured fixtures without running a single load test."

Expand **connection_pool_exhaustion** row:  
"Three fixtures at different severity levels, each captured against three providers
— Actuator, PromQL, Datadog — so the same diagnosis question gets asked of three
telemetry backends."

Point at scoring grid:
"Six dimensions: outcome, diagnosis quality, integrity, comparability, efficiency,
calibration. The LUCKY quadrant is the important one — wrong diagnosis, fix worked
anyway by coincidence. An agent that counts this as a success will fail on the
next fixture with a similar-looking signal but a different root cause. Scoring
diagnosis separately from outcome is what surfaces this."

**Say on evaluation:**
"34 draft test assertions across 8 groups in `docs/CRUCIBLE_TEST_ASSERTIONS.md`:
measurement integrity, collector correctness, guard and safety, approval binding,
deploy and verification, diagnosis quality, scorer, watchdog. Every assertion is
reviewed before it's committed — the reasoning is checked against the actual
failure mode it tests for."

---

## Step 15 — Memory taxonomy + backend files (2 min) [EXPLAIN]

**Say:**
"Seven memory kinds, each with a different lifetime and write authority.

**Working** — current hypothesis and in-flight snapshot. Dies with the run.  
**Episode** — experiment manifests and the journal. Append-only, survives crashes.  
**Fact** — confirmed operational knowledge: 'this app resets by truncate+reseed.'  
**Playbook** — cause signatures that worked, service-scoped until a human
promotes them globally. One app's quirk never becomes everyone's false positive.  
**Policy** — the SLA lives here. Agent has no write permission, ever.  
**Audit** — every guard refusal, every approval. Append-only, tamper-evident.  
**Document chunk** — the Knowledge RAG corpus, embedded at 768d."

**Show on Box B:**
```bash
# The SQLite memory store (all seven kinds except document chunks)
ls ~/.crucible/memory/
# memory.db — SQLite for Working/Episode/Fact/Playbook/Policy/Audit
# memory.faiss — FAISS index for Document chunk vectors (gemini-embedding-001, 768d)
# memory.faiss.ids.json — FAISS id → memory record mapping

# Read the audit log — every guard refusal and approval on record
sqlite3 ~/.crucible/memory/memory.db \
  "SELECT created_at, text FROM records WHERE kind='Audit' ORDER BY created_at DESC LIMIT 5;"
```

**Say:**
"Policy memory is where the choice of memory kind becomes a security decision.
An agent that can overwrite its own SLA is not safe to run autonomously. The
memory kind is the mechanism — not a policy rule, a structural constraint."

---

## Step 16 — CLI parity + headless mode (1 min) [EXPLAIN]

Point at any CLI twin label in the UI.

"Every button calls the same function as the CLI command — same guard, same
approval binding, same deploy logic. The UI is not a shortcut. An engineer
whose CI/CD pipeline has no browser runs this entirely headless:"

```bash
crucible run --scenario steady_50u --sla config/slo.yaml &
crucible status <run_id>
crucible approve <run_id> 1 --as raghu
crucible score <run_id>
crucible diff <run_id_a> <run_id_b>
crucible bench --replay
```

---

## Closing line (30 sec)

"The loop is complete: instrument, load, diagnose, propose, approve, deploy,
verify, record. Every step is measurable and every result is a file on disk.
Nothing is claimed that wasn't measured."

---

## Total estimated recording time: ~28–30 minutes

| Section | Time |
|---|---|
| Intro + architecture | 2 min |
| Knowledge RAG + embedder | 2 min |
| Onboarding chat | 1.5 min |
| users.csv | 1 min |
| Goal + Scenarios | 1.5 min |
| Mode + Budget + YAML review | 2 min |
| Guardrails + sandbox structure | 2 min |
| Preflight | 1.5 min |
| Active campaign + PDMA + Approval | 3 min |
| Locust web UI | 1 min |
| Results + backend files | 1.5 min |
| Datadog + CI/CD | 1 min |
| History & Diff | 2 min |
| Benchmarks + evals | 2 min |
| Memory taxonomy + backend files | 2 min |
| CLI parity | 1 min |
| Closing | 0.5 min |
| **Total** | **~28 min** |

---

## LIVE vs SCRIPTED — quick reference

| Screen / Feature | Status | Notes |
|---|---|---|
| History & Diff table | **LIVE** | Fetches `/v1/perf/screens/17` from Box B |
| Chat with Live AI ON | **LIVE** | POSTs to `/v1/perf/chat` → GLC v5 with screen context |
| Budget table by mode | **LIVE** | JS calculates from mode selection |
| Jobs tab | **LIVE** | Fetches `/v1/perf/jobs`; badge polls every 10 s |
| Launch Test Run | **LIVE** | POSTs `/v1/perf/jobs/run` — token required |
| Preflight "Run checks" | **LIVE** | POSTs `/v1/perf/jobs/preflight` — token required |
| Ceiling Probe launch | **LIVE** | POSTs `/v1/perf/jobs/ceiling` — token required |
| Approve / Reject | **LIVE** | POSTs `/v1/perf/runs/{id}/experiments/{n}/decision` |
| Pause | **LIVE** | POSTs `/v1/perf/campaign/{id}/pause` — token required |
| Abort | **LIVE** | POSTs `/v1/perf/campaign/{id}/abort` — token required |
| Active tab — live campaign | **LIVE** | Polls `GET /v1/perf/campaign/active` every 5 s; real run-id, status, elapsed, approval gate, job log |
| Active tab — no campaign | **DEMO** | TR-004 pre-loaded; approval animates locally |
| Results tab | **LIVE** | Fetches `GET /v1/perf/runs/latest/report`; Chart.js Before/After chart + real experiment table |
| Locust | **N/A** | Crucible runs Locust `--headless`; no web UI, no port 8089 |
| Datadog dashboard | **LIVE** | app.datadoghq.com — real metrics from Box A during/after run |
| Chat with Live AI OFF | **SCRIPTED** | Keyword-matched, instant, no network |
| Active demo charts | **SCRIPTED** | Pre-loaded TR-004 p99/RPS data |
| Benchmarks tab | **SCRIPTED** | Accurate data from real fixtures; no backend call |
| Guardrails tab | **SCRIPTED** | Static content from profile.yaml values |
| Doc upload chip | **SCRIPTED** | Chip added to UI; indexing via `POST /documents` |
| Overview KPIs | **SCRIPTED** | Hardcoded to match real run data |

---

## Emergency fallbacks

| Problem | Fallback |
|---|---|
| GLC v5 cold start (>30 s delay) | Toggle Live AI OFF; scripted answers are accurate |
| Box B unreachable | History tab shows "Could not load history" — explain the API call; all other tabs are self-contained |
| Approval gate already dismissed | Refresh the page — demo screen resets on load |
| Write action returns 401 | Token not saved — Settings → Control token → paste → Save |
| Write action returns 503 | `CRUCIBLE_CONTROL_TOKEN` missing from Box B `.env` — add it and `sudo systemctl restart crucible-ui` |
| Active tab not showing live overlay | Check `curl 127.0.0.1:8113/v1/perf/campaign/active` on Box B — campaign state file may not be created yet |
| Results tab showing TR-003 demo data | No completed run yet — check `curl .../v1/perf/runs`; run a campaign first |
| Results chart absent | Chart.js CDN blocked — reload; KPI strip still shows numbers |
| RAG doc not indexed (empty memory.faiss) | Run `§0.5` curl command on Box B before continuing |
| Gemini embedding fails (GLC v5 Render) | Check that `GEMINI_API_KEY` is set in Render env vars; embedder falls back gracefully |
