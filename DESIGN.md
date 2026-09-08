# DESIGN.md — Crucible

Every decision here exists for a stated reason — most traced to something that
happened during the K1/K3 feasibility spike, or to a rule inherited from the
EAG harness (S7/S17/S18). Nothing is arbitrary. If you're changing behaviour
covered here, the reason needs to still hold, or this doc needs to change with
it — never diverge from it silently.

## 1 · Vision, mission, principles

**Vision** — Any engineer who owns a service can find out why it is slow and
prove their fix worked, whatever their stack and whatever they spend on
observability.

**Mission** — Build an autonomous performance engineer: it runs its own
experiments, diagnoses from the evidence it can actually reach, proposes
bounded changes under human control, and proves every claim with a
measurement rather than an assertion.

**Five principles**

1. Nothing is claimed that was not measured. Verified and unverified are
   different outcomes and must never look identical in a results table.
2. The agent knows what it cannot see. Missing evidence is declared, never
   assumed absent.
3. The agent never grades itself. It cannot edit the SLA, the load profile,
   or the tests.
4. A human holds every irreversible action.
5. Understanding is scored separately from outcome. Being right by luck is
   not being right.

## 2 · Product structure

Postman's shape, coarser grain:

```
Install
└── Service ......... runtime · telemetry · git · restart · guardrails
    ├── Collection .... endpoint · SLA · scenarios · plan · hooks
    └── Collection
```

A **collection is a saved investigation, not an endpoint** — endpoints on the
same service share runtime, restart, telemetry, git and guardrails; only the
goal and scenarios differ. An investigation may span two endpoints.

**Config scoping**: global → service → collection → run override. Nearest
wins. Every effective value shows which level it came from — a silent model
swap invalidates comparison.

**Progressive disclosure is mandatory.** A first-time user adds a service,
fills in target and goal, and runs. The collection layer appears only when a
second collection is saved — don't front-load workspace/environment setup
before the first request the way Postman does.

## 3 · Foundational choices

**3.1 Built on S17Code, not from scratch.** The course's Route C forbids
third-party harnesses (LangChain, CrewAI, AutoGen); the EAG harness itself is
explicitly permitted. S17Code already carries the guarded coding loop,
economics, event engine, HITL, telemetry, FAISS memory and the gateway seam —
rebuilding those would cost a week of four.

**3.2 Model pinned per campaign, no failover.** `glc_v5` supports fallback
providers on 429/502/503; **this is disabled for Crucible.**
Cross-provider failover resolves to a different model family mid-campaign,
which silently invalidates every experiment-to-experiment comparison.

## 4 · Measurement integrity

These four decisions exist because of two specific failures during the K3
diagnosis-quality spike (see `docs/K3_RESULT.md` for the raw runs).

**4.1 The collector converts units and pre-computes derived values.**
Micrometer reports timers in **seconds**. Handed a raw snapshot, the model
read `acquire MAX: 2.4` as 2.4 ms — a healthy wait — and ruled the connection
pool out, when the true value was 2406 ms. Raw Micrometer tuples
(`COUNT`/`TOTAL_TIME`/`MAX`) are never handed to the model; the snapshot
carries `acquire_mean_ms` with the division already done, and every field
name ends in its unit.

**4.2 Gauges are sampled during load; the peak is recorded.** `pending` reads
0 once load drains — the model saw zero waiters and crossed the pool off, when
during the run it was 43. The load runner runs a background sampler and the
snapshot carries the peak. Corollary: **an unsampled gauge is `null`, never
`0`.** A clean zero and an untested zero must not look identical.

**4.3 `available_evidence` travels with every snapshot.** The agent must be
able to distinguish "I looked and found nothing" from "I never looked" —
without it, it eliminates live hypotheses on evidence it never gathered,
exactly what happened in K3 attempt 1. Carries `metrics`, `traces`,
`trace_reason`, `trace_sampling_rate`, `endpoint_breakdown`. Sampling rate
matters: most production tracing runs at 1–10% head sampling, and a p99
outlier is by definition rare, so "no slow spans" can be false even on a
fully instrumented deployment.

**4.4 The SLA is Policy memory, operator-only.** The most important boundary
in the product — an agent that can move its own goalpost passes every time.
Enforced twice: as a protected path in the guard, and as a `Policy` memory
kind the agent has no write permission for. A file guard can be bypassed if
config moves; a memory permission cannot. The same protection covers the load
profile — fewer users is not a fix, and it destroys comparability too.

**4.5 The verdict uses measured values, never predictions.** The agent
predicted 140 ms and measured 93 ms after the K3 pool-size fix — conservative
by ~1.5×. The K1 pool=10 baseline happened to read ~150 ms; had that been
used as the "after" figure instead of a fresh measurement, the prediction
would have looked near-perfect by coincidence. **Verification always re-runs
the actual proposed value, never an adjacent one.** The agent's prediction is
a tracked signal, not a decision input.

**4.6 The scorer is a separate process that calls no model.** Reads manifests
from disk. Changing scoring weights must never require re-running an
experiment.

**4.7 Diagnosis is scored separately from outcome.** The `LUCKY` quadrant —
diagnosis wrong, fix worked anyway — is invisible to outcome-only scoring. An
agent that raises the pool size because it misread a GC signal, on a fixture
where the pool also happened to be tight, records a success and will fail the
next fixture that looks similar but isn't.

## 5 · Adapters

All four exist because the product must work outside one enterprise's stack —
this is the difference between a script and a sellable tool.

| Interface | Implementations |
|---|---|
| `MetricsProvider` | Actuator · PromQL · Datadog · Dynatrace · New Relic · Elastic |
| `TraceProvider` (optional) | Jaeger · Tempo · Datadog APM · Dynatrace · none |
| `LoadRunner` | Locust · JMeter · k6 · Gatling |
| `TargetProfile` | Spring Boot · FastAPI · Express · .NET |
| `ModelProvider` | glc_v5 · Anthropic direct · OpenAI · OpenRouter · Ollama |

**Why PromQL first among metrics providers.** One adapter covers Prometheus,
Mimir, Thanos, VictoriaMetrics, Chronosphere, AWS/Azure/Google Managed
Prometheus — roughly half the observability market for one implementation.
Datadog, Dynatrace, New Relic and Elastic have proprietary query languages
and need their own adapters. (Grounded in the 2026 Gartner MQ for
Observability Platforms, published 13 July 2026: Leaders are Datadog,
Dynatrace, Grafana Labs, Elastic, Chronosphere and IBM.)

**Cause families are declared by the `TargetProfile`, not a global constant.**
`gc` is a JVM/CLR concept; CPython's analogue is `gil_contention`; Node's is
`event_loop_block`. A hardcoded list would make the agent propose impossible
hypotheses on one runtime and miss real ones on another.

**`SKILL.md` and `profile.yaml` are separate, deliberately.**

| | `SKILL.md` | `profile.yaml` |
|---|---|---|
| Read by | the model, into the system prompt | guard, applicator, scorer |
| Contains | how this runtime fails, what signals mean | allowed properties, bounds, restart command |
| Grants authority | never | yes |

Skills change how the model approaches work and are rendered into the prompt
and nowhere else — never into `allowed_side_effects`.

**Percentiles cannot be averaged across instances.** p99 of instance A and
p99 of instance B do not combine into service p99 — you need the underlying
histogram. Consequence: **Actuator** pins load to one instance, bypassing the
load balancer, and is declared a limitation; **Prometheus / Datadog /
Dynatrace** hold histogram buckets, so true service-wide percentiles work.

## 6 · Watchdog

Every 5 minutes during long scenarios, arithmetic — not a model call. A
24-hour scenario is 288 checks; at $0.002 each that's $0.58, more than the
whole campaign budget, to answer questions arithmetic already answers. A
model call fires only when a tripwire trips ambiguously — roughly twice per
long scenario.

**Seven tripwires**: target reachable, error rate, error rate trend,
throughput, latency ceiling, load generator alive, host contention (CPU
steal).

- **Throughput collapse is its own tripwire.** Errors alone are ambiguous;
  errors *and* a 34% throughput fall together mean the service is falling
  over — no longer a latency measurement.
- **Host contention aborts even when the app looks fine.** CPU steal above
  5% means a neighbouring tenant is affecting the numbers. Better to declare
  the measurement invalid than report a p99 that was about someone else's
  workload — a direct consequence of running on free-tier shared vCPU.
- **On a ceiling probe, the error tripwires are suspended.** A scenario
  declaring `push_beyond: true` / `expect_possible_failure: true` was sent
  to find where things break; aborting on a high error rate discards the
  answer.

## 7 · Experiment control

- **Abort discards the in-flight experiment only.** Each experiment commits
  to the sandbox branch. Aborting experiment 11 discards the uncommitted
  change and leaves HEAD at experiment 10 — verified improvements are not
  thrown away, and reverted experiments have already reverted themselves.
- **Pause holds without discarding**, alongside abort. Lock stays held,
  nothing reverts. A measurement interrupted by a pause is discarded and
  re-run, because the gap would corrupt it.
- **Duration and repeats belong to the scenario, not the plan.** A plan
  cannot shorten a scenario without changing what's actually measured. Plans
  control experiment count and model tier; scenarios control duration and
  repeats. A 24-hour scenario declares `repeats: 1`, and the claim states
  variance bounds are unavailable for it.
- **Wall clock is the real budget, not money.** Three light scenarios cost
  four cents and take three hours; adding a sustained burst makes it eight
  hours for the same four cents. Nobody picks a plan to save four cents —
  budget UI should foreground time, not $.
- **Snapshots are replayed; only the loop needs live runs** — the compute
  insight that makes a wide benchmark affordable on free tiers.

  | | Tests | Cost | Volume |
  |---|---|---|---|
  | Replay | diagnosis, refusal, confidence | ~2 s, $0.002 | hundreds |
  | Live | apply, restart, re-measure, verdict | ~15 min | ~20 |

  Capture a fixture once, and because it's captured once, capture it
  *properly*: 120 s warmup discarded, 300 s measured. **Every snapshot
  carries `collector_version`** — when the collector changes, old snapshots
  have wrong numbers baked in, and the eval runner must refuse mismatched
  snapshots and name which need recapture rather than silently scoring the
  model on corrupted data.

## 8 · Environments & credentials

A collection YAML committed to a repo has no secrets. Teammates import the
investigation and supply their own environment. Campaigns record which
environment they ran against, and History flags a diff across environments
rather than silently allowing comparison across them.

## 9 · Plan vs preflight

`crucible plan` — Terraform's plan/apply pattern, no Terraform involved. For a
tool that edits configuration on a running service, "show me first" is a
command, not a checkbox. Plan reads config and touches nothing.

Preflight is distinct: it exercises the target for real, including a restart
and revert, as one tiny end-to-end experiment.

## 10 · Hooks

`before_each` / `after_each` / `on_abort`, per collection. Solve database
drift: if experiment 1 inserts 100k rows, experiment 2 runs against a bigger
table and isn't comparable. Hooks are where reset-and-reseed, token refresh
and log capture live. **A failing hook blocks the experiment** — a reset that
silently did nothing would make every later measurement wrong.

## 11 · Manual override

Restart, revert, deploy and database reset can each be switched to manual
when automation isn't possible. The campaign blocks with instructions rather
than failing. **Every manual step is recorded on the manifest** — a run with
human intervention is not comparable to a fully autonomous one.

## 12 · Credential redaction

`/actuator/env` returns every property, including datasource passwords and
API keys. Snapshots go to a model and journals get replayed hundreds of
times. Redaction is **allowlist, never blocklist**, and runs before the
journal write.

## 13 · Memory taxonomy

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

**Playbooks have scope.** A signature discovered on one service stays
project-scoped until a human promotes it — one app's quirk never becomes
everyone's false positive.

## 14 · Two RAGs

- **Journal RAG** — past manifests, queried at diagnosis so the agent doesn't
  re-propose a disproven hypothesis. Dense retrieval is weak on exact tokens,
  and journals are full of identifiers like `hikaricp.connections.pending` —
  filter on structured fields first, use vectors for narrative only.
- **Knowledge RAG** — runbooks, architecture, incident notes. What stops the
  agent blaming the application for an API-gateway throttle.

Embedding note: `OllamaNomicEmbedder` needs Ollama on `localhost:11434`,
which is a real memory cost on a small cloud box and has already broken one
CI test. Check whether `glc_v5` exposes `/v1/embed`; `gemini-embedding-001`
at `outputDimensionality=768` is a candidate fallback in the same vector
space.

## 15 · Screens → capability mapping

Nineteen screens exist as design reference in `crucible-screens-v2.html`.
**Capability is in scope for all nineteen; UI is not** — see §16. Only the
campaign path (setup → live → report) gets built as a screen; everything
else is exposed through the CLI.

| # | Screen | Exists because |
|---|---|---|
| 1 | Home | one install, several services — needs a `Service` entity above campaigns |
| 2 | Service settings | the shared layer; also where config scoping is visible |
| 3 | Collection | the saved investigation; hooks and export live here |
| 4 | Environments | same collection, different instance; credentials, never exported |
| 5 | Target profile | the runtime adapter; cause families and operational contract |
| 6 | Telemetry | provider adapter and the metric mapping table |
| 7 | Setup overview | progressive capability — each connection says what it buys |
| 8 | Requirements | produces `slo.yaml`; surfaces open questions rather than guessing |
| 9 | Scenarios | banded SLA, traffic mix, tags, per-scenario repeats |
| 10 | Budget | three plans differing on confidence, transparent arithmetic |
| 11 | Plan | `crucible plan` — intent, no changes |
| 12 | Preflight | one tiny experiment end to end, including manual overrides |
| 13 | Live campaign | ruled-out list, proposal with exact diff, pause and abort |
| 14 | Plan graph | the live PDMA task graph, queued nodes shown as queued |
| 15 | Watchdog | seven tripwires, ceiling-probe suspension |
| 16 | Report | outcome, calibration, and the limits of the result |
| 17 | History & diff | trend, filterable list, campaign comparison, playbooks |
| 18 | Benchmark | replay vs live split, task classes, diagnosis quadrant |
| 19 | CLI | the surface many engineers will only ever use |

## 16 · Roadmap

Four weeks, 7 September – 4 October 2026, fixed, will not be extended. Scope
line is **capability in, UI polish out** — every adapter gets built and
proven; the UI covers only the campaign path; the CLI exposes everything
else. A screen is expensive; a CLI command is cheap.

**Capability matrix — all free tier**

| Adapter | In scope | Free-tier note |
|---|---|---|
| Metrics | Actuator · PromQL (local Prometheus) · Datadog | Datadog free: 1 host, 1-day retention |
| Traces | none · Jaeger (local container) | free |
| Load | Locust · k6 | both open source |
| Runtime | Spring Boot · FastAPI | — |
| Model | glc_v5 · one direct provider | existing keys |

Dynatrace is trial-only, not free ongoing — roadmap, not week 1–4. Grafana
Cloud free tier is a viable fourth metrics provider if time allows; it needs
no new adapter since it speaks PromQL.

**Target stack** — six containers, roughly 1.7 GB: Spring Boot (~800 MB),
Postgres (~256 MB), Redis (~64 MB), Prometheus (~256 MB), Jaeger (~256 MB),
httpbin (~64 MB). Crucible and Locust on a second box, ~1 GB. Fits Oracle's
2 OCPU / 12 GB split across two VMs, or a single Hetzner CX32.

**Week 1 (7–13 Sept) — foundations**: Gemini quota arithmetic; cloud
provisioning (Oracle first, Hetzner immediately on capacity failure);
re-run K1 on the cloud box (the 14.3% spread is a MacBook number); PerfLab
expanded to Postgres/Redis/httpbin, 8 endpoints covering 8 cause families;
`crucible/perf/collector.py` and `runner.py` with unit conversion and
mid-run gauge sampling from the first line; `TargetProfile` read from a file
from day one.

**Week 2 (14–20 Sept) — the loop and the second provider**:
`diagnosis.py`, `applicator.py`, `campaign.py`; HITL approval gate;
credential redaction; restart failure handling and auto-revert; one full
campaign verified end to end; PromQL adapter against local Prometheus; CLI
`init`/`plan`/`preflight`/`run`/`status`/`approve`/`abort`.

**Week 3 (21–27 Sept) — evidence and breadth**: journal RAG feeding
diagnosis; `scorer.py`, all six dimensions; fixture capture (overnight run —
see the flagged fixture-count discrepancy in `EVALUATION.md`); task set and
replay eval; watchdog with seven tripwires; FastAPI target profile and its
`SKILL.md`; Datadog adapter and Jaeger trace provider; CLI
`report`/`diff`/`score`/`bench`.

**Week 4 (28 Sept – 4 Oct) — proof**: deploy both boxes; full benchmark
(replay across three metrics providers, plus live); campaign UI for setup /
live / report; demo recording and final submission.

**Scope cuts happen at week 3, not week 4.** Order: Datadog adapter → k6
runner → Jaeger → FastAPI profile. Actuator, PromQL and Spring Boot are the
irreducible core.

## 17 · Competitive position

Two commercial tools exist in this space: **Akamas** (autonomous tuning,
optimisation-focused rather than diagnosis-focused) and **Tricentis
NeoLoad** (load testing with analysis, not an agent that decides what to try
next). Neither closes the diagnose → propose → verify loop. Nothing open
source does. This is the product's actual differentiation — keep it true by
protecting §4 (measurement integrity) and §5 (adapter breadth); either one
eroding is what would make Crucible just another load-testing tool with an
LLM bolted on.

## 18 · Packaging & deployment

`uv tool install crucible` → `crucible init` → `crucible serve`. Docker
Compose as the hosted path. State storage: SQLite plus a files directory
under `~/.crucible/`. Deployment target: Hetzner CX32 (~₹1,000 for the
capstone period) or Oracle Always Free, both chosen to keep everything on
free or near-free tiers per §16 — the only ongoing spend in the whole
architecture is that one box.
