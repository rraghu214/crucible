# Crucible — End-to-End Demo Script

*Updated 3 November 2026. Previous update: 2 November with Groups 45–56 trial fixes
and three additions (benchmark glossary, 1993 vs 26, guardrail layers). Practise
once end to end (Part P) before recording.*

> **3 November additions**
>
> 1. **§0.5** — retire PERF_LAB_PRODUCT.md from the knowledge store (done on Box B, 2 Nov) + ⏸ pause marker.
> 2. **§0.7** — tunnel marked optional; Cloudflare + Basic Auth path added.
> 3. **§0.8** — why not to delete `perftest_sandbox` or `~/perf-lab`.
> 4. **Step 1** — PerfLab's ten endpoints, checked against `PerfController.java` (~1 min extra).
> 5. **Step 4** — `demo-assets/sizes.csv`; no built-in generators (GUID, random) exist, so say so if asked.
> 7. **Step 9** — Locust headless note moved here from the removed Step 10; ⏸ markers for the run.
> 8. **Step 10** (was 11) — the stores behind a run: knowledge-index, results/ manifests, memory.sqlite.
> 9. **Steps 11–15** renumbered (old 12–16). Timing table updated to ~33 min.
> 10. **Benchmarks page** — LUCKY quadrant and 6-dimension definitions now shown on the page (code change).
> 11. **Guardrails page** — Three-layers tab added to the page (code change).
> 12. **Results page** — title now shows only the run id; headline is in the Findings card below (code change).

---

## How to use these documents together

| Document | When to open | Purpose |
|---|---|---|
| **[crucible-product-doc.html](crucible-product-doc.html)** | **Share with trainer/audience the day before** | Pre-demo product brief: problem, architecture, five principles, NFR table, memory taxonomy, design concepts. |
| **[DemoExecutionGuide.md](DemoExecutionGuide.md)** | 30 min before recording, and keep it open | Backstage manual: Box A/B state, resets, troubleshooting. |
| **[COMPLETION_CHECKLIST.md](../COMPLETION_CHECKLIST.md) step 10** | The day before | The pre-recording checklist. Step 9 (the dry run) was done as the 31 Oct – 2 Nov trial. |
| **This file** | Practice run, then recording | Part 0 = prep, Part P = practice run, Steps 1–16 = what to say and click. |

---

## Part 0 — Prep commands (run before practice AND before recording)

> Run these on **Box B** over SSH. Use `nohup`, not tmux. Always source `.env` first.

### 0.1 Pull latest code (first time after 25 October: follow checklist 9g)

```bash
ssh -i "<path-to-ssh-key>" ubuntu@<box-b-ip>

hostname                      # instance-20260912-2245 (Box B)
cd ~/crucible
git status --short            # config/slo.yaml and the profile show as modified: your wizard edits. Keep them.
git pull -q --ff-only origin capstone/perf-agent && git log --oneline -1
# Only if the pull refuses because of those two files:
#   git stash push config/slo.yaml config/profiles/spring-boot.yaml && git pull -q --ff-only origin capstone/perf-agent && git stash pop
uv sync --group load

# Restart the UI server. The script restarts serve under nohup, keeps the
# Cloudflare tunnel (so the public URL doesn't change), and prints healthz + the URL.
# serve loads ~/crucible/.env itself; only hand-typed CLI commands
# (crucible run/resume/report) need: set -a; . ./.env; set +a
~/restart-serve.sh        # {"ok":true,...} then "public URL: https://…/dashboard"
```

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

On **Box B** (`hostname` = instance-20260912-2245), never on Box A. Deploys go to
the `perftest` remote (Box A's `~/perflab-deploy.git`, SSH deploy key), **not**
`origin` (GitHub over HTTPS, which asks for a password and deploys nothing).

```bash
cd ~/perf-lab && git checkout -q perftest_sandbox && git fetch -q perftest \
  && git merge -q --ff-only perftest/perftest_sandbox \
  && sed -i 's/^spring.datasource.hikari.maximum-pool-size=.*/spring.datasource.hikari.maximum-pool-size=2/' src/main/resources/application.properties \
  && git diff --stat \
  && git commit -qm "reset: pool=2 for demo baseline" -- src/main/resources/application.properties \
  && git push perftest perftest_sandbox
# Chained with &&, so it stops at the first failure. The hook's output ends with
# "health UP" and "deploy verified: /api/version reports <sha>"; the
# "connection refused" lines before that are it polling during the restart.
# Then re-check 0.2.
```

### 0.4 Warm up the GLC v5 gateway

```bash
curl -s -X POST https://<your-gateway>.onrender.com/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash-lite","messages":[{"role":"user","content":"ping"}]}' | head -c 200; echo
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

**Before recording: retire PERF_LAB_PRODUCT.md so the upload is a first upload on camera.**
Done on 2 November 2026 (backup in `~/backups/knowledge-20261102-191809/`). If the
practice run re-uploads it, repeat this on Box B, then restart serve (0.1) so the
dashboard forgets its upload list:
```bash
TS=$(date +%Y%m%d-%H%M%S); mkdir -p ~/backups/knowledge-$TS \
  && cp -a ~/.crucible/knowledge ~/.crucible/knowledge-index ~/backups/knowledge-$TS/ \
  && python3 - <<'EOF'
import sqlite3, os
c = sqlite3.connect(os.path.expanduser("~/.crucible/knowledge-index/knowledge.sqlite"))
with c:
    for (d,) in c.execute("select document_id from documents where source_uri='upload://PERF_LAB_PRODUCT.md' and state='active'").fetchall():
        c.execute("update documents set state='superseded' where document_id=? and state='active'", (d,))
        n = c.execute("update records set status='superseded' where status='current' and kind='document_chunk' "
                      "and json_extract(metadata_json, '$.document_id')=?", (d,)).rowcount
        print("retired", d, "chunks:", n)
EOF
mv ~/.crucible/knowledge/PERF_LAB_PRODUCT.md ~/backups/knowledge-$TS/ 2>/dev/null; ls ~/.crucible/knowledge/
```
This retires only that document, with the same statements a re-index uses. Run
memory is untouched: `memory.sqlite` and `results/` are separate stores. The vector
index (`knowledge.sqlite.faiss`) rebuilds itself the next time the store opens.

> **⏸ Pause recording** while the chunking/indexing runs after Upload & index in Step 2.
> It takes ~4.5 min (gateway embed cooldown, 16 chunks). Resume when the chip says
> "indexed — N chunks". Do not re-upload on camera.

### 0.6 Confirm the UI endpoints

```bash
# The dashboard login is on, so every call needs it (read from .env, never typed or printed):
AUTH="$(grep ^CRUCIBLE_UI_USER= ~/crucible/.env|cut -d= -f2-):$(grep ^CRUCIBLE_UI_PASSWORD= ~/crucible/.env|cut -d= -f2-)"
curl -s -u "$AUTH" http://127.0.0.1:8113/v1/perf/jobs | python3 -m json.tool | head
curl -s -u "$AUTH" http://127.0.0.1:8113/v1/perf/campaign/active | python3 -m json.tool
# Expected: {"active": false, "running_job": null, "paused": []}
curl -s -u "$AUTH" http://127.0.0.1:8113/v1/perf/runs | python3 -m json.tool | head -20
# The run ids on disk: note two of them for Step 13
ls ~/crucible/results/                  # manifests: <run>.json, ceiling probes <run>.ceiling.json
```

### 0.7 Open the tunnel and browser

**Option A — SSH tunnel (no auth required):**
```powershell
# Windows laptop. Keep this terminal open throughout.
ssh -i "<path-to-ssh-key>" `
    -L 9113:127.0.0.1:8113 `
    ubuntu@<box-b-ip>
# Locust runs --headless: no web UI, no port 8089 to forward.
```
Browse **http://localhost:9113/dashboard**.

**Option B — Cloudflare Quick Tunnel (if `CRUCIBLE_UI_USER` / `CRUCIBLE_UI_PASSWORD` are set in `.env`):**
Browse the public Cloudflare URL directly. The Basic Auth login grants the control
token automatically (`CRUCIBLE_UI_LOGIN_GRANTS_CONTROL=1`), so you need only one
credential. No SSH port forwarding needed. *(The tunnel and login come down after submission.)*

In either case, confirm after opening:
1. **Settings → Control token** → paste `CRUCIBLE_CONTROL_TOKEN` (for Option A; Option B auto-grants it)
2. **Results** → a real run loads (chart and experiment table)
3. **Active** → "No campaign is running"

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
| P9 | Chat (Live AI ON): *Tell me about `<advisory-run-id>`: what did you propose, why, and was anything measured?* | Names the run, quotes the cause, the change and baseline evidence (pending connections), says nothing was applied or measured, calls the predicted p99 a *prediction*; Sources names the run manifest |
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

### 0.8 Do not delete `perftest_sandbox` or `~/perf-lab` (checked 2 November 2026)

- **Crucible never clones.** It uses the existing checkout named by `CRUCIBLE_WORKSPACE`
  in `~/crucible/.env` (`/home/ubuntu/perf-lab`). Remove that folder and Launch fails.
  Cloning on first use is backlog (DEBT.md, "The workspace is box configuration").
- **Crucible pushes to `perftest` (Box A's deploy repo), never to GitHub.** Deleting
  `perftest_sandbox` on GitHub changes nothing the run touches, and the audience would
  not see Crucible recreate it there.
- **Deleting it on `perftest` would break the demo.** Crucible would recreate it from
  the workspace's *local* `main`. GitHub's `main` has pool=2 since perf-lab PR #5
  (3 November), but Box B's local `main` is 12 commits behind and still has pool=10,
  and it lacks `/api/slow`. The baseline would show no bottleneck to find.

To start clean, use the pool=2 reset in 0.3. Each run's experiment commits then
sit on top of that reset commit.

**If asked how the target gets onto Box B** (operator decision, 3 November: say it,
don't build it before the demo): "The intended flow is that Crucible clones the
service from its base branch into its own sandbox folder, commits there, pushes the
sandbox branch, and that push triggers the deploy: a hook here, Jenkins in a real
shop. Today the checkout is made once by hand; cloning it automatically is the
dashed box in the README diagram."

---

## Pre-flight checklist (confirm before pressing Record)

```
Pre-read : crucible-product-doc.html sent to trainer
Box B    : crucible serve running on 8113 (healthz ok)
Tunnel   : 9113 forwarded (Option A only; Option B uses Cloudflare URL — no tunnel needed)
Box A    : pool_size = 2.0 confirmed
GLC v5   : warmed (0.4)
Knowledge: PERF_LAB_PRODUCT.md listed as indexed; knowledge-index/ non-empty (0.5)
Token    : saved in Settings
Results  : real KPIs + chart visible
Active   : "No campaign is running"
Run ids  : two real run ids noted for Step 13 (0.6)
Practice : Part P passed
```

---

## The story in one sentence

*"I deployed a Spring Boot service, pointed Crucible at it, and it found, fixed, and proved a connection-pool bottleneck, with me approving the one change and every number measured."*

---

## Step 1 — Problem statement + architecture + PerfLab (3 min) [EXPLAIN]

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

**Show the loop:** the Crucible repo's README on GitHub → **How it works** →
expand **"The full flow: what is built today, and what is planned (dashed)"**. "Solid
boxes are what runs today. Dashed boxes are designed and not built, and I'd rather
show you the line between them than blur it." Point at the two gates that matter:
the operator's approval, and the target proving the commit before anything is measured.

**PerfLab target (~1 min):**

"PerfLab is the target: a Spring Boot service with one endpoint per cause of
slowness, so every diagnosis has a known right answer." (Source:
`PerfController.java` in github.com/rraghu214/perf-lab; routes checked on Box A,
2 November 2026.)

| Endpoint | Cause family it plants |
|---|---|
| `/api/fast` | none: the baseline, no dependency |
| `/api/db` | connection pool exhaustion (holds a pooled connection 50 ms). **Today's demo** |
| `/api/async` | thread pool saturation |
| `/api/churn` | GC pressure |
| `/api/orders?size=` | inefficient query (N+1) |
| `/api/catalog?page=` | cache miss |
| `/api/downstream` | downstream latency (calls httpbin) |
| `/api/ledger` | lock contention (diagnosable, deliberately not fixable by config) |
| `/api/payload?rows=` | payload serialisation |
| `/api/slow` | application code |
| `/api/version` | app name, pool size in force, and the commit the jar was built from |

"Crucible drives `/api/db` with Locust, reads HikariCP and JVM meters from
`/actuator/metrics`, reads a changed property back from `/actuator/env`, and checks
`/api/version` for the commit it pushed. It measures only once the target proves
the change is running. Each experiment is a commit pushed to the sandbox branch;
a post-receive hook on Box A builds and restarts the service."

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

> **⏸ Pause recording** once the chip reads "indexing…" (~4.5 min for 16 chunks).
> Resume when the chat says "indexed — N chunks".

*(If it was already uploaded in practice, the chip reports "unchanged since
last upload". Say "identical text is never re-embedded" and move on. To avoid
that, repeat the §0.5 retirement after practice.)*

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

**Action:** upload `docs/myfolder/demo-assets/sizes.csv` (one `size` column, six
rows: 10 to 1000). It shows its row count and columns. Add a request row:
`/api/orders`, query `size=${size}`, then map `size` → `sizes.csv` / `size`.

**Say:**
"Realistic load needs realistic inputs. A request is a path on the target, never
another host. Each `${var}` must be mapped to an uploaded file and column, and
that is checked before anything runs. Rows are cycled. Templated endpoints are
judged one endpoint at a time."

*Built-in generators: there are none. No `${uuid}`, random number or timestamp
function exists today. Every `${var}` comes from a column of an uploaded CSV or
JSON file (`crucible/perf/loaddata.py`). If a unique id is needed, put the ids in
the file. If asked, say generators are not built. Do not show them.*

**Remove the request row before launching.** Otherwise the demo run loads
`/api/orders` instead of `/api/db`.

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
  property list and bounds in `spring-boot.yaml`, the target profile. The guardrails make this safe, not
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

**Five files behind every run** (skim them on the Crucible repo's GitHub page,
branch `capstone/perf-agent`; the manifest is on Box B):

| File | What it holds | Agent can write? |
|---|---|---|
| `config/slo.yaml` | Goal (p99, error budget, load profile). Launch writes it | No (Policy memory + protected path) |
| `config/profiles/spring-boot.yaml` | Allowed properties and bounds, protected paths | No (it IS the guard) |
| `config/profiles/spring-boot.SKILL.md` | How Spring Boot fails, in plain prose. Goes into the prompt only | No |
| `locust/locustfile.py` | The built-in load script driving `/api/db` | No |
| `results/<run-id>.json` | The manifest: goal, baseline, every change, approvals, commits, models, verdict | No: Crucible writes it, the agent can't |

"These five files are the whole system's memory and authority. The agent reads most
of them; it writes none of them directly. Everything it does is a parameter change
to one of the values listed in `allowed_properties` of `spring-boot.yaml`."

---

## Step 7 — Guardrails + sandbox structure (2 min) [UI]

**Navigate to:** Guardrails tab.

"Two lists. The core protections cannot be disabled."

"Crucible organises its guardrails in three layers. Session 17 supplied the
concepts behind the first two; Crucible adds the third."

**Layer 1 — command runner bounds.** "Session 17 defines four hard limits on
what the agent can run: no shell (commands are words, not shell strings), an
allowlist of allowed tools, inside the workspace only, and a timeout. An
allowlist of commands is not an allowlist of behaviours — `git` is on the list
but `git push origin main` is refused; `git -c core.pager=id log` executes
programs through git's config and is also refused. The list says which tools
exist; the constraints inside each tool are separate."

**Layer 2 — property bounds in `config/profiles/spring-boot.yaml`.** "`spring-boot.SKILL.md` tells the model how
this runtime fails. It goes into the prompt and grants nothing. `spring-boot.yaml`, the target profile,
grants authority: the allowed properties and their bounds. A SKILL.md saying
'raise the pool to 10,000' has zero effect — that number fails the guard before
it reaches the target. Session 17's rule, stated three times in the PDF: a skill
changes how the agent works; it can never change what the agent is allowed to do."

**Layer 3 — Policy memory.** "The SLA is Policy memory. The agent has no write
permission for it, ever. Two independent locks: a protected path in the file
guard, and the Policy memory kind. Both would have to fail. The operator can
still change it mid-run, and every such edit is recorded on the manifest as an
amendment."

Sandbox row: "Crucible writes to exactly one branch, `perftest_sandbox`, on the
PerfLab repo, never its base branch. Each experiment is one commit of one file,
pushed to that branch. The post-receive hook on Box A rebuilds."

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

> **⏸ Pause recording** while the baseline measures (~7 min to the first proposal).
> Resume when the approval gate appears. Pause again after **Approve** while it
> deploys and re-measures, and resume when the verdict shows.

"The baseline measured p99 above the 120 ms SLA. The pool is 2 of 2, saturated,
with 43 requests waiting at peak."

PDMA graph: "Perception: the collector read HikariCP metrics, converted
Micrometer's seconds to milliseconds before the model saw them, and sampled the
pending gauge during load, so we have the peak, not the post-run zero. Memory:
the journal was searched, so a disproven hypothesis isn't proposed again."

Approval gate (read the value off the screen; the 2 November trial proposed 2 → 50):
"The proposal is pool 2 → <value>, and this exact diff is what I approve. Then Crucible commits that one file, pushes, waits until the target
itself reports the commit it pushed, reads the new value back, and only then runs
a fresh measurement window."

**Click Approve.**

"Crucible drives Locust headless: no web UI, no port 8089. It is a subprocess
at the scenario's users and spawn rate. The diagnosis uses server-side Actuator
metrics — HikariCP pending connections, acquire time, server p99 — not just
Locust's client view. They measure different things."

**Optional, mid-run edit (30 s):** "I can change the goal mid-run." Raise p99 in
the wizard → **💾 Save changes**. "It is taken at the next experiment boundary,
recorded as an amendment, and in Semi-Automated it pauses for me to confirm."

"Every button has a CLI twin, so headless operation in CI works the same way."

---

## Step 10 — Results + memory stores (1.5 min) [LIVE]

**Navigate to:** Results → ↺.

"This comes from the manifest file on Box B, not hardcoded data. The title is
just the run id — the full outcome is in the Findings card below it."

Before/After chart: "Two measured windows, identical load. The only difference
is the pool size."

Trials: "The agent predicted [read off the screen]. The prediction is tracked as calibration,
but the verdict uses only the measurement."

**The stores behind this run (one line each):**
- `~/.crucible/knowledge-index/knowledge.sqlite`: **Knowledge RAG**, the uploaded
  documents in chunks. The chat searches it each turn; Sources says `PERF_LAB_PRODUCT.md`.
- `~/crucible/results/<run-id>.json`: the run manifests (Episode memory, kept as files).
  The **Journal RAG** reads past manifests at diagnosis, so a disproven fix isn't
  proposed again. The chat reads them when you ask about a run; Sources names the manifest.
- `~/.crucible/memory.sqlite`: the agent's own memory: each campaign's locked SLA
  (Policy) and the audit trail. Step 14 opens it.

"Two RAGs, two questions: what the documents say, and what earlier runs measured.
The Sources line on every chat answer says which one it used."

**Show on Box B:**
```bash
ls ~/crucible/results/
python3 -m json.tool ~/crucible/results/<run-id>.json | head -40
```

"Every result is a JSON file. It records the commit that was running, the model
that actually served the diagnosis, the environment, the collector version, each
approval, the caps and any mid-run edits. Reproducible by construction."

---

## Step 11 — Datadog + CI/CD (1 min) [EXPLAIN]

"Crucible reads the target's metrics directly from its Actuator endpoint, and has
adapters for Prometheus (PromQL) and Datadog. PerfLab also publishes the same
meters to Datadog, so an independent tool saw the same run: request volume is low
during the pool=2 baseline, drops to zero while the target restarts, then is about
nine times higher after the fix. Crucible measured 38 to 375 requests a second, 9.9
times. Same meters, different observer."

*Do not say Crucible fetched these through Datadog's API: this run read Actuator.
A live run reading Datadog needs a provider setting and a window fix (DEBT.md).
Datadog's own counts are per rollup bucket, so don't quote them as per-minute.*

**Settings → Metrics connections (preview).** "This is where you name the places Crucible can
read metrics from: add as many as you like, and later pick one by name in a profile or SLA. The list
is the roadmap: Actuator, Prometheus and Datadog are built; Grafana Cloud should work through the
Prometheus adapter; Dynatrace, New Relic and Elastic are planned. A credential is never typed: you give
the name of an environment variable on the server, so the secret is never stored or shown."

*It is a preview: connections live in the browser session only and no run reads them yet. Say so.*

"The deploy interface is the sandbox branch. Here a post-receive hook builds and
restarts. In Jenkins or GitHub Actions, the same push would trigger the pipeline."

---

## Step 12 — History + chat about runs (2.5 min) [LIVE]

**Navigate to:** History & Diff. The run table is live from Box B.

Live AI ON. Type, with a **real run id** from 0.6 (name the run: the chat cannot
tell which one you mean from the screen alone):
`Tell me about run-1793613354320-fully_auto_burst_100u: what did you propose, why, and was anything measured?`
*(The Advisory run. Its id says fully_auto because of the scenario name it was launched with.)*

→ The answer names the run, the cause, the change (pool size 2 → 20) and the evidence
(93 pending connections, acquire time about 2.2 s), says nothing was applied or
measured, and calls the predicted p99 a prediction. (That Advisory run has one
proposal, so "why #1 above #2" has no answer: do not ask it.)

Then: `Why was experiment 1 kept in run-1793610533986-burst_100u?`
*(The 2 November Semi-Automated run, or use the id of the run you just recorded.)*
→ It quotes the measured before → after (2500 → 68 ms, pool → 50), the run's own SLA
(150 ms, raised mid-run from 120 ms) and
the verdict reason, and its **Sources** line names the run manifest
(`results/<run-id>.json › run manifest`), not a document.

Then show the other RAG: `How many requests must Micrometer meters be initialised within?`
→ "10 requests", and its **Sources** line names `PERF_LAB_PRODUCT.md › …`, not a run.
This is the point of the two RAGs: one answer is grounded in the run files, the other
in the uploaded document, and the Sources line says which.

**Say:**
"The chat reads the run files on disk. Measured runs, Advisory runs and ceiling
probes. It picks the run I name, even with a typo, or the one on screen, and
says which. If I name a run that doesn't exist, it says so, rather than answering
about a different one. And it never presents a prediction as a result."

*(Optional: `which runs do I have?` lists them.)*

Every History row is a real manifest (Group 45 removed the demo rows). To show a
comparison, tick two real rows and compare: the Diff card is drawn from the
server's screen 17.

"Comparability is automatic. Two runs on different models or environments are
flagged, not silently compared."

**Ceiling probe:** "A ceiling result is a pair: the last level that met the SLA
and the first that didn't, both measured. It describes the service, so it is
never shown as a verdict on a change."

---

## Step 13 — Benchmarks + the LUCKY quadrant (3 min) [UI]

**Navigate to:** Benchmarks (drawn from disk: the replay files and the live manifests).

### Glossary (one-liners for the audience)

"The benchmark is four things: tasks, fixtures, snapshots and replay. Each number
below comes from the files on disk (counted 3 November 2026)."

| Term | One line | Count | Origin |
|---|---|---|---|
| **Task** | One behaviour the agent must show, asked as a question: "here is the evidence, what is wrong, and what would you change?" Five classes, from "diagnose and repair" to "say nothing is wrong" | **18** tasks in 5 classes (`config/tasks/`) | S18 |
| **Fixture** | PerfLab deliberately put into one broken (or healthy) state, with its true cause written down before any run | **20** fixtures: 18 broken across 9 causes, 2 healthy; 19 captured (`config/fixtures/`) | Crucible |
| **Snapshot** | One fixture measured under real load and frozen: 2 min warmup discarded, 5 min measured, read through one metrics provider | **23** snapshots: 19 Actuator, 2 PromQL, 2 Datadog (`fixtures/`) | Crucible |
| **Replay** | The model diagnoses a stored snapshot. No load, no target: about 2 seconds and a fraction of a cent per case | **40** cases: 26 task–fixture pairs, each asked through every provider view of its fixture. 37 have a true cause; **31 correct** | Crucible |

Plus **live**: the whole loop on the real target. **6** campaigns, **8** experiments.

**Point at the 2×2 grid** (Live card): "Down: was the diagnosis right? Across: did
the fix work? **LUCKY** is wrong diagnosis, fix worked anyway. Outcome-only scoring
calls that a win; the same agent fails the next look-alike. Naming and counting it
is Crucible's addition to Session 18's 'a benchmark is a procedure, not a score'."

**Say the numbers** (run 2, 1 November, gemini-3.5-flash-lite):
- Live: 7 CORRECT, 1 LUCKY, 0 UNLUCKY, 0 WRONG, every SLA met, $0.0115. The LUCKY
  one: on the N+1 fault it first raised the pool (11% faster, wrong cause); the
  next experiment fixed the query.
- Replay: right cause in 31 of 37, task grade 32 of 40, $0.054. Weak classes: D 2/5,
  E 1/2. Say so.
- Scored by six dimensions with no model call: outcome, diagnosis, integrity,
  efficiency, calibration, cost.

*If asked:*
- *"26 test cases"* = task–fixture pairs; each is asked once per provider view → 40.
- *Tests vs benchmark:* the pytest suite (~2,000) checks the code; the 40 + 8
  benchmark results check the agent's reasoning. Every test assertion is drafted
  with its reasoning and reviewed by me (`docs/CRUCIBLE_TEST_ASSERTIONS.md`).

---

## Step 14 — Memory, the files behind it, and the run's receipt (3 min) [LIVE on Box B]

This is a critical section: it is where the audience sees that nothing is hidden.
Everything Crucible knows is a file you can open. (Box B has **no `sqlite3` tool**;
the commands below use `python3`, which is there. Tested 2 November 2026.)

### 14.1 The memory kinds, and what is built today

"The design has seven memory kinds, each with its own lifetime and write authority.
Five are live. Two are designed and not built yet, and I would rather say so.

The taxonomy comes directly from the course — Session 13's PDF defines all seven
kinds in a table (Working, Episode, Fact, Playbook, Policy, Audit, Document
chunk). Crucible inherits them without renaming them."

| Kind | What it holds | Today |
|---|---|---|
| Working | the current hypothesis and in-flight snapshot; dies with the run | live |
| Episode | what each experiment did: the run manifests (15.4) | live, as files. The journal recalls them at diagnosis ("Recall past attempts") |
| Policy | the SLA. The agent can never write it | live, one record per campaign |
| Audit | every approval, apply, refusal and revert. Append-only | live |
| Document chunk | the knowledge corpus, in its own store | live |
| Fact | confirmed operational knowledge | **designed, not built** |
| Playbook | cause signatures that worked, promoted by a human | **designed, not built** (the Playbooks screen says so) |

"The memory write permissions are the third guardrail layer from Step 7. Session
11's rule: the LLM reads most kinds, writes a few, and can never write the audit
log. Crucible adds Policy to that protected set — an agent that can overwrite its
SLA is not safe to run autonomously."

**Do not claim the agent learns across runs beyond recall.** It reads earlier
manifests and is told what was tried and measured (experiment 1 of
`run-1793596989174` was handed 2 earlier attempts on the same pool setting). It does
not write Facts or Playbooks from what it finds. If asked: "It remembers what was
measured, and a human decides what becomes a rule. Promotion is the next step."

### 14.2 The files

```bash
ls ~/.crucible/
```

| File or folder | In one line |
|---|---|
| `knowledge/` | the documents you uploaded, kept as originals so they can be re-indexed |
| `knowledge-index/knowledge.sqlite` | those documents cut into searchable chunks (16 for PERF_LAB_PRODUCT.md) |
| `knowledge-index/knowledge.sqlite.faiss` (+ `.ids.json`) | the vector index: how meaning-based search finds a chunk. Appears after the Step 2 upload |
| `memory.sqlite` | the agent's memory: each campaign's locked SLA (Policy) and the audit trail |
| *(no `memory.sqlite.faiss`)* | not a missing file: Policy and Audit are never embedded, so this memory has nothing to vector-index and Crucible removes an empty index |
| `audit.jsonl` | the append-only log: approvals, applies, refusals, reverts, with times |
| `approvals/` | a request file for each proposal and your decision file. The campaign waits on these: the human gate, on disk |
| `ui-jobs/` | one folder per command started from the dashboard, with its log (what Jobs and Active show) |
| `locks/` | which campaign holds the deploy branch right now: one at a time |
| `series/` | the progress of a multi-scenario run, so it can resume |
| `events/`, `graphs/`, `outbox/`, `a2a.sqlite*` | stores inherited from the course harness; not used in this demo: skip |

Presenter notes: `memory.sqlite` also holds 16 document chunks from 22 October, an older
copy from before the document store was split out (DESIGN.md §14). They were retired on
2 November (status `superseded`, as a re-index does it; backup in `~/backups/` on Box B), so
the count below shows only current records. The chat searches `knowledge-index/`.

### 14.3 Memory and audit, live

```bash
python3 -c 'import sqlite3,os;c=sqlite3.connect("file:"+os.path.expanduser("~/.crucible/memory.sqlite")+"?mode=ro",uri=True);print(c.execute("select kind,count(*) from records where status=\"current\" group by kind").fetchall());[print(r) for r in c.execute("select created_at,kind,substr(text,1,90) from records where kind in (\"policy\",\"audit\") order by created_at desc limit 4")]'
tail -2 ~/.crucible/audit.jsonl
```

"The first line counts memory by kind: Policy and Audit. The next lines are the SLA
written as Policy when the campaign started, and the audit entry for writing it. The
last two lines are the approval you gave and the pool change it applied, 2 to 50."

"Choosing the memory kind is a security decision. An agent that can overwrite its SLA
is not safe to run autonomously. Here it is a structural constraint, not a rule
someone has to remember."

### 14.4 The run's receipt

"Every run writes one file, its receipt. It records the goal it was judged against,
the baseline it measured, every change it tried, who approved it, which commit was
deployed and what the target itself reported running, what was measured afterwards,
which AI model was used and the verdict. The dashboard, the chat and the scorer all
read this file. Nothing lives only in someone's head."

```bash
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));e=d["experiments"][0];print("run:",d["run_id"]);print("goal: p99 <=",d["sla"]["p99_ms"],"ms on",d["sla"]["endpoint"]);print("baseline p99:",d["baseline"]["load"]["p99_ms"],"ms");print("recalled earlier attempts:",len(e["prior_findings"]));print("change:",e["apply_result"]["changes"]);print("approved by:",e["approval"]["responder"]);print("deployed commit:",e["deployed_commit"][:7],"| target reports:",e["deploy"]["observed_commit"][:7]);print("after p99:",e["after"]["load"]["p99_ms"],"ms ->",e["verdict"],"| kept:",e["kept"]);print("model:",d["models_used"])' ~/crucible/results/<run-id>.json
```

"Read it top to bottom: the goal, the starting point, how many earlier attempts the
agent was reminded of, the one change, the human who approved it, the commit we
deployed and the same commit the target itself reports, the result, and the model."

The dashboard shows the summary (Results). The full record, audit trail and memory are
files on the box, which is the point.

---

## Step 15 — CLI parity (1 min) [EXPLAIN]

"Every button calls the same function as its CLI command: same guard, same
approval binding. The UI is not a shortcut."

```bash
cd ~/crucible && set -a; . ./.env; set +a      # hand-typed CLI needs .env loaded
uv run crucible plan
uv run crucible run --all-scenarios --advisory
uv run crucible run --cost-cap 0.05 --wall-clock-cap 2
uv run crucible status <run_id>
uv run crucible approve <run_id> --experiment 1 --as raghu
uv run crucible resume <run_id> --cost-cap 1
uv run crucible report --run <run_id>
uv run crucible diff --a <run_id_a> --b <run_id_b>
uv run crucible bench --tasks config/tasks/ --fixtures fixtures/
```
*Checked on Box B, 3 November 2026: each parses (`--help`). Show them; don't run
`run` or `bench` on camera. `crucible` alone is not on PATH, so keep `uv run`.*

---

## Closing line (30 sec)

"The loop is complete: instrument, load, diagnose, propose, approve, deploy,
verify, record. Every result is a file on disk, and nothing is claimed that
wasn't measured."

---

## Total estimated recording time: ~33 minutes

| Step | Section | Time |
|---|---|---|
| 1 | Problem statement + architecture + PerfLab API | 3 min |
| 2 | Knowledge upload + index | 2 min |
| 3 | Grounded chat | 1.5 min |
| 4 | Input data + request templates | 1 min |
| 5 | Goal & SLA | 1 min |
| 6 | Mode, scenarios, hooks, budget, review | 2.5 min |
| 7 | Guardrails + sandbox | 2 min |
| 8 | Preflight | 1.5 min |
| 9 | Live campaign + approval (+ optional edit) | 3 min |
| 10 | Results + memory stores | 1.5 min |
| 11 | Datadog + CI/CD | 1 min |
| 12 | History + chat about runs | 2.5 min |
| 13 | Benchmarks + LUCKY + glossary | 3 min |
| 14 | Memory taxonomy | 3 min |
| 15 | CLI parity | 1 min |
| — | Closing | 0.5 min |
| | **Total** | **~33 min** |

> Step 2 includes a ⏸ pause during indexing (~4.5 min, not counted in audience time).

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
| Active tab, no campaign | **LIVE** | "No campaign is running"; the pending approval comes from the server |
| Results tab | **LIVE** | `/v1/perf/runs/{id}/report` + Chart.js |
| History table | **LIVE** | `/v1/perf/screens/17` |
| History Diff card, ceiling card, Playbooks | **LIVE** | Screen 17 and `/v1/perf/runs` (ceilings); Playbooks says "not built" |
| Overview KPIs, Library, Benchmarks, Guardrails | **LIVE** | `/v1/perf/runs`, `/v1/perf/services`, screens 18 and 5 |
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
| Chat says a run is "not on disk" | The id is wrong: use an id from `ls ~/crucible/results/` |
| Launch refused (409) | A campaign is running or paused: finish it, or `crucible abort <run>` |
| Launch refused: cap overrun | The cap can't fit the baselines: raise it, or confirm the overrun in the dialog |
| Run paused at a cap | `crucible resume <run> --cost-cap <higher>` or abort, then `crucible report --run <run>` |
| Box B unreachable | History shows "Could not load history": explain the API call; every tab reads the server now, so switch to the CLI |
| Approval gate already dismissed | Refresh: Active re-reads the pending experiment from the server |
| Write action returns 401 | Token not saved: Settings → Control token → Save |
| Write action returns 503 | `CRUCIBLE_CONTROL_TOKEN` missing from Box B `.env`: add it, restart serve (0.1) |
| Active tab shows no live overlay | `curl 127.0.0.1:8113/v1/perf/campaign/active` on Box B; the state file may not exist yet |
| Results chart absent | Chart.js CDN blocked: reload; the KPI strip still shows numbers |
