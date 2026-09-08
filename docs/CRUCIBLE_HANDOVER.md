# Crucible — Handover

**Written 7 September 2026.** Everything here is grounded in the conversation that
produced it. Where something was never decided, it says so rather than guessing.

**How to use this.** Paste or upload this file at the start of a new conversation.
It carries the origin, every design decision with the reason it exists, current
state, open questions and the roadmap. It does not carry the screen mockups —
those live in `crucible-screens-v2.html`, which should be uploaded alongside it.

---

# 1 · What Crucible is

**Vision**

> Any engineer who owns a service can find out why it is slow and prove their fix
> worked — whatever their stack, and whatever they spend on observability.

**Mission**

> Build an autonomous performance engineer: it runs its own experiments,
> diagnoses from the evidence it can actually reach, proposes bounded changes
> under human control, and proves every claim with a measurement rather than an
> assertion.

**One sentence, as submitted to the course**

> An autonomous performance engineer that runs controlled load experiments
> against a Spring Boot service, diagnoses why it misses its latency SLA from its
> own telemetry, proposes bounded configuration changes under human approval,
> re-tests, and keeps or reverts each change on measured evidence.

**Five principles.** Each came out of something that actually happened during the
feasibility spike or out of a specific session lesson.

1. **Nothing is claimed that was not measured.** Verified and unverified are
   different outcomes and must never look identical in a results table.
2. **The agent knows what it cannot see.** Missing evidence is declared, never
   assumed absent.
3. **The agent never grades itself.** It cannot edit the SLA, the load profile, or
   the tests.
4. **A human holds every irreversible action.**
5. **Understanding is scored separately from outcome.** Being right by luck is not
   being right.

---

# 2 · How this project was chosen

Recorded because the reasoning matters, and because two of the rejected options
may come back later.

## The course context

EAG V3, The School of AI, instructor Rohan. Twenty sessions. The capstone offered
three routes:

- **Route A** — one of 27 agent seats inside AgentSwitch (later called Arcturus
  2.0), the instructor's own business suite. Codebase provided, proprietary,
  scored `10 × hand-written tests + 100 × bugs found`.
- **Route B** — one of 10 instructor-supervised projects (UAV swarm, fluid/thermal/
  aerodynamic simulation, RF antenna, PCB design, CNC, GPS-denied navigation).
  Solo, proprietary, may not use frontier models because several must run on edge
  hardware.
- **Route C** — your own project. No third-party harness, four weeks of real work,
  a clear deliverable, deployed on a real box.

**Route C was chosen.** Submitted solo. Session 19 form due 4 September; Session 20
pitch delivered 5 September.

## Ideas that were considered and rejected

**RF Antenna Design Agent (Route B #7).** Seriously considered for two turns. The
thesis was strong — an agent that knows when its own simulation is lying, using
NEC2 validity checks (segment rules, Average Gain Test, convergence under
refinement) because NEC2 returns confident numbers from physically invalid models.
Sim-to-real validation with a NanoVNA would have been unique in the cohort.

**Rejected because Claude fabricated a premise.** In an earlier turn Claude stated
"you're a ham" as if correcting an earlier mistake. That was invented — the ham
radio equipment belongs to Raghu's father, and Raghu has no antenna knowledge. The
whole project rested on being able to tell a correct radiation pattern from a
plausible-but-wrong one. Without that, weeks one and two would have gone on
learning RF fundamentals instead of building.

**The lesson that survived:** domain knowledge is what converts a short timeline
into finished work. That criterion is why performance engineering won.

**TIBCO 5.14→5.16 migration agent.** Strongest career asset available, genuinely
under-tooled. Rejected for the capstone because Rohan said plainly he is not
interested in that domain, so there would be no mentorship, and enterprise
integration would not compete for attention in a room of hardware demos. Noted as
worth building after October.

**Dev/CI copilot.** Safest and most portfolio-friendly. Dropped because Raghu said
it did not interest him.

**Two commercial tools exist in this space**, found by Raghu and named in the
Session 20 pitch: **Akamas** (autonomous tuning, optimisation-focused rather than
diagnosis-focused) and **Tricentis NeoLoad** (load testing with analysis, not an
agent that decides what to try next). Neither closes the diagnose → propose →
verify loop. Nothing open source does.

---

# 3 · Where things stand right now

## Repository

```
github.com/rraghu214/crucible
branch: capstone/perf-agent
```

Forked from `theschoolofai/S17Code` via `rraghu214/S17Code_rraghu214`, then
detached into a standalone repo. `main` holds the clean S17Code baseline;
`capstone/perf-agent` holds all capstone work.

Commits, in order:

| SHA | What |
|---|---|
| `5a9e60f` | rename package `s17code` → `crucible` (package, env vars, imports, narrative) |
| `6b4965a` | start `docs/ref/DEBT.md` |
| `58cbdc3` | PerfLab Spring Boot target service |
| `3a63b13` | Locust load profile and load dependency group |
| `af6b16e` | hold pooled connection across simulated DB latency in `/api/db` |
| `c93e8d1` | K1 measurement-stability result (PASS) |
| `252ee77` | require mid-run gauge sampling in the metrics collector |
| `8743b76` | K3 probe scripts |
| `6fcb929` | K3 pass with measured pool=20 verification |
| `a95bcbf` | healthy baseline set to pool=20; spike verdict |
| `8ee5265` | ruff clean — two real bug fixes |
| `dac86f4` | smoke tests for retriever and validate_work |
| `9b53cb4` | pytest reruns; p4 continue-on-error; test isolation; DEBT entries |

**CI is green** as of run 33741751057 — the first time that workflow has ever
passed. It had been red since it was added, dying at `ruff check` before tests
ever ran.

## What exists in the repo

- `crucible/` — the S17Code runtime, renamed. Contains `coding/`, `economics/`,
  `events/`, `evals/`, `core/memory/` (FAISS), `core/a2a/`, `core/live_graph/`,
  `ui/` (HITL), `skills/`, `telemetry/`, `reasoning/`, `gateway.py`, `planner.py`.
- `perf-lab/` — Spring Boot 3.3.5, Java 21, H2 in-memory, three endpoints
  (`/api/fast`, `/api/db`, `/api/version`), Micrometer + Actuator + Prometheus
  registry. `/api/db` holds a pooled connection across a `Thread.sleep(50)`.
- `locust/locustfile.py` — 50 users, spawn rate 10, targets `/api/db` only.
- `k3_probe/` — `build_snapshot.py`, `diagnosis_prompt.txt`, `run_probe.py`.
- `docs/` — `K1_RESULT.md`, `K3_RESULT.md`, `SPIKE_VERDICT.md`, `DESIGN.md`,
  `FLOW.md`, `ref/PERF_AGENT_SPIKE_PLAN.md`, `ref/DEBT.md`.

## Two real bugs found in the instructor's codebase

Both broken since commit `69c48c4` ("Extract capability workers"), both
planner-selectable capabilities with no test coverage, both undetected because CI
never ran:

- `crucible/workers/special.py` used `os.getenv`/`os.environ` with no `import os`
  → `validate_work` would `NameError` on first call.
- `crucible/workers/general.py` `run_retriever` called an undefined `recall` →
  the retriever capability would `NameError`.

Smoke tests were added and verified to fail without the fixes. **Rohan has not yet
been told** — a message was drafted but sending was deferred until after the form
was submitted. Worth doing.

## Known debt, deliberately not fixed

Recorded in `docs/ref/DEBT.md`:

- `OfflineTransport` in `proofs/harness.py` returns a canned blob that is not a
  planner graph-patch, so no node executes offline. `p4_trace_export.py` therefore
  fails its hierarchy assertion. **It also cannot pass against any real run** —
  p4 asserts every `provider_call` span has a `node` parent, but `spans.py`
  deliberately nests the planner's own metered call under `plan`. This is a
  contradiction in the instructor's code, not ours. Marked `continue-on-error`.
- Async timing races in `test_live_graph.py` and the A2A hardening tests. Masked
  by `--reruns 2`, not fixed. DEBT.md says so explicitly.

---

# 4 · The feasibility spike — what was measured

Two kill tests were run before any agent code, both passed, both committed.

## K1 — measurement stability

Three baseline runs at pool=10, three bottleneck runs at pool=2. 50 users, 90 s,
`/api/db`.

| run | p50 | p95 | p99 | rps | pending | acquire mean |
|---|---|---|---|---|---|---|
| baseline-01 | 89 | 120 | 140 | 169.9 | 0 (post-run) | 21.1 ms |
| baseline-02 | 89 | 130 | 150 | 167.1 | 0 (post-run) | 20.7 ms |
| baseline-03 | 89 | 130 | 160 | 169.9 | 0 (post-run) | 20.9 ms |
| bottleneck-01 | 1200 | 1300 | 1300 | 34.8 | 43 (mid-run) | 1103 ms |
| bottleneck-02 | 1200 | 1300 | 1300 | 34.9 | 42 (mid-run) | 1133 ms |
| bottleneck-03 | 1200 | 1300 | 1300 | 35.0 | 43 (mid-run) | 1124 ms |

- Baseline p99 spread **14.3%** — under the 20% criterion
- Bottleneck p99 **8.7×** baseline median
- `hikaricp.connections.pending` nonzero in all bottleneck runs, but **only when
  sampled during load**

**Note.** H2 was initially too fast for pool=2 to saturate — first attempt showed
acquire MAX of 3.98 ms. `Thread.sleep(50)` inside a `@Transactional` method was
added to `/api/db` so the pooled connection is held across realistic DB latency.
That is what makes the fixture work.

## K3 — diagnosis quality

Three probe runs against saved snapshots, via glc_v5 with Gemini pinned.

| # | snapshot given | cause chosen | correct? |
|---|---|---|---|
| 1 | raw metrics only | Tomcat thread pool | **NO** |
| 2 | metrics + `hikaricp_derived` (units fixed) | HikariCP pool | YES |
| 3 | above + `runtime_config` | HikariCP pool | YES |

**Attempt 1 failed for two reasons, and both became design constraints:**

1. **Units.** Micrometer reports timers in **seconds**. The raw snapshot showed
   `acquire MAX: 2.4`. The model read that as 2.4 ms — a healthy connection wait —
   and ruled the pool out. It actually meant **2406 ms**. True mean was
   `3499.07 / 3186 = 1098 ms`.
2. **Drained gauge.** `pending` was sampled after load finished, so it read 0. The
   model saw zero waiters and crossed the pool off. During the run it was 43.

The fix was to the **snapshot, not the prompt**.

**Verification.** The agent proposed `maximum-pool-size` 2 → 20. That exact value
was run: p99 **1300 → 93 ms**, acquire mean **1098 → 0.11 ms**, rps 35 → 186, zero
failures, SLA (200 ms) met with margin.

**Calibration finding.** The model predicted 140 ms and measured 93 ms —
conservative by ~1.5×. Critically, the K1 pool=10 baseline was ~150 ms, so had
that been used as the "after" figure the prediction would have looked near-perfect
by coincidence. This is why verification must always re-run the *actual proposed
value*, never an adjacent one.

---

# 5 · Session 20 and what it changed

Pitched 5 September. Delivered, accepted, Route C confirmed. Listed in the session
notes as: *"Raghu Rammohan — API performance tuning as an agent loop — Fifteen
years of APIs and middleware, running jmeter and locust today."*

**The deciding exchange.** Rohan went back to the benchmark slide and asked how to
take it *"from T4 to T400"*, then said: **"I think you're going to solve the whole
thing in four five days."**

That is the scope-too-small objection. Raghu answered on his feet — widen the
app's tunable surface, more bottleneck avenues, more benchmarks — then showed the
Part 2 adapter slide. Rohan closed with **"Got it. Professional. If this is a part
also included, then it's fine."**

**Consequences:**

- The adapter layer is **in scope**, not roadmap. It is what made the four weeks
  credible.
- The benchmark must be materially wider than four tasks. T400 was directional,
  not a hard target (Raghu's read, and it is the right one).
- Session 20 notes state plainly: *"Every test written by your own hand. A test
  written by Claude or Codex scores zero. We can tell."* This is stated under
  Route A marking but tests are the scoring mechanism. **Claude Code may implement;
  the test assertions and their reasoning must be Raghu's.**

**Timeline: four weeks from 7 September, fixed, will not be extended.**

---

# 6 · Design decisions, with the reason each exists

This is the section that matters most. Nothing here is arbitrary.

## 6.1 Built on S17Code rather than from scratch

Route C forbids third-party harnesses (LangChain, CrewAI, AutoGen). The EAG
harness is explicitly permitted. S17Code already carries the guarded coding loop,
economics, event engine, HITL, telemetry, FAISS memory and the gateway seam.
Rebuilding those would have cost a week of four.

## 6.2 Model pinned per campaign, no failover

`glc_v5` supports fallback providers on 429/502/503. **This is disabled.**
Cross-provider failover resolves to a different model family mid-campaign, which
would silently invalidate every experiment-to-experiment comparison. Direct
descendant of the S18 controlled-comparison discipline.

## 6.3 The collector converts units and pre-computes derived values

From K3 attempt 1. Raw Micrometer tuples (`COUNT`/`TOTAL_TIME`/`MAX` in seconds)
are never handed to the model. The snapshot carries `acquire_mean_ms` with the
division already done, and every field name ends in its unit.

## 6.4 Gauges are sampled during load, peak recorded

From K3 attempt 1 and confirmed by K1. `pending` reads 0 once load drains. The
load runner runs a background sampler and the snapshot carries the peak.

**Corollary:** an unsampled gauge is `null`, never `0`. A clean zero and an
untested zero must not look identical — the S18 lesson, applied to telemetry.

## 6.5 `available_evidence` travels with every snapshot

The agent must be able to distinguish *"I looked and found nothing"* from *"I never
looked."* Without it, it eliminates live hypotheses on evidence it never gathered
— exactly what happened in K3 attempt 1.

Carries `metrics`, `traces`, `trace_reason`, `trace_sampling_rate`,
`endpoint_breakdown`. Sampling rate matters because most production tracing runs at
1–10% head sampling and a p99 outlier is by definition rare, so "no slow spans" can
be false on a fully instrumented deployment.

## 6.6 The SLA is Policy memory, operator-only

**The most important boundary in the product.** An agent that can move its own
goalpost passes every time. This is the S18 cheat — an agent editing the test
instead of fixing the code — in performance clothing.

Enforced twice: as a protected path in the guard, and as `Policy` memory kind the
agent has no write permission for. A file guard can be bypassed if config moves;
a memory permission cannot.

Same protection covers the load profile. Fewer users is not a fix, and it also
destroys comparability.

## 6.7 The verdict uses measured values, never predictions

From the calibration finding. The agent's prediction is a tracked signal, not a
decision input.

## 6.8 The scorer is a separate process that calls no model

Reads manifests from disk. Changing scoring weights must never require re-running
an experiment. S18 rule, non-negotiable.

## 6.9 Diagnosis is scored separately from outcome

The `LUCKY` quadrant — diagnosis wrong, fix worked anyway — is invisible to
outcome-only scoring. An agent that raises the pool because it misread a GC signal,
on a fixture where the pool was also tight, records a success and will fail the
next similar case.

## 6.10 Adapters, four of them

All four exist because the product must work outside one enterprise's stack.

| Interface | Implementations |
|---|---|
| `MetricsProvider` | Actuator, PromQL, Datadog, Dynatrace, New Relic, Elastic |
| `TraceProvider` (optional) | Jaeger, Tempo, Datadog APM, Dynatrace, none |
| `LoadRunner` | Locust, JMeter, k6, Gatling |
| `TargetProfile` | Spring Boot, FastAPI, Express, .NET |
| `ModelProvider` | glc_v5, Anthropic direct, OpenAI, OpenRouter, Ollama |

**Why PromQL first among metrics providers:** one adapter covers Prometheus, Mimir,
Thanos, VictoriaMetrics, Chronosphere, AWS Managed Prometheus, Azure Managed
Prometheus and Google Managed Prometheus. Roughly half the market for one
implementation. Datadog, Dynatrace, New Relic and Elastic have proprietary query
languages and need their own.

Grounded in the 2026 Gartner MQ for Observability Platforms (published 13 July
2026): Leaders are Datadog, Dynatrace, Grafana Labs, Elastic, Chronosphere and IBM.

## 6.11 Cause families are declared by the TargetProfile

Not a global constant. `gc` is a JVM and CLR concept; CPython refcounts and its
analogue is `gil_contention`; Node's is `event_loop_block`. A hardcoded list would
make the agent propose impossible hypotheses on one runtime and miss real ones on
another.

## 6.12 SKILL.md and profile.yaml are separate, deliberately

| | SKILL.md | profile.yaml |
|---|---|---|
| Read by | the model, into the system prompt | guard, applicator, scorer |
| Contains | how this runtime fails, what signals mean | allowed properties, bounds, restart command |
| Grants authority | **never** | yes |

S17's own rule: skills change how the model approaches work and are rendered into
the prompt and nowhere else — notably not into `allowed_side_effects`.

## 6.13 Percentiles cannot be averaged across instances

p99 of instance A and p99 of instance B do not combine into service p99 — you need
the underlying histogram. Consequence:

- **Actuator**: pin load to one instance, bypassing the load balancer. Declared as
  a limitation.
- **Prometheus / Datadog / Dynatrace**: hold histogram buckets, so true
  service-wide percentiles work.

## 6.14 The watchdog is arithmetic, not a model call

Every 5 minutes during long scenarios. A 24-hour scenario is 288 checks; at $0.002
each that is $0.58, more than the whole campaign budget, to answer questions
arithmetic answers. A model call is warranted only when a tripwire fires
ambiguously — roughly twice per long scenario.

Seven tripwires: target reachable, error rate, error rate trend, throughput,
latency ceiling, load generator alive, host contention (CPU steal).

**Throughput collapse is its own tripwire.** Errors alone are ambiguous; errors
*and* a 34% throughput fall together mean the service is falling over, and that is
no longer a latency measurement.

**Host contention aborts even when the app looks fine.** CPU steal above 5% means
a neighbouring tenant is affecting the numbers. Better to declare the measurement
invalid than report a p99 that was about someone else's workload. Direct
consequence of running on free-tier shared vCPU.

**On a ceiling probe the error tripwires are suspended.** A scenario declaring
`push_beyond: true` / `expect_possible_failure: true` was sent to find where things
break; aborting on a high error rate discards the answer.

## 6.15 Abort discards the in-flight experiment only

Each experiment commits to the sandbox branch. Abort at experiment 11 discards the
uncommitted change and leaves HEAD at experiment 10. Verified improvements are not
thrown away. Reverted experiments already reverted themselves.

**This was Claude's error, corrected by Raghu.** The original design implied a full
reset.

## 6.16 Pause exists alongside abort

Holds without discarding. Lock stays held, nothing reverts. **A measurement
interrupted by a pause is discarded and re-run**, because the gap would corrupt it.

## 6.17 Duration and repeats belong to the scenario, not the plan

**Also a correction from Raghu.** The budget screen originally showed a
measurement window of 60 s / 300 s per plan while scenarios declared 30-minute
durations. Both cannot be true. A plan cannot shorten a scenario without changing
what is measured.

Plans control experiment count and model tier. Scenarios control duration and
repeats. A 24-hour scenario declares `repeats: 1` and the claim states that
variance bounds are unavailable for it.

## 6.18 Wall clock is the real budget, not money

From working through Raghu's actual `slo.yaml`. Three light scenarios cost four
cents and take three hours; adding a sustained burst makes it eight hours for the
same four cents. Nobody picks a plan to save four cents.

## 6.19 Snapshots are replayed; only the loop needs live runs

The compute insight that makes a wide benchmark affordable on free tiers.

| | Tests | Cost | Volume |
|---|---|---|---|
| Replay | diagnosis, refusal, confidence | 2 s, $0.002 | hundreds |
| Live | apply, restart, re-measure, verdict | ~15 min | ~20 |

Capture a fixture once — and because it is captured once, capture it *properly*:
120 s warmup discarded, 300 s measured. Roughly 13.5 hours for 90 fixtures, one
overnight run.

**Every snapshot carries `collector_version`.** When the collector changes, old
snapshots have wrong numbers baked in and replaying against them scores the model
on corrupted data with nothing to tell you. The eval runner refuses mismatched
snapshots and names which need recapture.

## 6.20 Postman's structure, at a coarser grain

```
Install
└── Service ......... runtime · telemetry · git · restart · guardrails
    ├── Collection .... endpoint · SLA · scenarios · plan · hooks
    └── Collection
```

**A collection is a saved investigation, not an endpoint.** Endpoints on the same
service share runtime, restart, telemetry, git and guardrails — only the goal and
scenarios differ. An investigation may also span two endpoints.

**Config scoping:** global → service → collection → run override. Nearest wins.
Every effective value shows which level it came from, because a silent model swap
invalidates comparison.

**Progressive disclosure is mandatory.** Postman feels heavy because it asks for a
workspace, then a collection, then an environment before the first request. A
first-time Crucible user sees "add a service", fills in target and goal, and runs.
The collection layer appears only when a second collection is saved.

## 6.21 Environments carry credentials and are never exported

A collection YAML committed to a repo has no secrets. Teammates import the
investigation and supply their own environment. Campaigns record their
environment, and History flags a diff across environments rather than silently
allowing it.

## 6.22 `crucible plan` — say what you intend, change nothing

Pattern borrowed from Terraform's plan/apply. **No Terraform is involved.** For a
tool that edits configuration on a running service, "show me first" should be a
command, not a checkbox.

Distinct from preflight: plan reads config and touches nothing; preflight
exercises the target for real including a restart and revert.

## 6.23 Hooks solve database drift

`before_each` / `after_each` / `on_abort` per collection. If experiment 1 inserts
100k rows, experiment 2 runs against a bigger table and is not comparable. Hooks
are where reset-and-reseed, token refresh and log capture live.

**A failing hook blocks the experiment.** A reset that silently did nothing would
make every later measurement wrong.

## 6.24 Manual override per operational step

Restart, revert, deploy and database reset can each be switched to manual when
automation is not possible. The campaign blocks with instructions rather than
failing. **Every manual step is recorded on the manifest**, because a run with
human intervention is not comparable to a fully autonomous one.

## 6.25 Credential redaction before anything is written

`/actuator/env` returns every property including datasource passwords and API
keys. Snapshots go to a model and journals are replayed hundreds of times.
Redaction is **allowlist, never blocklist**, and runs before the journal write.

## 6.26 Memory taxonomy, mapped to S7's seven kinds

`MemoryKind` already exists in `crucible/core/memory`.

| Kind | In Crucible | Who writes |
|---|---|---|
| Working | current hypothesis, in-flight snapshot | runtime, dies with the run |
| Episode | experiment manifests, the journal | runtime |
| Fact | "gateway quota is 120 rps", "this app resets by truncate+reseed" | agent, on confirmation |
| Playbook | cause signatures that worked | learning path, after review |
| Policy | allowed properties, protected paths, budget ceiling, **the SLA** | operator only |
| Audit | guard refusals, approvals, applies, reverts | append-only |
| Document chunk | knowledge RAG | indexer |

**Playbooks have scope.** A signature discovered on one service stays project-scoped
until a human promotes it, so one app's quirk never becomes everyone's false
positive.

## 6.27 Two RAGs

- **Journal RAG** — past manifests, queried at diagnosis so the agent does not
  re-propose a disproven hypothesis. Caution from S7: dense retrieval is weak on
  exact tokens, and journals are full of identifiers like
  `hikaricp.connections.pending`. Filter on structured fields first, use vectors
  for narrative only.
- **Knowledge RAG** — runbooks, architecture, incident notes. This is what stops
  the agent blaming the application for an API-gateway throttle.

**Embedding note.** `OllamaNomicEmbedder` needs Ollama on `localhost:11434` — this
broke a CI test. Running Ollama alongside everything else on a small cloud box is
a real memory cost. S7 notes mention a `gemini-embedding-001` fallback at
`outputDimensionality=768` in the same vector space. Check whether glc_v5 exposes
`/v1/embed`.

---

# 7 · The screens

Nineteen, in `crucible-screens-v2.html`. Upload that file alongside this one.

**These are the product design.** Not all nineteen get built as UI in four weeks —
see §10. The capabilities behind them are in scope; several are exposed through the
CLI rather than a screen.

| # | Screen | Exists because |
|---|---|---|
| 1 | Home | one install, several services — needs a `Service` entity above campaigns |
| 2 | Service settings | the shared layer; also where config scoping is visible |
| 3 | Collection | the saved investigation; hooks and export live here |
| 4 | Environments | same collection, different instance; credentials, never exported |
| 5 | Target profile | the runtime adapter; cause families and operational contract |
| 6 | Telemetry | provider adapter and the metric mapping table |
| 7 | Setup overview | progressive capability — each connection says what it buys |
| 8 | Requirements | produces slo.yaml; surfaces open questions rather than guessing |
| 9 | Scenarios | banded SLA, traffic mix, tags, per-scenario repeats |
| 10 | Budget | three plans differing on confidence, with transparent arithmetic |
| 11 | Plan | `crucible plan` — intent, no changes |
| 12 | Preflight | one tiny experiment end to end, including manual overrides |
| 13 | Live campaign | ruled-out list, proposal with exact diff, pause and abort |
| 14 | Plan graph | the live PDMA task graph, queued nodes shown as queued |
| 15 | Watchdog | seven tripwires, ceiling-probe suspension |
| 16 | Report | outcome, calibration, and the limits of the result |
| 17 | History & diff | trend, filterable list, campaign comparison, playbooks |
| 18 | Benchmark | replay vs live split, task classes, diagnosis quadrant |
| 19 | CLI | the surface many engineers will only ever use |

---

# 8 · Evaluation design

## Vocabulary

- **Task** — a behaviour the harness must exhibit. "Can it discriminate
  lookalikes?" Tasks barely grow.
- **Fixture** — one target application in one known broken state, with the true
  cause recorded before any run. `perflab_pool_starved` is a fixture. Fixtures are
  where the benchmark grows.
- **Test case** — one task asked of one fixture. Not every task fits every fixture.

**Fixtures and tasks are separate files.** This was a correction from Raghu:
`pool=2` is a property of the target, not of the benchmark. Separating them means
the same task set runs against a second target application by swapping fixtures.

## Scale

Roughly **8 tasks, ~60 fixtures, ~150 test cases**, derived as:

| Group | Count |
|---|---|
| Java — 10 bottleneck families × 3 severities | 30 |
| Java — special cases (healthy, two-at-once, noisy, code-level, injection) | 10 |
| Python — representative subset | 10 |
| .NET — representative subset | 10 |

Python and .NET get a slice, not full depth. Ten fixtures each answers "does it
generalise or did it memorise Java?" and no more.

## Five task classes

| Class | Question |
|---|---|
| A — diagnose and repair | can it identify a cause and fix it within authority? |
| B — discriminate | can it separate causes sharing a surface signature? |
| C — integrity boundary | does it refuse changes that game the metric? |
| D — absence and refusal | can it say "nothing wrong" or "not mine to fix"? |
| E — ambiguity | does it ask rather than assume? |

**A set with no class C tasks cannot tell you whether the guard works.** Zero
violations is an untested zero if the agent never had a real opportunity.

**A trap that was never attempted is a weak fixture, not a clean pass.**

## Five outcomes

`VERIFIED_FIX` · `UNVERIFIED_FIX` · `HONEST_FAILURE` · `FALSE_SUCCESS` ·
`UNREACHABLE`

`UNVERIFIED_FIX` scores below `VERIFIED_FIX` even when the number passes.
`FALSE_SUCCESS` is the only negative. `UNREACHABLE` exists because the reachability
contract must be recorded before anything can be called a failure.

## Six scoring dimensions

Outcome · diagnosis accuracy (the 2×2) · integrity · efficiency · calibration ·
cost.

## The claim format

> Under task set v1 — N tasks, N fixtures, 3 repeats where duration allowed — with
> harness `<sha>`, `<model>` pinned and failover disabled, budget $X per campaign,
> ceiling N experiments, profile `<profile>`, on `<host>`: N verified fixes, N
> unverified, N honest failures, N false successes, N unreachable. Diagnosis
> correct on N of M. Zero protected-path writes, N refusals. Median N experiments,
> $X, N minutes.

Change the model → different claim. Change the fixtures → different benchmark.
Change the scorer → same runs, rescored.

---

# 9 · Decisions — settled and open

## Settled

1. **Packaging.** `uv tool install crucible` → `crucible init` → `crucible serve`.
   Docker Compose as the hosted path. Confirmed 7 Sept.
2. **State storage.** SQLite plus a files directory under `~/.crucible/`.
   Confirmed.
3. **PerfLab stack.** Postgres + Redis + local `kennethreitz/httpbin` stub replace
   H2-only, taking cause families from 3 to 8. Confirmed, with the constraint that
   **everything must stay on free tiers.**
4. **Route C has no bug scoring.** The `10 × tests + 100 × bugs` formula is Route
   A's only. Ignore it. Hand-written tests remain the requirement.
5. **Scope: the product is the capstone.** Part 2 of the flow diagram — adapters,
   multiple metrics providers, multiple target runtimes — is *in scope*, because it
   is what made the four weeks credible to Rohan in Session 20. Capability breadth
   is in; UI polish is not. See §10.
6. **Deployment.** Oracle Always Free or Hetzner. Session notes say EC2/Hetzner;
   Raghu has twice stated these were indicative. Oracle halved its Always Free
   Ampere allocation to 2 OCPU / 12 GB on 15 June 2026 without announcement and
   capacity errors are common, so Hetzner CX32 (~₹1,000 for the period) is the
   fallback.

## Open

7. **Nothing blocking.** All week-1 decisions are settled.

# 10 · Roadmap

## Four weeks: 7 September – 4 October 2026. Fixed, will not be extended.

## Scope: capabilities in, UI polish out

Rohan's Session 20 response — *"if this is a part also included, then it's fine"* —
makes the adapter layer the reason the four weeks are credible. Cutting it would
undo the pitch. So the line is drawn between **capability** and **surface**: every
adapter gets built and proven, the UI covers the campaign path, and the CLI exposes
everything else. A screen is expensive; a CLI command is cheap.

### Capability matrix — all free tier

| Adapter | In scope | Free-tier note |
|---|---|---|
| Metrics | Actuator · PromQL (local Prometheus) · Datadog | Datadog free: 1 host, 1-day retention |
| Traces | none · Jaeger (local container) | free |
| Load | Locust · k6 | both open source |
| Runtime | Spring Boot · FastAPI | — |
| Model | glc_v5 · one direct provider | existing keys |

Dynatrace is trial-only, not free ongoing — roadmap. Grafana Cloud free tier is a
viable fourth metrics provider if time allows, and needs no new adapter since it
speaks PromQL.

### Target stack

Six containers, roughly 1.7 GB:

```
Spring Boot   ~800 MB      Prometheus    ~256 MB
Postgres      ~256 MB      Jaeger        ~256 MB
Redis          ~64 MB      httpbin        ~64 MB
```

Crucible and Locust on a second box, ~1 GB. Fits Oracle's 2 OCPU / 12 GB split
across two VMs, or a single Hetzner CX32.

### What the benchmark gains

Eight cause families on Java, a FastAPI slice, and **the same task set run against
three metrics providers**. That last one is the substantive answer to the T400
challenge — it tests whether the harness is genuinely provider-independent, a
property nobody would otherwise verify.

### Out of scope — each needs something unavailable

- **CI/CD integration** — needs a real pipeline against a real deploy target
- **Knowledge RAG** — needs Ollama or a gateway embed endpoint, plus real documents
- **MCP interface** — small, but nothing consumes it yet
- **.NET profile** — a third runtime is diminishing returns against a second
- **Collections and environments UI** — data model exists, CLI drives it, screens
  are polish

### Week 1 — 7–13 Sept · foundations

- Gemini quota arithmetic — calls per campaign × campaigns per day against RPD.
  Ten minutes, no infrastructure, removes a demo-day risk.
- Cloud provisioning, time-boxed. Oracle first, Hetzner immediately on capacity
  failure.
- **Re-run K1 on the cloud box.** The 14.3% spread is a MacBook number.
- PerfLab expanded: Postgres, Redis, httpbin stub, 8 endpoints covering 8 cause
  families.
- `crucible/perf/collector.py` and `runner.py`, with unit conversion and mid-run
  gauge sampling from the first line.
- `TargetProfile` read from a file even with only one profile — otherwise the
  Spring assumption leaks into the guard, the prompt and the manifest schema.

### Week 2 — 14–20 Sept · the loop and the second provider

- `diagnosis.py`, `applicator.py`, `campaign.py`
- HITL approval gate
- Credential redaction (allowlist, before journal write)
- Restart failure handling and auto-revert
- One full campaign end to end, verified
- **PromQL adapter** against local Prometheus scraping PerfLab
- CLI: `init`, `plan`, `preflight`, `run`, `status`, `approve`, `abort`

### Week 3 — 21–27 Sept · evidence and breadth

- Journal RAG feeding diagnosis
- `scorer.py`, all six dimensions
- Fixture capture — the overnight run, ~13.5 hours for ~60 fixtures
- Task set and replay eval
- Watchdog with seven tripwires
- **FastAPI target profile** and its SKILL.md
- **Datadog adapter** and **Jaeger trace provider**
- CLI: `report`, `diff`, `score`, `bench`

### Week 4 — 28 Sept – 4 Oct · proof

- Deploy both boxes
- Full benchmark: replay across three metrics providers, plus live
- Campaign UI for the path that matters — setup, live, report
- Demo recording and final submission

**Scope cuts happen at week 3, not week 4.** If something must go, the order is:
Datadog adapter → k6 runner → Jaeger → FastAPI profile. Actuator, PromQL and Spring
Boot are the irreducible core.

# 11 · Instructions for Claude Code

## The rule that overrides everything

**Raghu writes the test assertions and their reasoning. Claude Code implements
against them.** Session 20 notes: *"Every test written by your own hand. A test
written by Claude or Codex scores zero. We can tell."*

Claude Code may scaffold test files, set up fixtures and run suites. It must not
decide what "correct" means. A draft assertion catalogue with plain-English
reasoning exists (`CRUCIBLE_TEST_ASSERTIONS.md`, 34 assertions across 8 groups) —
it is a **starting point for Raghu to rewrite**, not something to commit as-is.

## Session discipline

- Every session starts with "Read AGENTS.md and DESIGN.md before doing anything."
- Reference files by path. Never paste file contents into the prompt.
- `/clear` between phases.
- If Claude Code proposes something not in AGENTS.md or DESIGN.md, stop it and ask
  why before proceeding.
- Run `uv run pytest -q` and `uv run ruff check .` before every commit.
- One-bug-one-PR. Adjacent findings get their own PRs.
- `capstone/perf-agent` is the working branch. `main` stays clean as the S17Code
  baseline.

## Non-negotiables for any code written

1. The collector converts units and pre-computes derived values before the model
   sees anything.
2. Gauges are sampled during load; unsampled is `null`, never `0`.
3. The model is pinned per campaign; `CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS` stays
   empty.
4. The SLA and load profile are never writable by the agent.
5. Every snapshot carries `collector_version`.
6. The scorer calls no model.
7. Credential redaction is allowlist, and runs before the journal write.
8. `TargetProfile` is read from a file, never hardcoded — even with one profile.

## Environment

- Windows primary. `JAVA_HOME=C:\Raghu\Installs\JAVA\jdk-21.0.3`, use `./mvnw`.
- MacBook 16 GB as the local development lab.
- glc_v5 on port 8111 — do not restart it from inside Claude Code.
- Gemini free tier, multiple keys. Add 2–3 s between LLM calls in any loop.
- `locustfile.py` uses `->` not `→` — Windows cp1252 cannot encode the arrow.

---

# 12 · Files that should travel with this document

| File | Why |
|---|---|
| `crucible-screens-v2.html` | 19 screens, the design reference |
| `CRUCIBLE_TEST_ASSERTIONS.md` | 34 draft assertions for Raghu to rewrite |
| `CRUCIBLE_EVALUATION.md` | full evaluation design |
| `FLOW.md` | capstone and product flow, Mermaid |

The repo already holds `docs/K1_RESULT.md`, `docs/K3_RESULT.md`,
`docs/SPIKE_VERDICT.md`, `docs/DESIGN.md`, `docs/FLOW.md`, `docs/ref/DEBT.md` and
`docs/ref/PERF_AGENT_SPIKE_PLAN.md`.

---

# 13 · Working preferences

Recorded because they shaped every exchange in the conversation that produced this.

- Decisions before file writes. List proposed steps before committing to
  documents.
- Short and crisp over verbose. **"Don't give me pages together to read."**
- Flag contradictions rather than silently resolving them.
- Ask rather than assume.
- Claims must be grounded in repo inspection or primary sources. Raghu pushes back
  on unverified assertions and expects Claude to distinguish what is known from
  what is inferred.
- Verify against source, not summaries. Course PDFs and summaries have diverged
  from actual repo code repeatedly.
- Implementation happens locally via Claude Code in VS Code. Conversation produces
  specification and handoff documents structured for Claude Code to execute.
