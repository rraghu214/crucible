# Crucible — End-to-End Demo Script

> **How to use this file**  
> Follow each numbered step in order. Steps marked **[LIVE]** make real network
> calls (REST API on Box B). Steps marked **[UI]** are polished screens — the
> data is accurate and most buttons now call real endpoints too (see the
> LIVE/SCRIPTED table at the bottom). Everything is noted so you know exactly
> what to expect.
>
> **October 2026:** All write actions (Launch, Preflight, Abort, Pause,
> Approve/Reject, Ceiling Probe, Bench) call the live REST API. The **Active**
> tab polls live campaign state every 5 seconds; the **Results** tab loads the
> latest run report on open. Enter `CRUCIBLE_CONTROL_TOKEN` in Settings before
> any write action.

---

## Part 0 — Prep commands (run once before recording)

> Run these **on Box B** over SSH, in order. They set the stage so the demo
> campaign starts from a clean, known state.

### 0.1 Pull latest code and restart the UI

```bash
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" ubuntu@150.136.143.227

# Once on Box B:
cd ~/crucible
git pull origin capstone/perf-agent
uv sync
sudo systemctl restart crucible-ui
systemctl status crucible-ui       # must show: active (running)
```

### 0.2 Verify Box A is reachable and pool=2

```bash
# Health check
curl -s http://10.0.0.79:8080/actuator/health | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# Expected: UP

# Confirm pool size is 2 (the "broken" state we're going to fix)
curl -s http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# Expected: 2.0
# If it shows 20.0, reset application.properties first (see §0.3)
```

### 0.3 Reset Box A to pool=2 (only if needed)

```bash
# On Box B — reset to the "broken" baseline commit
cd ~/perf-lab
git checkout perftest_sandbox
git log --oneline -5     # find the pool=2 commit
# Edit spring-boot application.properties -> maximum-pool-size=2
git add src/main/resources/application.properties
git commit -m "reset: pool=2 for demo baseline"
git push origin perftest_sandbox
# Box A post-receive hook rebuilds; wait ~90 s then verify pool=2 above
```

### 0.4 Warm up GLC v5

```bash
# Send one LLM call to burn off the Render cold start (up to 60 s)
curl -s -X POST https://glc-v5-rraghu214.onrender.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"ping"}]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'][:40])"
```

### 0.5 Confirm SPA endpoints are up

```bash
# Serve check
curl -s http://127.0.0.1:8113/v1/perf/screens | python3 -m json.tool | head -5
# Jobs endpoint
curl -s http://127.0.0.1:8113/v1/perf/jobs | python3 -m json.tool
# Active campaign endpoint (should return {active: false})
curl -s http://127.0.0.1:8113/v1/perf/campaign/active | python3 -m json.tool
# Latest run report (should return the last completed run)
curl -s http://127.0.0.1:8113/v1/perf/runs/latest/report | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('run_id','(no run yet)'), d.get('outcome',''))"
```

### 0.6 Open tunnel and browser

```powershell
# On your Windows laptop — keep this terminal open throughout the recording
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" `
    -L 9113:127.0.0.1:8113 ubuntu@150.136.143.227
```

Open **http://localhost:9113/dashboard** and:

1. Go to **Settings → Control token** → paste `CRUCIBLE_CONTROL_TOKEN` → Save.
2. Confirm token status shows "Token saved".
3. Go to **Results** tab — it auto-loads the latest run; confirm KPIs and chart appear.
4. Go to **Active** tab — it should show the TR-004 demo view (no live campaign yet).

---

## Pre-flight checklist (confirm before pressing Record)

```
Box B  : crucible-ui.service active (running) on port 8113
Tunnel : ssh -i ... -L 9113:127.0.0.1:8113 ubuntu@150.136.143.227
Browser: localhost:9113/dashboard
GLC v5 : warmed (§0.4 complete)
Box A  : pool_size = 2  (§0.2 confirmed)
Token  : Settings → Control token → saved  (§0.6 complete)
Results: tab shows real run data + Before/After chart
Active : tab shows TR-004 demo view (no live campaign)
```

---

## The story in one sentence

*"I deployed a Spring Boot service, pointed Crucible at it, and it found, fixed, and proved a connection-pool bottleneck — autonomously, with a full audit trail."*

---

## Step 1 — Problem statement (2 min) [EXPLAIN]

**Say:**  
"Performance engineering today is manual — load test, open Grafana, form a guess,
apply a change, re-test. That loop takes hours, requires three tooling contexts,
and falls apart when the person who knows the tool isn't the person who owns
the service.

Crucible closes that loop. It runs its own experiments, diagnoses from the
telemetry it can actually reach, proposes a bounded change, waits for your
approval, applies it, and measures again. Every claim is a measurement —
nothing is asserted."

**Show (in browser):**
Open `localhost:9113/dashboard` — Overview tab is visible by default.
Point at the four KPIs: 12 runs · best p99 58 ms · 95% improvement · 94.6% AI accuracy.

**Say:**
"Three boxes: Box A is the Spring Boot service on Oracle Cloud. Box B — this
machine — runs Crucible and Locust. GLC v5 hosted on Render is the LLM gateway.
Crucible never holds LLM keys; the gateway owns them."

---

## Step 2 — Architecture + Knowledge RAG (2 min) [UI + EXPLAIN]

**Navigate to:** New Test Run (click "+ New Test Run" or left nav)

**Step 1 of wizard — Target & Repo**

Point at the doc drop zone.

**Say:**  
"Any architecture document, runbook, or incident note can be uploaded here.
Crucible chunks it semantically — not by fixed character count — and embeds it
using Gemini embedding-001 at 768 dimensions into a FAISS index. When the agent
diagnoses a run, it queries this index first, so it won't blame the application
for an API gateway throttle that a runbook already described."

**Action:** Drag any PDF onto the drop zone — its name appears as a chip.

**Say:**
"The embedder is pinned per campaign — the same rule that pins the LLM model.
Two different models at 768 dimensions produce incomparable vectors, so changing
embedder means re-indexing, not querying across two."

---

## Step 3 — Onboarding chat (1.5 min) [SCRIPTED / LIVE toggle]

Chat pane is visible on the right side.

**Say:**  
"The chat is context-aware. It knows which screen you're on."

**Type in chat:**  
`What SLA should I set for /api/db at 50 users?`

→ Scripted answer appears (instant, no LLM needed for demo reliability).

**Now toggle Live AI ON** (small toggle in chat header).

**Type:**  
`What cause families does Spring Boot declare?`

→ Real GLC v5 call. The system prompt carries the full memory taxonomy, campaign
history, and a per-screen hint. Wait for the response.

**Toggle back to Scripted** before the campaign steps (avoids cold-start delays
during the core demo).

---

## Step 4 — users.csv + POST body template (1 min) [UI]

**Still on wizard Step 1.**

**Say:**  
"For realistic load, Locust reads a users.csv file — not chunked into RAG, just
raw rows. Each column maps to a variable in the POST body template."

Point at the users.csv row in the Request Payloads card. Show the variable
mappings (`{{username}}`, `{{password}}`).

**Say:**  
"This drives authenticated user flows rather than synthetic anonymous load — 
the difference matters when your auth middleware adds 40 ms per request."

---

## Step 5 — Goal & SLA + Scenarios (1.5 min) [UI]

Click **Next: Goal & SLA**.

Fill in: `Reduce /api/db p99 below 120 ms at 50 users`  
Click **✨ Let AI suggest SLA** — it fills p99 = 120 ms, error budget = 1%, noise = 2.08%.

Click **Next: Scenarios**.

**Say:**  
"Two scenarios by default: steady_50u is your normal load, burst_100u is a 2×
spike. The SLA applies to both."

---

## Step 6 — Mode selection + Budget (2 min) [LIVE]

Click **Next: Budget** — pause to explain modes first.

Click each mode card and narrate:

- **Advisory** — *"AI diagnoses and suggests. You apply everything manually.
  No GitHub connection needed. Best for first-time setup."*  
  → Budget table updates: ~2.5 h · ~3 LLM calls · ~$0.006
  
- **Semi-Automated** — *"You approve each proposed diff. AI deploys and
  measures. The approval is bound to the exact diff you were shown — you can't
  approve one thing and deploy another."*  
  → Budget table: ~3.5 h · ~8 calls · ~$0.016

- **Fully Automated** — *"AI acts within the property bounds in profile.yaml —
  no approval prompts between experiments. Inspired by the idea of an AI
  scientist that can run unattended experiments and iterate on evidence.
  The guardrail bounds are what make this safe, not trust in the model."*  
  → Budget table: ~5 h · ~14 calls · ~$0.028

- **Ceiling Probe** — *"A different kind of campaign entirely. No config changes.
  It escalates load in steps until the SLA breaks, and reports the last passing
  level and first failing level as a pair. Started only by an operator flag —
  the agent can never initiate it because it changes the load profile."*  
  → Budget table: ~1.5 h · ~2 calls · ~$0.004

**Say:**  
"Wall clock is the real budget. Adding a third scenario costs cents but adds 40
minutes. The budget cap prevents runaway spending."

Set mode back to **Semi-Automated**. Click **Next: Review**.

---

## Step 7 — Guardrails + YAML (2 min) [UI]

**Navigate to:** Guardrails (left nav).

**Say:**  
"Two lists. The green 'Always enforced' rules cannot be toggled — they exist
because removing them caused real failures in our feasibility spike. The amber
'property bounds' rows come from profile.yaml for this specific runtime."

Point at the Always-enforced block:
- *"The SLA lives in Policy memory. The agent has no write permission for it —
  ever. Enforced twice: as a protected path in the file guard, and as a Policy
  memory kind. A single enforcement point can be bypassed if config moves; two
  independent mechanisms both have to fail."*
- *"Force-push refused always. The sandbox branch is the only branch Crucible
  can push to."*

Point at the property bounds:
- *"SKILL.md tells the model how this runtime fails and what signals mean. It is
  rendered into the prompt and nowhere else — it never grants permission to
  change anything. Profile.yaml is what grants authority. They are deliberately
  separate files."*

---

## Step 8 — Preflight (1.5 min) [EXPLAIN + CLI]

**Navigate to:** Preflight (left nav).

**Say:**  
"Before any campaign, Crucible runs a preflight: it deploys a no-op change,
confirms the target reports it live by reading the property back from
/actuator/env, reverts, and confirms the revert is live too.

These exact steps — deploy, property read-back, revert, read-back — happen
before every experiment in a real campaign. Preflight exercises them cheaply,
once, before you've committed to an 8-trial run.

The CLI equivalent:"
```bash
crucible preflight --apply-probe
```

"If anything fails here — a broken SSH key, a missing post-receive hook,
a config file in the wrong location — it fails in 5 minutes, not 40."

---

## Step 9 — Active campaign: PDMA loop + Approval (3 min) [LIVE when running / DEMO when idle]

**Navigate to:** Active tab (left nav).

**What you see depends on whether a live campaign is in progress:**

- **No live campaign (demo mode):** The TR-004 demo screen appears — pre-loaded
  with pool=2 / 43 pending data. Charts and metrics are illustrative.
- **Live campaign running:** The TR-004 header hides and a green ● LIVE overlay
  appears at the top, polling `GET /v1/perf/campaign/active` every 5 seconds.
  It shows the real run-id, status, elapsed time, and — when the model has a
  proposal — the live approval gate with cause, params and evidence.

> **To launch a real campaign** for the demo (takes ~14 min total — start during
> a natural pause):
> ```bash
> # From Box B over SSH
> cd ~/crucible
> source .env
> uv run crucible run --run-id demo-live-1 --workspace ~/perf-lab \
>     --provider gemini --model gemini-2.5-flash --experiments 3 &
> ```
> Or click **New Test Run → Launch** in the SPA (requires token saved in Settings).
> Then navigate to Active tab — the live overlay appears within 5 seconds.

**What to say and point at (demo mode):**

"This is a campaign in progress. The metric band across the top shows the
live readings: p99 is 312 ms — well above the 120 ms SLA. Pool active is
2 out of 2 — fully saturated. Pending: 43 threads waiting for a connection."

Point at the chart area:  
"p99 and RPS charts update throughout the measured window."

Point at the Agent Task Flow (PDMA graph):  
"The agent's current task graph. Four nodes are done — Baseline snapshot,
Recall past attempts from Journal RAG, Diagnose, Guard check. The yellow
node is the approval gate — the campaign is waiting for a human decision."

Point at the Approval Gate card:

"The proposal: raise maximum-pool-size from 2 to 20.
The diagnosis: pool saturation — 43 waiters, 2406 ms max acquire.
GC and CPU are healthy — the pool is the only thing saturated.

The exact diff is shown. What you approve is what deploys.
After I approve, the campaign will push to the perftest_sandbox branch,
wait for the read-back from /actuator/env to confirm the property is live,
then run a fresh 300-second measurement window."

**Click Approve** → gate animates to "Change applied — measuring now".

> **If a live campaign is running:** clicking Approve calls
> `POST /v1/perf/runs/{run_id}/experiments/{n}/decision` for real.
> The live overlay shows the updated status within 5 seconds.

**Show the CLI twin labels:**  
"Every button has a CLI equivalent shown below it. Crucible can run entirely
headless — no UI required. CI/CD pipelines use `crucible approve` directly."

---

## Step 10 — Results: outcome + calibration (1.5 min) [LIVE]

**Navigate to:** Results tab.

> The Results tab loads automatically from `GET /v1/perf/runs/latest/report`
> on open. If a completed run exists on Box B, you see real data. If not, the
> TR-003 demo data is shown unchanged.

"The Results tab loaded the latest completed run — the data you see is from
the actual manifest file on Box B, not hardcoded.

The Before / After chart shows the measurement delta across all three
percentiles — p50, p95, p99. This isn't a prediction; these are the two
real measured windows, one before and one after the change.

The trial breakdown shows exactly what changed between experiments: pending
threads dropped from 43 to 0, pool utilisation dropped from 100% to 20%.

Every result is a manifest JSON file on disk — not a database row that can
be lost. The manifest records the commit SHA, the model that ran the diagnosis,
the environment, and the collector version."

**Point at the ↺ Latest run button:**  
"This refreshes from the API — so if a new campaign just finished, one click
shows its results here."

**Point at the ▼ Pick run button (if visible):**  
"Multiple runs are available. I can switch to any previous campaign without
leaving the tab."

---

## Step 11 — History & Diff: live data + ceiling probe (2 min) [LIVE]

**Navigate to:** History & Diff tab.

Wait for the table to load from the live API.

"The history table is live — it fetches from /v1/perf/screens/17 which reads
the actual campaign manifests on Box B."

Point at run rows and outcome pills.

**Ask in chat (Scripted mode):**  
`What does TR-002 vs TR-003 reveal?`

→ Response: *"baseline p99 went from 1206 ms → 58 ms — 95.2% improvement.
Only change: maximum-pool-size 2 → 20. Comparability score ✓ — same model,
same environment, same scenario."*

**Say:**  
"Select two rows and click Compare for a side-by-side diff. The comparability
check is automatic — if two runs used different models or different environments,
the comparison is flagged rather than silently shown as equivalent."

---

## Step 12 — Benchmarks: harness depth + LUCKY quadrant (2 min) [UI]

**Navigate to:** Benchmarks tab.

"94.6% accuracy. 20 fixtures across 9 cause families — connection pool,
GC pressure, thread saturation, slow queries, cache miss, downstream latency,
lock contention, payload serialisation, and healthy baseline."

Expand **connection_pool_exhaustion** row:  
"Three fixtures. The severe one was captured against three providers — Actuator,
PromQL, and Datadog — to verify the model gives the same diagnosis regardless
of which telemetry backend you're using."

"The replay split: diagnosis runs against captured snapshots in under 2 seconds.
No live service needed. I can run hundreds of diagnosis evaluations in the time
one live campaign takes.

But — outcome alone isn't enough. The LUCKY quadrant is the key insight:
a wrong diagnosis that leads to a correct fix by coincidence looks like a
success in outcome-only scoring. On the next fixture with a similar-looking
signal but a different root cause, the same agent fails. That's why diagnosis
quality is scored separately from outcome."

---

## Step 13 — Memory types (2 min) [EXPLAIN]

**Navigate to:** Overview tab or keep on Benchmarks — this is verbal.

"Seven memory kinds. Each has a different lifetime and write authority.

**Working** — current hypothesis and in-flight snapshot. Dies with the run.  
**Episode** — experiment manifests and the journal. Append-only.  
**Fact** — things the agent confirmed: 'this app resets by truncate+reseed.'  
**Playbook** — cause signatures that worked, service-scoped until a human
promotes them to global. One app's quirk never becomes everyone's false positive.  
**Policy** — the SLA lives here. Agent has no write permission, ever.  
**Audit** — every guard refusal, every approval. Append-only, tamper-evident.  
**Document chunk** — the Knowledge RAG corpus."

"The Journal RAG at diagnosis time: filter on structured fields first —
'has this pool saturation hypothesis been tried and disproven on this service?'
— then vector search for narrative only. Dense retrieval is weak on exact
identifiers like hikaricp.connections.pending."

---

## Step 14 — CLI parity + headless mode (1 min) [EXPLAIN]

**Point at any CLI twin label visible on screen.**

"Every UI button maps to a CLI command. The UI calls the same function, same
guard, same approval binding. Nothing is bypassed by clicking a button instead
of running a command.

Full headless operation:"
```bash
crucible run --scenario steady_50u --sla config/slo.yaml &
crucible status <run_id>
crucible approve <run_id> 1       # approve experiment 1
crucible score <run_id>
crucible diff <run_id_a> <run_id_b>
crucible bench --replay           # replay eval, no live service
```

---

## Step 15 — Live AI chat: screen-aware context (1 min) [LIVE]

Go back to **History & Diff** tab.

Toggle **Live AI ON** in the chat header.

**Type:**  
`What does CP-001 ceiling probe tell us about headroom?`

Wait for GLC v5 reply. 

**Say:**  
"The system prompt carries the campaign history, the memory taxonomy, and knows
we're on the History & Diff screen. The response comes from the actual LLM — 
GLC v5 routing to Gemini — not a hardcoded string."

Toggle back OFF.

---

## Closing line (30 sec)

"The loop is complete: instrument, load, diagnose, propose, approve, deploy,
verify, record. Every step is measurable and every result is a file on disk.
Nothing is claimed that wasn't measured."

---

## Total estimated recording time: ~22–25 minutes

| Section | Time |
|---|---|
| Problem + architecture | 2 min |
| Knowledge RAG | 2 min |
| Onboarding chat | 1.5 min |
| users.csv | 1 min |
| Goal + Scenarios | 1.5 min |
| Mode + Budget | 2 min |
| Guardrails + YAML | 2 min |
| Preflight | 1.5 min |
| Active campaign + Approval | 3 min |
| Results | 1.5 min |
| History & Diff | 2 min |
| Benchmarks | 2 min |
| Memory types | 2 min |
| CLI parity | 1 min |
| Live AI chat | 1 min |
| Closing | 0.5 min |
| **Total** | **~27 min** |

---

## What is LIVE vs SCRIPTED — quick reference

| Screen / Feature | Status | Notes |
|---|---|---|
| History & Diff table | **LIVE** | Fetches `/v1/perf/screens/17` from Box B |
| Chat with Live AI ON | **LIVE** | POSTs to `/v1/perf/chat` → GLC v5 |
| Budget table by mode | **LIVE** | JS calculates from mode selection |
| Jobs tab | **LIVE** | Fetches `/v1/perf/jobs`; polls badge every 10 s |
| Launch Test Run button | **LIVE** | POSTs to `/v1/perf/jobs/run` — requires token |
| Preflight "Run checks" | **LIVE** | POSTs to `/v1/perf/jobs/preflight` — requires token |
| Apply Probe button | **LIVE** | Same as above with `apply_probe:true` |
| Launch Ceiling Probe | **LIVE** | POSTs to `/v1/perf/jobs/ceiling` — requires token |
| Approve / Reject | **LIVE** | POSTs to `/v1/perf/runs/{id}/experiments/{n}/decision` |
| Pause button | **LIVE** | POSTs to `/v1/perf/campaign/{id}/pause` — requires token |
| Abort button | **LIVE** | POSTs to `/v1/perf/campaign/{id}/abort` — requires token |
| **Active tab — live campaign** | **LIVE** | Polls `GET /v1/perf/campaign/active` every 5 s; shows real run-id, status, elapsed, approval gate, job log |
| **Active tab — no campaign** | **DEMO** | TR-004 pre-loaded; demo approval still animates locally |
| **Results tab** | **LIVE** | Fetches `GET /v1/perf/runs/latest/report` on open; renders Chart.js Before/After bar chart and real experiment table |
| Chat with Live AI OFF | **SCRIPTED** | Keyword-matched, instant, no network |
| Active tab charts/data | **SCRIPTED** | Demo p99/RPS charts (pre-loaded TR-004 state) |
| Benchmarks tab | **SCRIPTED** | Data accurate (from real fixtures), no backend call |
| Guardrails tab | **SCRIPTED** | Static content from profile.yaml values |
| Doc upload chip | **SCRIPTED** | Adds chip to UI; actual RAG indexing via CLI |
| GitHub connection test | **SCRIPTED** | 1.5 s delay then hardcoded "connected" message |
| Overview KPIs | **SCRIPTED** | Hardcoded to match real run data |

---

## Emergency fallbacks

| Problem | Fallback |
|---|---|
| GLC v5 cold start (30s+ delay) | Toggle Live AI OFF — scripted answers are accurate |
| Box B unreachable | History tab will show "Could not load history" — explain the API call |
| History shows no rows | Run `crucible run` on Box B first to generate a manifest |
| Approval gate already dismissed | Refresh the page — the active screen resets on load |
| Write action returns 401 | Token not saved — go to Settings, paste `CRUCIBLE_CONTROL_TOKEN`, Save |
| Write action returns 503 | `CRUCIBLE_CONTROL_TOKEN` not set in Box B `.env` — restart crucible serve after adding it |
| Jobs tab shows no output | Job may still be starting — click Refresh or wait 5 s |
| Active tab shows TR-004 but a campaign is running | Campaign endpoint unreachable — check `curl 127.0.0.1:8113/v1/perf/campaign/active` on Box B |
| Results tab shows demo data (TR-003) | No completed run on Box B yet — run at least one campaign, or check `curl .../v1/perf/runs` |
| Results chart does not appear | Chart.js CDN blocked — reload; the KPI strip still shows the numbers |
