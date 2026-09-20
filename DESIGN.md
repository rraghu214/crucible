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

**3.2 No error-driven failover; budget-driven downgrade is permitted, but
never silently.** Two different mechanisms can change which model answers, and
they are treated differently.

**Error-driven cross-provider failover is disabled, always.** `glc_v5` supports
fallback providers on 429/502/503; Crucible never uses them. Verified in the
gateway's own source on 11 September 2026: when a request names a provider,
`candidates()` expands to that provider's own key pool only (`gemini` becomes
`gemini_1`..`gemini_5`, all the same model), `auto_route` is skipped, and tier
escalation cannot fire because `tier` stays `None`. Crucible names both a
provider and a model on every request, so two independent mechanisms hold it in
place. Failover across the five Gemini keys is key rotation, not model change.

**Budget-driven downgrade is allowed.** The tier ladder is deliberately
cross-model (`config/tiers.yaml`), and under budget pressure the controller
walks it down — Gemini to Groq — rather than refusing outright. The free-tier
constraint (§16) leaves no cheaper rung of the *same* model to fall back to, so
the alternative to changing model is stopping the campaign, and a campaign that
completes on a weaker model is worth more than one that halts.

**What that costs, and the control that pays for it.** The reason the original
rule existed still holds: a campaign whose experiments were diagnosed by
different models is not internally comparable. Permitting the downgrade
therefore requires that it can never happen *invisibly*:

- every experiment records the provider and model that actually served it, taken
  from the gateway's response rather than from what was requested;
- the report and the scorer flag a campaign whose experiments span models,
  instead of comparing them as though they were alike.

This is the same trade the rest of the design makes everywhere else: prevention
where it is cheap, and where it is not, measurement plus disclosure. A result
that says which model produced it is honest; one that quietly averages two is
not (principles 1 and 2).

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

**4.8 An unreadable metric is declared, never guessed and never dropped.**
Every derived field carries its unit, and a test enforces that at authoring time.
But two different things can go wrong at runtime and they do not get the same
treatment:

- A field *the collector produces* without a unit is a **code defect**. No human
  can fix it at run time and asking would be theatre. It fails the suite.
- A **metric the provider offers that we have no conversion for** is a data
  condition, and a real one: every new provider and every runtime upgrade brings
  them. Guessing the unit reintroduces the K3 failure exactly — reading seconds
  as milliseconds is how a saturated pool looked healthy. Dropping it silently is
  worse, because the agent then reasons from an incomplete picture it cannot see
  the edges of.

So an unknown metric is **declared**. It is carried in the snapshot with its raw
value and an explicit `unit: unknown`, excluded from every derived calculation,
and listed in `available_evidence` so the agent knows there was something it
could not read. That is principle 2 — the agent knows what it cannot see —
applied to the collector rather than to the tracing backend.

**Where an operator is present, ask.** The unit is a one-line answer a human
almost always knows, and the answer belongs in the `TargetProfile`, not in
memory: it is part of the runtime's contract, so the next campaign against that
runtime inherits it. Unattended runs never block on this — they declare and
continue, because a campaign that halts at 3am over one unreadable gauge is worth
less than one that finishes and says which gauge it could not read.

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
- **Host contention aborts even when the app looks fine.** CPU steal means a
  neighbouring tenant is affecting the numbers. Better to declare the
  measurement invalid than report a p99 that was about someone else's
  workload — a direct consequence of running on free-tier shared vCPU.

  **The threshold is a per-environment config value, not a constant.** Default
  **5%**; **10% on Oracle free tier**. Measured on the Oracle Ashburn box on
  12 September 2026 (`docs/K1_CLOUD_RESULT.md` §4.4): steal is 0–1% at idle and
  a steady **4–6% under load**. A fixed 5% would therefore have aborted most of
  a K1 run that passed every stability criterion it was judged against — the
  tripwire would have rejected a measurement that was demonstrably sound.

  This is the same lesson as the noise threshold it sits beside: a number
  measured on one machine is a property of that machine. Both are calibrated
  per environment, and both are recorded with the campaign so a later reader
  knows which numbers a verdict was judged against.

  **Observed steal is recorded on every manifest whether or not it trips.**
  A run that stayed under the threshold is not the same claim as a run where
  nobody looked, and the margin matters: 4% under a 5% limit and 4% under a 10%
  limit are different levels of confidence in the same number.
- **On a ceiling probe, the error tripwires are suspended.** A scenario
  declaring `push_beyond: true` / `expect_possible_failure: true` was sent
  to find where things break; aborting on a high error rate discards the
  answer.

## 7 · Experiment control

- **Abort discards the in-flight experiment only.** Each experiment commits
  to the sandbox branch. Aborting experiment 11 discards the uncommitted
  change and leaves HEAD at experiment 10 — verified improvements are not
  thrown away, and reverted experiments have already reverted themselves.
  **Once a change has been pushed and deployed, abort is no longer purely
  local**: the box is running experiment 11 even though HEAD is back at 10,
  so abort must also redeploy the last good commit. See §19.9.
- **Pause holds without discarding**, alongside abort — this is the
  mechanism for exactly the "24-hour run, something unrelated breaks
  mid-way" case: pausing stops the load generator and freezes the
  scenario's elapsed-time clock; the lock stays held and nothing already
  measured reverts. You fix whatever's wrong, then resume, and the scenario
  continues from where it paused rather than restarting. Only the single
  measurement window that was actively in flight at the moment of pause is
  discarded and re-run — a gap inside that one window would corrupt its
  numbers — everything measured before the pause stands as-is. A full
  24-hour re-run is never the cost of a pause.
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

**Every environment declares its kind, and production is refused.** Crucible
edits configuration on a running service and restarts it; the entire safety
argument in §19 assumes the target is pre-prod. That assumption must be
stated by the environment and enforced by the campaign, not held as a
convention in someone's head — a campaign refuses to start against an
environment declared `production`.

**Credentials are held in a form that cannot become a printable string.**
Snapshots reach a model and journals are replayed hundreds of times (§12), so
an HTTPS token embedded in a remote URL is one careless log line from
disclosure. An SSH deploy key referenced by path is not: the path is safe to
log, the key never enters argv or the environment. See §19.8.

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

**Embedding.** `OllamaNomicEmbedder` needs Ollama on `localhost:11434`, which
is a real memory cost on a small cloud box and has already broken one CI test.
`glc_v5` does expose `/v1/embed` and `/v1/embedders` (verified against the
running gateway, 11 September 2026), so the gateway is the first choice;
`gemini-embedding-001` at `outputDimensionality=768` is the fallback, and any
free provider of a comparable model is acceptable in its place.

**Correction — matching dimensionality is not the same vector space.** An
earlier draft of this note called the 768-dimension fallback "a candidate
fallback in the same vector space". That was wrong. Two different models at
768 dimensions produce vectors that are simply incomparable, and querying one
corpus with the other returns nearest neighbours that are noise wearing the
shape of an answer — no error, no warning, just quietly wrong retrieval, in
the component whose whole job is to stop the agent re-proposing a disproven
hypothesis. Changing embedder means **re-indexing** the corpus, never querying
across the two.

**The embedder is pinned per campaign, exactly as the chat model is (§3.2),
and every stored vector carries its `embedder_id`** — provider, model,
dimensionality and normalisation. Retrieval against a different `embedder_id`
refuses and names what needs re-indexing rather than silently returning wrong
neighbours. This is the `collector_version` rule (§7) applied to memory: a
corpus embedded under one model is exactly as stale to another as a snapshot
is to a changed collector.

`outputDimensionality` is a *request parameter*, so one model id can produce
incompatible vectors at different settings — `gemini-embedding-001` at 768 and
at 3072 are both truthfully "gemini-embedding-001". A startup canary that
embeds a fixed sentence and compares it against a stored reference vector for
that `embedder_id` catches this in a single call.

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
re-run K1 on the cloud box (the 14.3% spread came from the local Windows
dev machine, with Locust co-located on it and H2 rather than Postgres behind
the target); PerfLab
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

**The model gateway is hosted, and on neither box.** `glc_v5` runs at
https://glc-v5-rraghu214.onrender.com rather than on a developer's machine or on
Box A/Box B. Three reasons, in order of how much they cost to learn:

- A gateway on a laptop makes every campaign depend on that laptop staying
  awake, online, and tunnelled. This is not theoretical: on 21 September 2026 an
  end-to-end run measured its baseline, then the reverse SSH tunnel dropped and
  the campaign refused to continue — correctly, since a gateway that is down and
  a model that declined are different facts, but a run was lost.
- §16 puts fixture capture in week 3 as an **overnight run**. An overnight run
  cannot have a laptop in its dependency chain.
- Not Box A, which is the target and must share its CPU with nothing (§1). Not
  Box B either, if it can be avoided: Box B runs the load generator, and a model
  call during a scenario would contend with it on a single OCPU.

Crucible's *code* default stays `127.0.0.1:8111`. A tool should not ship pointing
at somebody else's gateway; `GLC_BASE_URL` is deployment configuration.

Two consequences of the free tier are accepted rather than solved. The instance
spins down when idle, so the first call of a campaign pays a cold start — charged
to wall clock (§7), never to a measured window. And its filesystem is ephemeral,
so the gateway's own cost history does not survive a redeploy. Nothing depends on
that: Crucible prices each call from the usage returned **in the response** and
records it on the manifest, and §4.6 already requires the scorer to read
manifests from disk rather than query a service.

## 19 · Deploy and the push boundary

Crucible's sandbox is not where performance is measured. The agent edits
configuration in its own workspace; the **target** runs on a separate pre-prod
box (Oracle Cloud or Hetzner, §18). A change reaches that box by being pushed
to a dedicated branch and deployed from it. Without that there is no
autonomous loop at all — which is why `git push` cannot simply be forbidden,
and why it cannot simply be allowed either.

**19.1 Pre-prod is a precondition, not a convention.** Every rule below
assumes the target is a pre-prod environment that can be broken and restored.
Crucible is never pointed at production. The environment declares its kind
(§8) and a campaign refuses to start against one declared `production`. If
this rule is ever relaxed, none of the remaining guardrails are sufficient on
their own. *(Candidate for promotion to an AGENTS.md non-negotiable.)*

**19.1a Crucible writes to one branch and to no other.** The agent applies and
pushes only to a dedicated sandbox branch (`deploy.branch`, e.g.
`perftest_sandbox`). The branch the operator works from (`deploy.base_branch`) is
**read once**, to create the sandbox branch when it does not yet exist, and is
never written to. A profile naming a protected branch — `main`, `master`,
`develop`, `trunk`, `release/*`, `hotfix/*` — or naming the same branch for both
is refused when the profile is **loaded**, so a bad configuration fails during
`crucible plan` rather than eight minutes into a campaign.

The check belongs to `DeployTarget`, not to any one deployer. A guardrail living
inside `GitPushDeployer` is one that the next adapter — Jenkins, Argo, anything —
gets written without, by someone reading the interface and not the history. Every
adapter takes a `DeployTarget`, so every adapter inherits the rule.

This does not replace §19.8's branch protection on the remote, which remains the
real control; it catches the mistake earlier and more cheaply, on a machine we
own, before it reaches a server somebody else configures.

**19.1b The workspace is the TARGET's repository, not Crucible's.** The agent
edits configuration in a checkout of the application under test. That repository
belongs to whoever owns the service; Crucible is a tool they install. The seam is
`Applicator.workspace`, exposed as `--workspace`, and `profile.yaml`'s
`config_file` is relative to it.

PerfLab lived at `perf-lab/` inside this repository until 21 September 2026 and
now has its own: **https://github.com/rraghu214/perf-lab**. The split was not
tidiness. While they were one repo:

- the deploy branch descended from Crucible's working branch, so its tree carried
  Crucible's own source and Box A received a copy of the tool that was testing
  it — inert, since the hook built one subdirectory, but wrong-shaped;
- a reader could reasonably mistake the nesting for the architecture;
- and two copies of the target would have drifted apart the moment anyone edited
  the wrong one, which is the failure `AGENTS.md` already records for
  `CRUCIBLE_TEST_ASSERTIONS.md`.

Now the deploy branch contains the target and nothing else, which is what every
real deployment looks like.

What the split did NOT change is the content of an experiment's commit. The
applicator stages one path explicitly — `git add -- <config_file>` and
`git commit -- <config_file>` — so unrelated work in the tree never rode along
with an experiment even when the repos were joined. Verified on the local
rehearsal: each experiment commit reads `1 file changed`.

**19.2 The refspec is configuration, never model output.** Deploy is its own
capability, with remote and branch pinned by `profile.yaml`
(`deploy.remote`, `deploy.branch`, e.g. `perftest_sandbox`). The model decides
*whether* to deploy; it never decides *where*. `git push` stays out of
`GIT_SUBCOMMANDS`, because that list governs argv the **model composes** — and
the risk was never that push exists. Pushing a commit to a sandbox branch on
pre-prod is reversible. The risk is a model composing `HEAD:main` or
`--force`, which no argv-parsing allowlist reliably catches.

**19.3 Force-push is refused, always.** Reverting means deploying an earlier
commit, never rewriting history. A force-push would destroy the experiment
history that the journal and every manifest depend on, making prior results
unreproducible — the same class of loss as editing a manifest after the fact
(§6.1 of the assertions).

**19.4 The human gate is on the change, not on the transport.** The operator
already approves the proposed configuration change. Deploying that approved
change is a mechanical consequence, not a second decision, and a second gate
would cost autonomy while adding no safety. First contact with an environment
is covered by **preflight** (§9), which exercises deploy, restart and revert
once, end to end, before any campaign relies on them.

**19.5 Deploy automation is declared, never guessed.** Where the operator has
wired a pipeline — Jenkins, GitHub Actions, or anything equivalent — deploy is
autonomous. Where they declare it manual, the campaign blocks with
instructions and records the manual step on the manifest (§11). The agent
never infers which mode it is in; an agent that guessed wrong would either
stall a working pipeline or silently skip a deploy that never happened.

**19.6 No measurement begins until the change is proved to be in force.**
Measuring before a change lands attributes the *old* configuration's numbers to
the *new* change — a silent error of exactly the K3 class (§4), and one that no
later check would catch, because the resulting number is perfectly plausible.
Deploy latency is charged to wall clock (§7), never to the measured window.

This is not hypothetical. During the K1 cloud run a full set of pool=20
measurements was taken against a JVM still running pool=2; it was caught only
because a pool of 20 cannot cap `active` connections at 2, and every other figure
looked ordinary.

**The proof must come from the target, never from Crucible's own record.**
Crucible's memory can only say what it *did* — "I pushed sha X". The failure
lives entirely in the gap between that and what the target is *running*, and the
gap is real: a build can fail and leave the previous artifact, a restart can
silently not take, an old process can still hold the port. In the K1 case the
operator believed pool=20 was deployed and any notes would have agreed; the box
disagreed.

**What counts as proof is a ladder, because most applications do not publish a
commit.** Requiring one would make this rule unimplementable outside a testbed
we control. Each runtime declares how its changes can be observed, in
`profile.yaml`:

| Rung | Proves | Availability |
|---|---|---|
| Read the changed property back as a metric | the change is in force | anywhere with Prometheus / Datadog / Actuator |
| Read it from a config endpoint | the change is in force | Actuator `/env`, or an app's own endpoint |
| Commit sha | the right build is running | only where the app bakes one in |
| Process uptime / start time | *a* restart happened, not what changed | almost everywhere |
| Nothing available | — | record the experiment **unverified** and say so |

Reading the property back is stronger than a sha for the question actually being
asked: a sha proves the right build landed, the read-back proves *this change*
took effect. It is also the rung most targets can reach, so it is the default —
including for PerfLab, which could use its sha but should exercise the path
every other user will take.

**The deployer's own report corroborates; it does not verify.** A post-receive
hook or a CI job knows which sha it checked out and built, and that is worth
recording: it distinguishes "the build failed" from "the build succeeded but the
restart did not take" from "it is running but will not say what it is" — three
conditions an operator would respond to differently, and which Crucible otherwise
cannot tell apart. So the deployer's report is carried on the manifest next to
what the target says.

It is not the gate, for the same reason Crucible's own commit sha is not. A build
system reporting its own success is evidence about the build, produced by the
thing doing the building. It can prove an artifact exists on disk; it cannot
prove the process currently answering requests is that artifact — a stale process
still holding the port looks identical from the build's point of view. Only the
target can settle that.

**The bottom rung is the honest one, and it is a stated limitation.** Where
nothing can observe the change — no metric carrying the property, no config
endpoint, no commit, no usable uptime — the experiment is recorded as
**unverified** and the report says so (principle 2: the agent knows what it
cannot see). Such a result is real but weaker: it rests on the deploy pipeline
having done what it said. Crucible does not refuse to run in that case, because
refusing would exclude a large class of real applications; it refuses to *present
the result as verified*. A campaign made entirely of unverified experiments is a
campaign whose conclusions rest on a deploy tool's word, and the report must let
a reader see that rather than discovering it later.

**19.7 Every experiment records the commit it ran against.** The deployed sha
is carried per experiment in Episode memory (§13), so "which change produced
this number" is answered from the journal rather than reconstructed by
inference. This is also what makes revert mechanical rather than a judgement
call: the last good commit is a recorded fact, not a guess.

**19.8 Credentials never become a printable string.** Prefer an SSH deploy key
referenced by path over an HTTPS token embedded in a remote URL — a key path
is safe to log, a token is not, and journals are replayed hundreds of times
(§12). The key is repo-scoped and write-only; branch protection on the remote
keeps it off `main` and off feature branches even if §19.2 fails. Where CI
performs the deploy, Crucible holds no credential for the box at all: it can
write to a sandbox branch, and only the pipeline can touch the environment.
Any `.env` holds non-secret pointers only — remote name, branch, key path.

**19.9 Abort is no longer purely local.** §7's abort discards the in-flight
experiment and leaves HEAD at the last good one. Once a change has been pushed
and deployed, the box is running experiment 11 even though HEAD is back at 10,
so abort must **also redeploy the last good commit**. An abort that stops at
the local revert leaves the environment in a state no manifest describes,
which is worse than not aborting: the next campaign would measure it and
attribute the result to something else.

**19.10 One campaign per deploy branch at a time.** Two campaigns pushing to
the same branch would interleave commits and invalidate both. The experiment
lock (§7) extends to the deploy branch, not just the workspace.

## 20 · Ceiling discovery

**The question this answers:** "my service meets its SLA at 100 requests a
minute — what is it actually capable of?" A campaign that only ever proves the
SLA is met tells an engineer nothing about the margin they are operating on.

This is not speculative. The 13 September pool=20 runs
(`docs/K1_CLOUD_RESULT.md` §6) found that at 50 users the target has **no
bottleneck left to find**: `pending` is flat zero, acquire time is 0.01 ms, CPU
is 25% utilised, and throughput is capped at ~200 rps by the load profile's own
think time rather than by anything in the service. Raising the pool cannot move
it. The only way to learn what that service can do is to raise the load.

**20.1 The intent is declared by a human, never inferred.** A ceiling probe
deliberately drives a service until it breaks, so it is started by an explicit
operator choice — a flag on the campaign, surfaced in the UI and the CLI — and
never by the agent concluding on its own that pushing harder would be
informative.

This is not ceremony. `DESIGN.md` §4.4 says the load profile is **never writable
by the agent**, because fewer users is not a fix and changing the profile
destroys comparability. Ceiling discovery *is* a change to the load profile. The
flag is what makes it legitimate: the operator authorises the escalation and its
bounds, and the agent escalates only inside them. Without the flag the rule would
have to be weakened, and §4.4 is enforced twice precisely so that it cannot be.

**20.2 The operator sets the bounds, not just the intent.** Maximum users,
maximum duration, and the step size. An unbounded "keep going until something
breaks" on a shared box is how a neighbouring tenant's measurements get ruined
along with ours.

**20.3 Escalate in steps, and hold each step long enough to measure.** A step
that is not held past warmup measures JIT, not capacity — the cold-versus-warm
gap on the Oracle box was ~50%, against a 2.08% noise threshold. Each step is a
measured run in its own right: same warmup discard, same mid-run gauge sampling.

**20.4 The result is a knee, reported as a pair.** "The last level that met the
SLA" and "the first level that did not", with both measurements attached. A
single number would imply a precision the method does not have — the true
ceiling lies between two tested points, and the step size bounds how tightly.

**20.5 Stop conditions are the watchdog's, plus the SLA.** Error rate,
throughput collapse, host contention and load-generator health already exist
(§6). Ceiling discovery adds one: the SLA itself. The probe stops at the first
step that misses it, having found what it came for.

Note the asymmetry with §6's existing ceiling probe: a scenario declaring
`push_beyond: true` **suspends** the error tripwires, because aborting on errors
discards the answer it was sent to find. Ceiling *discovery* does the opposite —
it stops at the first failure, because that failure is the answer. Both are
legitimate; they are different questions, and a campaign must say which one it
is asking.

**20.6 A ceiling result is not a verdict on a change.** It characterises the
service as it stands. It must never be presented alongside experiment
comparisons as though it were one, because nothing was changed and nothing was
verified — the same separation §4.7 draws between diagnosis and outcome.
