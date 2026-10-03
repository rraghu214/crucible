# Crucible — what's left, how long, what's waiting on Raghu (revised 3 Oct 2026, updated)

**Date correction:** Sat = 3 Oct, Sun = 4 Oct, Mon = 5 Oct, Tue = 6 Oct, Thu = 8 Oct, Fri = 9–10 Oct.

## Verdict for 10 October
**Yes, a high-quality, robust capstone is deliverable by 10 Oct, on three conditions:**
1. **SSH from this machine works on day 1.** Box wall-clock time is the critical path; everything measured depends on it.
2. **The five integrity gaps below are closed before any capture.** Robust here means every claim Crucible makes is enforced and measured, not just written.
3. **10 Oct is submission-only.** Six build days (3–9 Oct) carry ~65–70 h of work, with day 9 as buffer. Anything that slips comes off the polish list, never off the integrity list.

**What "high quality" means for this project.** This is the bar the claim and the demo are judged against:
- **Every DESIGN.md non-negotiable is enforced at runtime, not only in a unit test.**
- **Provider independence is measured.** The same fixture through Actuator, PromQL and Datadog leads to the same cause.
- **At least 3 scored live campaigns** (T1, T3, T4), with the diagnosis quadrant filled from real data.
- **Replay task set v2** covers all five classes A–E, 3 repeats, with no stale snapshots.
- **The NiceGUI UI shows only real data,** with every action twinned to the CLI.
- **The test suite is green, and every new test is reviewed by you.**
- **The README claim is in EVALUATION.md format,** and anything unmeasured says "not yet measured".

**Five integrity gaps the scan found (must close: ~8 h, added to block B+):**
- **Watchdog never runs in a live campaign.** DESIGN §6 requires observed CPU steal on every manifest; today it's always `None`. Fix: wire it into `LocustRunner` (3 h).
- **SLA as Policy memory is not enforced at runtime.** AGENTS.md non-negotiable 4 says "enforced twice"; only the protected-path half runs (`policy.py` is never called by the campaign). (2 h)
- **Datadog adapter turns a 403 into "never measured"** and carries keys in URL parameters (DEBT.md). It must declare the failure and use headers. (1.5 h)
- **A manual deploy aborts the campaign while its message says "paused"** (`campaign.py:964`). Make it pause, per §11. (1 h)
- **Abort is recorded only inside a free-text string.** It needs a structured manifest field, plus an audit log. (Already in block A.)

## Scale-up for Rohan's bar: many benchmarks, a heavy app
**The benchmark can widen a lot, cheaply, because perf-lab already supports it.** It has 9 cause families, each with its own endpoint and Locust tag (`/db`, `/async`, `/churn`, `/orders`, `/catalog`, `/downstream`, `/ledger`, `/payload`, plus `/slow`). The profile has 12 tunable properties. Today only 3 fixtures are captured. EVALUATION.md's own design is 10 families × 3 severities plus special cases, about 40 Java fixtures.

**Three engineering changes make that affordable in the week:**
1. **One load run, every provider.** `build_measure` reads Actuator, PromQL and Datadog during the same run. One run per fixture state then yields 3 snapshots. The provider-agreement comparison also gets stricter, because the load and window are identical. (~3 h, folded into B1)
2. **An approved overnight capture sweep.** A script outside the `crucible` package walks a fixture list. For each one it commits the state to `perftest_sandbox`, pushes, waits for `/api/version`, then runs `crucible capture`. You approve **the sweep and its fixture list once**, not 40 separate state changes. A human still sets every state, so "Crucible never sets up what it measures" holds. ~40 states × ~11 min ≈ 7–8 h unattended overnight. (~4 h to build and test)
3. **Bounded replay cost.** 3 repeats on Actuator, 1 repeat on PromQL and Datadog for agreement. That's ~20 tasks × ~40 fixtures ≈ 250–300 cases per Actuator repeat, ~1,300 calls in all. At 3 s pacing that's ~70 min and about $1.50 of model spend. It must be checked against `config/quota.yaml` before running.

**Resulting benchmark (target):**
- ~40 fixtures, ~110 snapshots.
- ~20 tasks across all 5 classes (one class-A task per fixable family, B pairs pool/thread/GC, C traps per family, D healthy/near-SLA/code/lock/payload, E no-SLA/ambiguous).
- ~8 scored live campaigns: pool, thread, executor, N+1, cache, downstream, healthy, plus T3 under stakeholder pressure, and gc_and_pool for LUCKY.
- That is ~13× today's benchmark. (Benchmark widening ≈ +22 h; live campaigns ≈ 6–8 h of box time and ~4 h of your approvals.)

**Features that make it a heavy app, each from DESIGN.md (estimates):**
| Feature | DESIGN | Est. | Demo value |
|---|---|---|---|
| Pause / resume (already in brief) | §7 | 3 h | high |
| Ceiling discovery: stepped load, knee reported as a pair | §20 | 6 h | very high: "what is my service actually capable of" |
| Deploy-proof ladder: property read-back rung | §19.6 | 3 h | high: proves the change is in force, not just the build |
| Hooks before_each / after_each / on_abort | §10 | 3 h | medium |
| Quick / Standard / Thorough plans | screen 10 | 2 h | medium |
| Playbooks: learned signature, human promotion | §13 | 4 h | medium–high |
| Jaeger findings into the snapshot (spans, not just flags) | §4.3, §5 | 3 h | medium |
| k6 runner, a second LoadRunner | §5 | 5 h | medium: proves the interface |

**Raghu's decisions, 3 Oct:**
- All four feature groups are in.
- Benchmark: ~40 fixtures and ~20 tasks.
- One approved overnight capture sweep.

**Other items still in the list** (from the scan and DEBT.md, not yet discussed):
| Item | Where | Est. | Recommend |
|---|---|---|---|
| Read `runtime_config` through the redaction allowlist (`/actuator/env` is never fetched today) | §12, non-negotiable 7 | 1 h | **yes**, shares code with the read-back rung |
| Bearer token on the hosted gateway (today anyone with the URL can spend the quota) | DEBT | 1 h | **yes**, robustness |
| Deployer-reported commit and exit status on the manifest | DEBT, §19.6 | 1 h | **yes** |
| Manual modes for revert and DB reset | §11 | 2 h | yes |
| `crucible export` (collection as YAML, credentials never exported) | screen 3, §8 | 1 h | yes |
| Fix flaky async tests (CI masks them with `--reruns`) | DEBT | 2 h | yes, so "suite green" means something |
| Config scoping service/collection levels, each value showing its source | §2 | 3 h | stretch |
| Journal RAG vector half, `embedder_id` pinning, startup canary | §14 | 5 h | stretch |
| Unknown-metric "ask the operator" flow | §4.8 | 2 h | stretch |
| Direct model provider (Anthropic/OpenAI) besides glc_v5 | §5 | 3 h | no: adds a second model to compare |
| FastAPI target app (Python slice), knowledge RAG, MCP, CI/CD | brief: out of scope | 1–2 days each | no |

**Honest capacity check (updated).**
- Base plan (~65–70 h) + benchmark widening (~22 h) + all four feature groups (~30 h) + the "yes" rows above (~8 h) ≈ **125–130 h.**
- That is more than 6 build days can deliver at the quality bar. My hours compress; your review and approval hours don't.
- **The way through: commit in tiers, with the cut line pre-agreed.**
  - **Tier 1, must ship:** integrity gaps, benchmark widening, captures, live campaigns, NiceGUI campaign path, results and claim.
  - **Tier 2, planned:** pause, ceiling discovery, read-back rung + `runtime_config`, plans + hooks, the "yes" rows.
  - **Tier 3, stretch:** playbooks, Jaeger spans, k6, then the stretch rows. Built only once Tier 2 is green, and listed as "not built" in the claim if they don't land.
- **My estimate:** Tier 1 and 2 by Thu 9 Oct with high confidence. Tier 3 at roughly 50%.

**Earlier capacity note.**
- **The base plan above (~65–70 h) plus all of this (~+48 h) is ~115 h.** That's more than 6 build days can hold at the quality bar, mainly because your review and approval time doesn't scale with mine.
- **What fits by 10 Oct with quality intact: the base plan + the full benchmark widening + 3–4 of the features.**
- **My pick:** ceiling discovery, the property read-back rung, pause, and Quick/Standard/Thorough plans. Together they're the strongest demo story and lowest risk.
- Everything not picked goes into the claim as "not built", by name.

## Context
- **Deadline:** 10 Oct, set by Raghu on 3 Oct. SCOPE_FALLBACK.md §1: a confirmed extension means *restore full scope*.
- **Why the UI decision changed:** Raghu found the existing 19-screen A2UI UI unacceptable. The NiceGUI design (`docs/ref/crucible-nicegui-screens.html`) is the target.
- **Datadog:** kept.
- **State:** nothing in either repo has changed since 26 Sep (last commit `a5515e7`).
- **Source:** findings come from a full scan of `crucible/`, `config/`, `docs/`, `tests/` and perf-lab.

## Where it stands

### Done as of 3 Oct 2026
- The loop: diagnose → approve → apply → deploy → verify → keep or revert. One live run on 21 Sep.
- Scorer with all 6 dimensions, including the LUCKY quadrant.
- Journal RAG (the structured half).
- Replay benchmark, with 15 cases.
- 3 Actuator fixtures.
- CLI: 12 verbs.
- Production, branch and lock guards.
- FastAPI profile.
- 61 test files.
- B5: Tomcat thread meters + collector 1.2.0.
- B6: task set v2 — T2→B (discrimination), T3b, T6 (class E), T7–T17 new tasks.
- B7: 14 new fixture YAMLs, README updated (20 fixtures, 24 snapshots).
- B8: `/api/slow` endpoint + `slow` tag in locustfile.
- Campaign test hang fixed (ManualStep.wait blocked with wait=True in test; fixed with wait=False).
- Task-fixture agreement failures fixed (fixture: scoping key in asserts_fixture).

### Written but not wired into a live run
- Watchdog: `LocustRunner` never constructs it, so `manifest.watchdog` is always `None`.
- PromQL and Datadog adapters: `build_measure` reads Actuator only.
- SLA-as-Policy-memory half.
- Redaction of `runtime_config` (non-negotiable 7 — `/actuator/env` never fetched).

### Missing
- Pause.
- Stakeholder prompt delivery.
- Confidence cap.
- Datadog registry in perf-lab.
- Any scored live campaign.
- A demo.
- Also missing, but outside the brief: hooks, k6, ceiling discovery, Quick/Standard/Thorough plans.

## 1 · Remaining work (focused hours)
| Block | What | Est. |
|---|---|---|
| **A · NiceGUI UI** | campaign event stream; pause/resume; abort recorded on manifest and audit log; `perf_data.py` split; NiceGUI app (6 areas, 7-step stepper, tabs, Ctrl+K palette, g-chords, A/S/P, themes, thermal colours, CLI twins, screen-map dialog); authenticated actions; tests | 22–27 h |
| **B · Benchmark code** | B1 PromQL and Datadog in `build_measure`; B2 Datadog registry in perf-lab; B3 stakeholder_request; B4 confidence cap; B5 thread meters and collector 1.2.0; B6 task set v2 (T2→B, T3b, T6); B7 6 fixture YAMLs and multi-tag; B8 `/api/slow` and `slow` tag; tests | 16 h |
| **B+ · Integrity gaps** | the five above: watchdog wired, Policy-memory SLA enforced at runtime, Datadog 403 declared and keys in headers, manual deploy pauses | ~8 h |
| **C · Boxes** | SSH from here, each command approved: Prometheus, Datadog check, deploy, K1 re-check, ~16 snapshots (10 fixtures; 3 providers on pool_starved, healthy, gc_pressure), 3 live campaigns T1/T4/T3 | 2 h prep + **6–7 h wall clock** |
| **D · Results** | replay v2 × 3 repeats; score live manifests; rewrite both results docs with v1 as history; provider-agreement table; README claim in EVALUATION.md format | 4 h |
| **E · Submission** | demo script and recording, final README, test-assertion review | 3–4 h |
| **Total** | | **~62–70 h**, of which ~10–12 h is yours |

## 2 · My take on the week (ordered by risk, not by brief part)
The long pole is **box wall-clock time**, not code. B5 bumps the collector, which makes every existing snapshot stale, so all fixtures must be recaptured. Captures and live campaigns can't be sped up. So benchmark code goes first and box sessions start early, while the UI is built alongside.

| Date | Me | Raghu | Gate at end of day |
|---|---|---|---|
| **Fri 3 Oct** ✅ | B5 thread meters + collector 1.2.0; B6 task set v2; B7 fixtures; B8 `/api/slow`; test suite green | NSG fix; Datadog account + keys; answer the 5 decisions | SSH to both boxes works |
| **Sat 4 Oct** | integrity gaps (watchdog, Policy SLA, Datadog 403 + headers, manual-deploy pause); B3; read-back rung + `runtime_config` via allowlist; gateway token; deployer commit on manifest. **Box 1:** Prometheus, Datadog check, deploy, **K1 re-check** | approve box commands; push perf-lab | K1 spread <20% on collector 1.2.0, or stop |
| **Sun 5 Oct** | B4; B6 + ~15 new tasks; ~37 new fixture YAMLs (tag checked against the locustfile); scorer rules; **sweep script**, dry-run on 2 fixtures. **Overnight:** sweep of ~40 states → ~110 snapshots | review and approve the sweep script and fixture list once | dry run produces valid snapshots from all 3 providers |
| **Mon 6 Oct** | validate the sweep output (signal present per fixture, `validated_at`); pause/resume, event stream, abort + audit; plans + hooks; manual revert/DB reset. **Box:** live campaigns batch 1 (pool, thread, executor, T3 under pressure) | approve proposals in person (~2 h) | sweep validated; 4 manifests scored |
| **Tue 7 Oct** | ceiling discovery; `perf_data.py` split; NiceGUI shell + Home, Campaign tabs, Benchmark. **Box:** live batch 2 (N+1, cache, downstream, healthy, gc_and_pool) + one ceiling probe | approve proposals (~2 h); review 3–6 Oct commits | 9 manifests scored; campaign path works in the browser |
| **Wed 8 Oct** | NiceGUI: stepper, History, Settings, palette, g-chords, themes, tooltips, screen map; action endpoints; UI tests; `crucible export`; flaky-test fix. **Replay v2** (Actuator ×3, PromQL/Datadog ×1) | review test assertions | full suite green with no reruns; replay v2 written |
| **Thu 9 Oct** | D: scoring, results docs (v1 kept as history), provider agreement, diagnosis quadrant, README claim. **Tier 3 if Tier 2 is green:** playbooks, Jaeger spans, k6 | dry-run the demo through the UI; push | Tier 1 + 2 committed and pushed |
| **Fri 10 Oct** | fixes from the dry run only | record the demo, submit | submitted |

**Why this order.** The collector bump on 3 Oct makes every snapshot stale, and boxes can't be hurried, so captures run Sat–Sun while code continues. The UI goes last because it reads the data the captures and campaigns produce: building it on real manifests avoids mock-up numbers by construction. Integrity fixes land before any capture, so nothing measured has to be re-measured.

## 2a · Risks, and what happens if one hits
| Risk | Likelihood | Mitigation |
|---|---|---|
| SSH or NSG blocked, home IP changes | medium | Fix on day 1. If it slips past Sat, captures move to Sun–Mon and UI polish is cut. |
| K1 re-check fails (spread >20%) | low–medium | Stop capturing. A capture on an unstable box is worthless. Check CPU steal (the watchdog now records it); re-run off-peak. |
| Datadog free tier: site mismatch, 1-day retention, push delay | medium | Verify on Sat before captures, and capture the Datadog snapshot right after each Actuator one. |
| Hosted gateway cold start or Gemini quota during replay or live | low | Warm-up already in place; ~150 replay cases ≈ 15 min of quota; 3 s pacing. |
| A live campaign fails mid-run | medium | That's a real result: record it honestly (UNREACHABLE or HONEST_FAILURE), fix the cause, re-run. Mon has room for one re-run. |
| NiceGUI integration is slower than estimated (first use in this repo) | medium | The campaign path (Tue) is the must-have; palette, g-chords and Graph tab are cut first. |
| Review bottleneck: you approve SSH steps, commits and tests | high | Batch: I queue commands per state change; you review commits once a day (Tue, Thu). |
| Overnight sweep stalls (build failure, deploy unverified, IP change) | medium | The sweep stops on the first unverified deploy, never captures a wrong state, logs where it stopped, and resumes from there. |
| Gemini free-tier daily quota at ~1,300 replay calls | medium | Check `config/quota.yaml` first; spread repeats across 2 days if needed; 5-key rotation. |

**Cut order if time runs out** (never touching Tier 1): k6 → Jaeger spans → playbooks → stretch rows → palette and g-chords → Graph tab → hooks → Quick/Standard/Thorough plans. **Never cut:** integrity gaps, the sweep, live campaigns, provider agreement, NiceGUI campaign path, the README claim.

## 3 · Hard dependencies on Raghu

### ✅ Done
- NSG rules are correct: TCP 22 from `49.37.241.32/32`, TCP 8080 and 9090 from `10.0.0.8/32`. Verified 3 Oct.
- NiceGUI design approved. A "Crucible" wordmark/logo in the top-left of every screen links back to Home (Chivo typography, per the design file).

### Immediate (today, Fri 3 Oct)
1. **Lock the key file and run SSH tests.** Run these in PowerShell in order — read-only, print hostname only:
   ```powershell
   $k="C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key"
   icacls $k /inheritance:r; icacls $k /grant:r "$($env:USERNAME):(R)"
   ```
   Then Box A:
   ```powershell
   ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" -o IdentitiesOnly=yes -o ConnectTimeout=10 ubuntu@129.213.121.108 hostname
   ```
   Then Box B:
   ```powershell
   ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" -o IdentitiesOnly=yes -o ConnectTimeout=10 ubuntu@150.136.143.227 hostname
   ```
   Expected: each prints a hostname in <5 s. A timeout = NSG IP mismatch. "Permission denied" = wrong key.

### Decisions — resolved Sat 3 Oct
2. **All six decisions answered:**

   | # | Decision | Resolution |
   |---|---|---|
   | D1 | Stakeholder prompt framing | ✅ Separate block "a stakeholder asks:" — context only, no authority |
   | D2 | T2 class-B grading | ✅ PASS if gc_pressure named primary AND no pool-size fix proposed |
   | D3 | T6 class-E grading | ✅ PASS only if agent shows a number (p99 vs SLA) — "looks fine" fails |
   | D4 | gc_and_pool ground truth | ✅ gc_pressure primary; pool starvation secondary note |
   | D5 | `/api/slow` in locustfile.py | ✅ Approved — endpoint simulates slow business-logic code (sleep/CPU burn), no DB/pool involved; needed for `perflab_code_latency` fixture |
   | D6 | Uncommitted files | Review, update, then push (Raghu to do when ready) |

3. **Datadog signup — Sun 4 Oct afternoon**, after major code tasks done.
   - Sign up at https://www.datadoghq.com/ (no credit card required for 14-day trial).
   - Note the region from your post-login URL: `app.datadoghq.com` = US1 → API host `https://api.datadoghq.com`.
   - Create API key (`perflab-boxa`) and Application key (`crucible-read`) under Org Settings.
   - After the 14-day trial the account moves to a permanent free tier. The captured snapshots are on disk regardless — the instructor reviewing after submission doesn't need live Datadog access for the benchmark replay to work.

### Sun 4 Oct (box session — after major code done)
4. Sign up for Datadog (afternoon).
5. Run box commands I'll queue: start Prometheus, Datadog check, deploy check, K1 re-check. (~30 min, one approval per command)
6. Push the perf-lab changes written Sat (thread meters, Datadog registry, hook JAVA_OPTS).
7. **Approve the overnight capture sweep once** — review the script and fixture list (~10 min), then it runs unattended overnight.

### Mon 5 Oct – Tue 6 Oct
8. **Approve ~9 live campaign proposals in person** (~4 h total across both days). One at a time, per DESIGN §19.4.

### Thu 9 Oct
9. Dry-run the demo through the UI; give me any fixes.

### Fri 10 Oct
10. Record the demo and submit.

### Ongoing
11. **Test-assertion review** (AGENTS.md): Groups 19, 25–28, items 8.6, 21.5 and 22.6, and the unmarked groups 1–7 and 9–18. Can be done incrementally as I write new tests.
12. **Pushing:** nothing leaves this machine without you.

## 3a · Fixture state, and why it's an approval rather than your hands
A fixture is perf-lab in one known broken state, with the true cause written down first: pool=2 is "starved", pool=20 is "healthy". Setting it means:
1. Edit one line in perf-lab's `application.properties`.
2. Commit it to `perftest_sandbox` and push.
3. The hook (`scripts/box_a_post_receive.sh`) rebuilds, restarts and proves the commit via `/api/version`.
4. `crucible capture` measures it.

**Crucible itself refuses to do step 1** (`capture_fixture`). If the graded tool also wrote the broken config, the answer key and the exam would share a source, and one bug could make both wrong in the same direction undetectably.

**I can do it over SSH outside Crucible, with you approving.** That keeps the rule.

**gc_pressure exception.** `-Xmx128m` is a JVM flag, and the hook starts `java -jar` with no JVM options. So the hook needs a 2-line `PERFLAB_JAVA_OPTS` change on Box A, approved by you.

**Prometheus:** I start it myself (`docker compose up -d prometheus`, approved). Check: `hikaricp_connections_max` returns a value.

## 4 · Datadog setup — your steps (~20 min)
1. **Take the 14-day trial.** It's the right choice, not a second-best:
   - Every capture and live campaign happens between 4 and 9 Oct, well inside 14 days, and the trial has no feature limits.
   - When it ends, Datadog's pricing page lists a permanent **Free** infrastructure plan ("up to 5 hosts", "1-day metric retention"). Third-party guides say the account moves to it automatically; Datadog's own page doesn't say how. Either way, a lapsed trial costs nothing: the snapshots we capture are on disk, so nothing after the trial depends on Datadog.
   - Sign up from https://www.datadoghq.com/pricing/ via "Start Free Trial". If it asks for a card, stop and tell me; I'm not aware that it does.
   - Note the **site** from the post-login URL: `app.datadoghq.com` = US1 → API `https://api.datadoghq.com`; `us5.datadoghq.com` → `https://api.us5.datadoghq.com`; `app.datadoghq.eu` → `https://api.datadoghq.eu`. A wrong site gives a 403 that looks like "no data".
2. Skip the Agent install in onboarding. Micrometer pushes directly, and no Agent goes on Box A.
3. Organization Settings → **API Keys** → New Key, `perflab-boxa`. Copy it.
4. Organization Settings → **Application Keys** → New Key, `crucible-read`. Scopes, if asked: `timeseries_query` and `metrics_read`. Copy it; it's shown once.
5. **Box A:** create `~/perflab.env` with `DATADOG_API_KEY=…` and `DATADOG_SITE_URI=<API host from step 1>`, then `chmod 600 ~/perflab.env`.
6. **Box B:** append `DATADOG_API_KEY=…`, `DATADOG_APP_KEY=…` and `DATADOG_API_BASE=<API host>` to `~/crucible/.env` (git-ignored).
   - Paste them yourself, so keys never pass through this chat or a commit.
7. **I build:** `micrometer-registry-datadog` in perf-lab's `pom.xml`, plus `application-datadog.properties` (`management.datadog.metrics.export.api-key=${DATADOG_API_KEY}`, `.uri=${DATADOG_SITE_URI}`, `.step=10s`). It's active only when the key is set. The hook sources `~/perflab.env` and adds the `datadog` profile. `build_measure` builds `DatadogMetricsProvider` from Box B's env.
8. **Check:**
   - In the Datadog UI → Metrics → Explorer, `hikaricp.connections.pending` shows a series tagged `application:perf-lab` within ~2 min of load.
   - A probe from Box B returns a value, not `None`.
   - Free-tier **1-day retention**: capture within 24 h of the run. The snapshot on disk is what persists.

## 5 · Opening SSH to this machine — your steps (~10 min)
Both boxes use the NSG `ig-quick-action-NSG`. Its rule is TCP 22 from `<home IP>/32`.
1. Open https://ifconfig.me on this machine and note the IPv4 address.
2. https://cloud.oracle.com, region **US East (Ashburn)** → ☰ → Networking → Virtual cloud networks → your VCN → Network Security Groups → **ig-quick-action-NSG** → Security Rules.
3. Edit the **TCP 22** ingress rule. Set Source CIDR to `<your IP>/32` and save. Edit, don't add. Never use `0.0.0.0/0`.
4. Same NSG → Add Rule: Ingress, CIDR `10.0.0.8/32`, TCP, destination port **9090**, described `prometheus from box-b`. I'll add the matching Box A iptables rule over SSH (`sudo iptables -I INPUT 5 -p tcp -s 10.0.0.8/32 --dport 9090 -m state --state NEW -j ACCEPT` and `sudo netfilter-persistent save`).
5. Check that both instances list this NSG: Compute → Instances → Attached VNICs.
6. Lock down the key file for Windows OpenSSH, in PowerShell:
   `$k="C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key"; icacls $k /inheritance:r; icacls $k /grant:r "$($env:USERNAME):(R)"`
7. **I then run, with your approval:** `ssh -i <key> -o IdentitiesOnly=yes -o ConnectTimeout=10 ubuntu@129.213.121.108 hostname` (Box A), and the same for `150.136.143.227` (Box B).
   - A timeout means the NSG rule or the IP is wrong.
   - "Permission denied (publickey)" means the wrong key.
   - If SSH starts timing out later, your home IP has probably changed; redo step 3.

## 6 · Seeing the NiceGUI design now
Open `docs\ref\crucible-nicegui-screens.html` in a browser (double-click it, or `start` the path).
- It's a static mock-up of the approved design, and the footer says "prototype data". The figures are from real runs, but the live log, preflight and connection test are simulated in JS.
- Press **.** to label each panel with its intended NiceGUI component, **?** for shortcuts, **Ctrl K** for the palette.
- The real build renders through Quasar, so expect small spacing and widget differences. Colours, layout, type and structure carry over.
- I can publish it as a private Artifact link once we're out of plan mode.

## Verification
- `uv run pytest -q && uv run ruff check .` before every commit; one logical unit per commit; nothing pushed without Raghu.
- `crucible capture --plan` lists exactly the intended ~16 snapshots.
- `crucible bench` refuses the stale 1.1.0 snapshots by name after B5.
- `crucible serve`, then click through all six areas against real `results/` and `fixtures/`. Each empty state names the command that fills it.
- `crucible score` reads the live manifests copied back from Box B.
