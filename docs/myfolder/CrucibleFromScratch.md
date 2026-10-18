# Crucible from scratch

This document is for someone who has forgotten everything about Crucible, or never
knew it, and needs to understand all of it: why it exists, how the machines are
wired, how a campaign moves through the code, what lands on disk, how it is
evaluated, how the UI works, what the tests protect, and what is still missing.

It describes the code on branch `capstone/perf-agent` as of **18 October 2026**.
Where it states what a function does, that was checked by reading the function.
File references are clickable from VS Code.

**How to read it.** Sections 1–3 are orientation; read them first, in order.
Section 5 (one campaign, end to end) is the heart of it. Everything after is
reference, to read when you need it.

---

## Contents

1. [What Crucible is, and why it is built this way](#1-what-crucible-is-and-why-it-is-built-this-way)
2. [The machines and how they are wired](#2-the-machines-and-how-they-are-wired)
3. [The repository](#3-the-repository)
4. [The configuration files](#4-the-configuration-files)
5. [One campaign, end to end](#5-one-campaign-end-to-end)
6. [What lands on disk](#6-what-lands-on-disk)
7. [The journal: memory across campaigns](#7-the-journal-memory-across-campaigns)
8. [The watchdog](#8-the-watchdog)
9. [Ceiling discovery](#9-ceiling-discovery)
10. [Evaluation: fixtures, tasks, replay, scoring, reports](#10-evaluation)
11. [The model gateway](#11-the-model-gateway)
12. [The command line](#12-the-command-line)
13. [The UI](#13-the-ui)
14. [The inherited S17Code harness](#14-the-inherited-s17code-harness)
15. [The tests](#15-the-tests)
16. [The rules that must not be broken, and the incident behind each](#16-the-rules-that-must-not-be-broken)
17. [Results so far](#17-results-so-far)
18. [Known gaps and debt](#18-known-gaps-and-debt)
19. [Glossary](#19-glossary)
20. [Reading order for the code](#20-reading-order-for-the-code)

---

## 1. What Crucible is, and why it is built this way

### 1.1 The problem

A service misses its latency goal (its **SLA**: say, p99 under 120 ms). Someone
guesses a cause, changes a setting, and the number moves. Nobody checks properly
whether the change caused the move, because proper checking means running the same
load before and after, on the same box, knowing how noisy the box is, and making
sure the change was actually live when the "after" was measured.

### 1.2 What Crucible does

Crucible is an **autonomous performance engineer**. One **campaign** is a loop:

```
measure a baseline under fixed load
  -> diagnose why the SLA is missed (the only step that uses a language model)
  -> propose ONE change inside the profile's bounds
  -> guard: is the change permitted at all?
  -> a human approves that exact change
  -> apply it (one line, one file, one commit) and deploy it
  -> prove the target is running it
  -> re-measure under identical load
  -> verdict against the measured noise floor: keep, or revert
  -> repeat until the SLA is met, the agent abstains, or the budget runs out
```

### 1.3 The five principles

From `DESIGN.md` §1. Almost every rule in the code exists to protect one of them.

1. **Nothing is claimed that was not measured.** Verified and unverified must never
   look identical.
2. **The agent knows what it cannot see.** Missing evidence is declared, never
   assumed absent.
3. **The agent never grades itself.** It cannot edit the SLA, the load profile, or
   the tests.
4. **A human holds every irreversible action.**
5. **Understanding is scored separately from outcome.** Right by luck is not right.

### 1.4 Where the rules came from: the feasibility spike

Before any agent code, two "kill tests" were run (`docs/K1_RESULT.md`,
`docs/K3_RESULT.md`, `docs/SPIKE_VERDICT.md`).

**K1: is the measurement stable enough?** The same load three times on the same
config. On the Windows laptop p99 varied 14.3%; re-run on the Oracle cloud box it
varied **2.08%** (`docs/K1_CLOUD_RESULT.md`). That 2.08% is the **noise floor** in
`config/slo.yaml`. Any change smaller than it is indistinguishable from doing nothing.

**K3: can a model diagnose from metrics?** The first attempt **failed**, for two
reasons that became the core of the collector:

- **Units.** Micrometer reports timers in seconds. The raw snapshot said
  `acquire MAX: 2.4`. The model read 2.4 ms (healthy) and ruled the connection pool
  out. It was 2,406 ms.
- **A drained gauge.** `pending` (requests waiting for a connection) was read after
  the load finished, so it read 0. During the run it was 43.

Fixing the **snapshot** (converted units, gauges sampled during load), not the
prompt, made the diagnosis correct. The fix was verified by re-running the exact
proposed value: p99 1300 → 93 ms. The model had predicted 140 ms, which is why a
prediction is tracked but never used as the result.

### 1.5 What it is built on

Crucible is built on **S17Code**, the EAG course's own agent harness. The course's
"Route C" forbids third-party agent frameworks (LangChain, CrewAI, AutoGen) but
permits the course harness. Everything performance-specific is new and lives in
`crucible/perf/`. The inherited parts are summarised in §14.

---

## 2. The machines and how they are wired

```
  Your laptop (Windows)                 Render (free tier)
  - development, tests                  - glc_v5 model gateway
  - SSH into both boxes                   https://glc-v5-rraghu214.onrender.com
         |                                      ^
         | SSH                                  | HTTPS: diagnosis calls only
         v                                      |
  Box B  150.136.143.227 / 10.0.0.8  ----------+
  - ~/crucible   (this repo; runs campaigns, Locust, the UI)
  - ~/perf-lab   (checkout of the TARGET repo = Crucible's workspace)
         |
         |  1. HTTP load (Locust) + metrics reads (Actuator, Prometheus)
         |  2. git push perftest <sha>:refs/heads/perftest_sandbox  (SSH deploy key)
         v
  Box A  129.213.121.108 / 10.0.0.79   (the target; pre-prod)
  - PerfLab (Spring Boot 3.3.5, Java 21) on :8080, Postgres, Redis, httpbin, Prometheus :9090
  - ~/perflab-deploy.git  bare repo; hooks/post-receive = scripts/box_a_post_receive.sh
  - ~/perflab-work        where the hook checks out and builds
  - ~/perflab.env         JVM overrides + Datadog keys (never committed)
         |
         | Micrometer push every 10 s (only when DATADOG_API_KEY is set)
         v
  Datadog (free tier: 1 host, 1-day retention)
```

**Why two boxes.** The target must share its CPU with nothing, or the measurement
is about the load generator. Box A is Oracle free tier with shared vCPUs, which is
why CPU **steal** is watched (§8).

**Why the gateway is hosted** (`DESIGN.md` §18). A gateway on the laptop made every
campaign depend on the laptop staying awake. On 21 September a run was lost when
an SSH tunnel dropped. The free Render instance sleeps when idle, so the first call
of a campaign pays a cold start of up to about a minute. Crucible warms it up
before the baseline (`Campaign.run`, `warm_up_gateway`).

**PerfLab** (https://github.com/rraghu214/perf-lab) is the target. It is a separate
repository, and has been since 21 September 2026 (`DESIGN.md` §19.1b). It has one
endpoint per cause family, so each bottleneck can be switched on by configuration.
From `locust/locustfile.py`:

| Locust tag | Endpoint | Cause family it exercises |
|---|---|---|
| fast | `/api/fast` | baseline |
| db | `/api/db` | connection_pool_exhaustion |
| async | `/api/async` | thread_pool_saturation |
| churn | `/api/churn` | gc_pressure |
| orders | `/api/orders` | inefficient_query (N+1) |
| catalog | `/api/catalog` | cache_miss |
| downstream | `/api/downstream` | downstream_latency |
| ledger | `/api/ledger` | lock_contention |
| payload | `/api/payload` | payload_serialization |
| slow | `/api/slow` | application_code |

Live campaigns currently load only `/api/db` (`build_campaign` sets the Locust tag
to `db` when the SLA's endpoint ends in `/db`).

**The deploy hook** (`scripts/box_a_post_receive.sh`, installed as Box A's
`post-receive`). When Box B pushes to `perftest_sandbox`, the hook:

1. sources `~/perflab.env`, exporting its variables (`set -a`);
2. adds the Spring `datadog` profile if `DATADOG_API_KEY` is set;
3. checks out the commit into `~/perflab-work`;
4. builds with `./mvnw -Dperflab.commit=<sha>`, so the jar knows its own commit;
5. stops the old JVM and starts the new one with `setsid nohup`, so it survives
   the SSH session;
6. waits for `/actuator/health` = UP and for `/api/version` to report the pushed sha;
7. logs each step to `~/perflab-deploy.log`.

Crucible never composes this command and never holds a credential for Box A.

---

## 3. The repository

```
crucible/                     the Python package
  perf/                       THE PRODUCT: everything performance-specific
    campaign.py               the loop (state machine), SLA, verdicts, locks, manifests
    commands.py               every CLI verb's implementation; wires real campaigns
    runner.py                 Locust load generator + mid-run gauge sampler
    collector.py              raw metrics -> the snapshot the model reads
    providers/                MetricsProvider adapters: actuator, promql, datadog; jaeger traces
    diagnosis.py              prompt, model call, parsing -> a Proposal (only model use in the loop)
    applicator.py             the guard; edit one property; commit; restart; revert
    approval.py               approval gates (file, preapproved, denying); manual steps
    deploy.py                 branch policy; git push deployer; prove the running commit
    profile.py                TargetProfile loaded from config/profiles/*.yaml
    policy.py                 the SLA as Policy memory (second lock)
    verdicts.py               IMPROVED / WORSE / INCONCLUSIVE / NOT_MEASURED / ABORTED
    journal.py                prior findings from earlier campaigns (structured, no vectors)
    watchdog.py               seven tripwires during a measurement
    ceiling.py                stepped load ramp to find the capacity knee
    hooks.py                  before_each / after_each / on_abort hooks
    fixtures.py               capture a target state as a replayable snapshot
    replay.py                 run tasks against saved fixtures (no live target)
    scorer.py                 grade campaigns and replay runs; calls no model
    report.py                 the report and the diff; calls no model
    quota.py                  free-tier rate-limit arithmetic
  ui/
    nicegui_app.py            the web UI (7 pages)
    actions.py                one argv builder per CLI command
    jobs.py                   runs CLI commands as detached processes for the UI
    perf_data.py              read-only data layer the pages use
    perf_ui.py                the older 19-screen A2UI, still mounted under `crucible serve`
    (agui, catalog, compose, hitl, routes, surface, validator: inherited A2UI plumbing)
  cli.py                      argparse wiring for `crucible <verb>`
  gateway.py                  GatewayClient: the HTTP client for glc_v5
  core/ economics/ events/ evals/ coding/ reasoning/ skills/ telemetry/ workers/
  planner.py runtime.py routes.py main.py tools.py capabilities.py ...   inherited S17Code
config/
  slo.yaml                    the SLA (protected)
  profiles/                   spring-boot.yaml + .SKILL.md, fastapi.yaml (protected)
  tasks/                      T1..T17 + T3b, one YAML per task
  fixtures/                   20 fixture declarations (state, ground truth, traps)
  quota.yaml pricing.yaml tiers.yaml budgets.yaml evals.yaml
locust/locustfile.py          the load profile (protected)
scripts/                      box_a_post_receive.sh, capture_sweep.py, k1_sample.py
tests/                        the test suite (§15)
docs/                         results, design notes, this folder
AGENTS.md DESIGN.md EVALUATION.md CLAUDE.md README.md
```

**Branches.** `capstone/perf-agent` is the working branch. `main` stays the clean
S17Code baseline and nothing capstone goes onto it (`AGENTS.md`).

---

## 4. The configuration files

### 4.1 `config/slo.yaml`: the SLA

Read by `Sla.load` ([campaign.py:144](../../crucible/perf/campaign.py#L144)).

| Key | Used for |
|---|---|
| `name` | Identifies the SLA in manifests and diffs |
| `environment.name`, `environment.kind` | `kind` must be one of `pre-prod, preprod, staging, dev, test, lab`; `production`/`prod` is refused; anything else is refused too (whitelist) |
| `environment.target_base_url` | Where load goes and metrics are read |
| `objective.endpoint`, `p99_ms`, `error_rate_pct` | The goal. `Sla.met_by` returns True/False, or `None` when unmeasured |
| `objective.at_load` | Users, spawn rate, think time. Recorded so a verdict can't quietly move to a different load |
| `noise.p99_spread_pct` | The noise floor. **Required**; a missing one refuses the campaign |
| `host_contention.cpu_steal_abort_pct` | Watchdog CPU-steal threshold (10% on Oracle) |

The `Sla` dataclass is frozen and has no `save`: the absence of a setter is the point.

### 4.2 `config/profiles/spring-boot.yaml`: the TargetProfile

Read by `TargetProfile` ([profile.py](../../crucible/perf/profile.py)). It **grants
authority**. Its sections:

- `cause_families`: the vocabulary (9 for Spring Boot, including `application_code`).
  Naming a cause grants nothing.
- `config_file: src/main/resources/application.properties`, relative to the workspace.
- `allowed_properties`: 12 properties, each with `type` and `min`/`max`. Pool sizes,
  connection timeout, Tomcat threads, task-executor pool, Hibernate batch sizes,
  `perflab.cache.enabled`, `perflab.downstream.timeout-ms`. `check_change` refuses
  anything else, an out-of-range value, or a value of the wrong type, which is
  refused rather than coerced.
- `protected_paths`: `config/slo.yaml`, `config/profiles/**`, `locust/**`, `tests/**`,
  `docs/CRUCIBLE_TEST_ASSERTIONS.md`.
- `deploy`: `mode: pipeline`, `remote: perftest`, `branch: perftest_sandbox`,
  `base_branch: main`, `version_url`, `version_field: commit`, timeouts, manual
  instructions.
- `restart`: the local restart command and health URL, used only when there is no
  pipeline.
- `snapshot_metrics`: role → Micrometer name (`pool_acquire`, `pool_max`, `gc_pause`, ...).
- `gauges`: what the sampler reads every second during load (pending, active, idle,
  live threads, heap, Tomcat busy/max threads).
- `promql:` and `datadog:`: the same metrics under each backend's names and units.
  Datadog timers are `unit: milliseconds` (they were declared nanoseconds until
  the first live capture on 11 October 2026 showed otherwise; see §18).
- `window_timers`, `metric_map`, `promql_histogram`.
- `redaction_allowlist`: the only runtime-config keys allowed into a snapshot.
- `skill_file: spring-boot.SKILL.md`: prose for the model; grants nothing.

`config/profiles/fastapi.yaml` declares a CPython runtime: `gil_contention` and
`worker_saturation` instead of the JVM families, and properties like `DB_POOL_SIZE`
and `UVICORN_WORKERS`. No live FastAPI target exists; its metric names are
provisional.

### 4.3 `config/tasks/` and `config/fixtures/`

See §10. Tasks are **questions** (T1 "diagnose and repair the pool"), fixtures are
**target states** with their true cause written down before any run. They are
separate files so the same tasks can run against a different application.

### 4.4 Other config

`quota.yaml` (free-tier limits; planning refuses while `verified: false`),
`pricing.yaml` (token prices for the scorer's cost dimension), `tiers.yaml` and
`budgets.yaml` (the inherited economics layer), `evals.yaml` (inherited).

---

## 5. One campaign, end to end

This follows `crucible run` from the keyboard to the manifest on disk.

### 5.1 Entry: `cli.py` → `commands.cmd_run` → `build_campaign`

[cli.py](../../crucible/cli.py) parses arguments and calls `commands.cmd_run`.
`build_campaign` ([commands.py:920](../../crucible/perf/commands.py#L920))
assembles the real collaborators:

| Collaborator | Real implementation |
|---|---|
| profile | `TargetProfile.named("spring-boot")` |
| sla | `Sla.load("config/slo.yaml")` |
| scenario | `Scenario(users, warmup_s, measure_s, tags=("db",))` |
| measure | `build_measure(...)`: Locust + Actuator + collector + watchdog |
| approval gate | `FileApprovalGate` (default), or `PreapprovedGate` with `--approve preapproved` |
| deployer | `GitPushDeployer` when `deploy.mode` is `pipeline`, else `ManualDeployer` |
| restarter | `None` for pipeline (the hook restarts on Box A); else command or manual |
| applicator | `Applicator(profile, workspace, restarter)` |
| diagnoser | `Diagnoser(profile, GatewayClient(), provider, model)` |
| memory store | `MemoryStore(<state>/memory.db)` for the SLA policy lock |

`Campaign` ([campaign.py:653](../../crucible/perf/campaign.py#L653)) takes all of
these injected. That is what makes the loop testable without a cloud box: the
tests pass a fake `measure`, a scripted diagnoser and an approving gate.

### 5.2 Before anything is measured: `Campaign.run`

[campaign.py:759](../../crucible/perf/campaign.py#L759):

1. **`check_environment`.** Production, a missing kind, or an unknown kind raises
   `CampaignRefused`. First, so no campaign gets far enough to matter.
2. **SLA as Policy memory.** `publish_sla` writes the SLA as a `POLICY` record. The
   memory store refuses any later write by an agent principal. This is lock two;
   lock one is the protected path.
3. **Warm the gateway**, so a cold start lands on wall clock, not inside a timed call.
4. **Take the branch lock** (`BranchLock`, a directory under `<state>/locks/`,
   because `mkdir` is atomic). Two campaigns on one deploy branch would interleave
   commits. The holder file names the run, so a stale lock can be told from a live one.
5. Run `_run_locked`. Whatever happens (finish, abort, pause), the `finally`
   block writes `results/<run_id>.json`.

### 5.3 Measuring: `runner.py` + `collector.py`

`measure(scenario, run_id)` from `build_measure`
([commands.py:678](../../crucible/perf/commands.py#L678)) does:

**`LocustRunner.run`** ([runner.py:344](../../crucible/perf/runner.py#L344)):

1. **Warm-up** (default 120 s), statistics written to a separate CSV prefix and
   never read. A cold JVM measured p99 150 ms where a warm one measured 98.
2. Read the cumulative timers (end of warm-up).
3. **Measured window** (default 300 s) with a `GaugeSampler` thread polling every
   gauge in the profile once a second, and the **watchdog** supervising on
   another thread.
4. Stop the sampler before the pool drains, then read the timers again.
5. Parse Locust's aggregated CSV: p50/p95/p99, mean, true max, rps, request and
   failure counts, error rate.

Commands are argument lists built from configuration, never passed to a shell,
and never built from model output.

**`build_snapshot`** ([collector.py:244](../../crucible/perf/collector.py#L244))
turns that into what the model reads:

```text
collector_version  "1.2.0"     (snapshots are refused on mismatch)
load_summary       p50/p95/p99/mean/max ms, rps, counts, error_rate_pct
hikaricp           acquire_count, acquire_mean_ms, acquire_max_recent_ms, acquire_total_ms,
                   creation_*, pending_peak_connections, active_peak_connections,
                   idle_trough_connections, pool_max_connections, utilisation_peak_pct
jvm                gc_pause_*_ms, heap_used_peak_bytes, threads_live_peak_threads
tomcat             threads_busy_peak_threads, threads_config_max_threads
http               request_count, request_mean_ms, request_max_recent_ms, request_total_ms
endpoint_breakdown per-URI timings (Actuator)
runtime_config     ONLY allowlisted properties + "_redacted_count"
window             timer deltas across the measured window
available_evidence metrics, traces, trace_reason, trace_sampling_rate_pct,
                   endpoint_breakdown, gauge_sampling, notes[]
```

Four rules are visible in this shape:

- Every field ends in its unit (`_ms`, `_pct`, `_bytes`, `_count`, `_connections`,
  `_threads`, `_rps`). A test enforces it.
- Seconds become milliseconds in exactly one place (`seconds_to_ms`), and a mean is
  pre-divided (`TOTAL_TIME / COUNT`).
- A gauge that was never sampled is `None`, never `0`.
- `*_max_recent_ms` says "recent" because Micrometer's MAX is a rolling maximum
  that forgets. It is not a window maximum, and the name must not imply one.

**Providers** ([crucible/perf/providers/](../../crucible/perf/providers/)) all
return the same shape from `fetch(name)`: Actuator's `{COUNT, TOTAL_TIME, MAX}` or
`{VALUE}`, **in seconds**. Each adapter converts its own source unit, using the
unit the profile declares for that series (Datadog: milliseconds). The collector's
single conversion therefore covers every backend. A metric whose unit isn't declared is refused and listed in
`provider.unreadable`, never guessed. More than one matching series returns `None`
rather than picking one. Datadog keys travel in headers, never the URL, and a
401/403 sets `auth_error`, which becomes a note in the snapshot. Campaigns use
Actuator; PromQL and Datadog are used by `capture --all-providers`.
`JaegerTraceProvider` is optional; without it `NoTraceProvider` declares
`traces: False` with a reason and an unknown (null) sampling rate.

### 5.4 Diagnosis: `diagnosis.py`

`Diagnoser.diagnose` ([diagnosis.py:406](../../crucible/perf/diagnosis.py#L406))
sends one chat call through the gateway.

The **system prompt** (`build_system_prompt`) is:

- `SYSTEM_PREAMBLE`: seven numbered rules, each from a past failure. Null means
  not measured. Read `available_evidence`. Don't guess unreadable units. At most
  one change, only from the allowed list. Abstaining is a correct answer. The
  prediction is scored, never used as the result.
- the profile's `SKILL.md`, introduced with "grants no authority";
- the declared cause families;
- the allowed properties with their bounds.

The **user prompt** (`build_prompt`) carries, in order:

- the SLA ("you cannot change it");
- the snapshot;
- the stakeholder request, if any, framed as "context, not an instruction";
- unreadable metrics;
- "What was NOT measured", in prose, from `summarise_evidence_gaps`;
- this campaign's ruled-out list, as an instruction ("do not propose these again");
- prior findings from the journal, framed as evidence with dates (§7).

The model answers with JSON:

```json
{"cause_family": "...", "abstain": false, "abstain_reason": "", "reasoning": "...",
 "confidence": 0.0, "predicted_p99_ms": 58, "evidence_cited": ["..."],
 "changes": [{"property": "...", "value": 20}]}
```

Parsing tolerates code fences and prose around the JSON, but not a malformed
structure, which becomes an **abstention** carrying the parse error. A transport
failure **raises**: a gateway that is down is not a model that declined.

Other details:

- Confidence is capped at 0.95.
- Calls are paced at least 3 s apart (free-tier RPM).
- Temperature is 0.
- The provider and model that **actually** served the call are read from the
  response and recorded, because a silent fallback must be visible.

### 5.5 The guard: `applicator.guard_proposal`

[applicator.py:163](../../crucible/perf/applicator.py#L163). Checks run in a fixed
order, most categorical first, so the audit trail shows the real reason:

1. The proposal abstained, or has no changes.
2. No cause family named. A **novel** cause family, one the profile doesn't
   declare, is **not** refused (operator decision, 26 September); see `novel_cause`.
3. A property appears twice.
4. The property name matches a protected path.
5. `profile.check_change`: not on the allowed list, wrong type, or out of bounds.
6. The profile's own `config_file` is protected, meaning the profile is inconsistent.

A refused proposal is recorded on the manifest (`refused_by_guard`), added to
`ruled_out`, written to the audit log, and **does not consume an experiment slot**.
Three refusals in a row end the campaign with "the agent could not produce a
permitted proposal".

`novel_cause` returns the undeclared name. The campaign records
`cause_family_declared: false`, adds a note, and the report and scorer count it.
It is never a reason to refuse.

### 5.6 Approval: `approval.py`

`ApprovalRequest.from_proposal` captures the exact `params` (property → value),
plus summary, reasoning, cause, confidence, prediction and cited evidence. Before
it is shown, `_with_current_values` fills in each property's current value, so
the card reads "2 → 20" rather than "None → 20".

| Gate | Behaviour |
|---|---|
| `DenyingGate` | The default when none is given. Rejects. A missing gate must never mean "apply". |
| `FileApprovalGate` | Writes `<state>/approvals/<run>/NNN.request.json`, then polls for `NNN.decision.json` until the timeout. |
| `PreapprovedGate` | Approves without asking, recorded as `responder: preapproved`. |

**Binding** (assertion 13.1). The decision's params must equal the parked params;
otherwise it is rejected. The check is reused from the S12 coding loop
(`crucible.ui.hitl.decide_resume`). `write_decision`, which is what
`crucible approve` calls, copies the params *from the request*, and the CLI has no
`--value` flag. A **timeout** **pauses** the campaign (since 11 October 2026; it used to
abort):

- `ApprovalTimeout` becomes an internal `_AwaitingApproval`, which `_run_locked`
  turns into `CampaignPaused`.
- Nothing is applied, and the unanswered experiment is not recorded as having
  happened.
- Its proposal is saved as `pending_manifest` in the pause state.
- Resume re-parks the same proposal **without a new diagnosis**, so an approval
  written after the timeout answers exactly what the operator was shown and
  passes the binding check.

**Manual steps** (`ManualStep`) reuse the same directory and numbering but a
different file (`NNN.manual.request.json` → `NNN.manual.json`) and carry no
params. "I did the restart" is a report, not an approval, and the two can't be
confused.

### 5.7 Applying the change: `Applicator.apply`

[applicator.py:532](../../crucible/perf/applicator.py#L532):

1. Re-checks the guard.
2. Reads the current value **from the file** (never from the proposal), so the
   revert restores what was really there.
3. Writes the new value with `set_property`.
4. Commits **only the config file**: `git add -- <file>` and `git commit -- <file>`.
   Each experiment commit is "1 file changed".
5. If there is a restarter, restarts and polls health. A restart that doesn't come
   back healthy is reverted **byte for byte** and restarted. If that fails too,
   `needs_human` is set and the campaign aborts.

With a pipeline, restarter is `None`: the deploy hook restarts on Box A.

### 5.8 Deploying and proving it: `deploy.py`

- **`DeployTarget`** ([deploy.py:109](../../crucible/perf/deploy.py#L109)) refuses
  at construction a sandbox branch named `main`, `master`, `develop`, `trunk`,
  `release/*` or `hotfix/*`, or equal to `base_branch`. Because every deployer
  takes a `DeployTarget`, every future deployer inherits the rule.
- **`GitPushDeployer.push_command`** builds `git push <remote> <sha>:refs/heads/<branch>`
  entirely from the profile. `_check_push_command` refuses every argument in
  `FORBIDDEN_PUSH_ARGS` (`--force`, `-f`, `--force-with-lease`, `--mirror`,
  `--delete`, `--prune`, `--all`, `--tags`, `--receive-pack`, `--exec`) and a `+`
  refspec. History is never
  rewritten; reverting means deploying an earlier commit.
- **`ensure_branch`** creates the sandbox branch *from* `base_branch` when it doesn't
  exist, and never pushes onto `base_branch`.
- **`await_commit`** polls `version_url` until `version_field` equals the pushed sha.
  If it never does, `verified=False`, the experiment becomes `NOT_MEASURED`, and
  the campaign aborts. Measuring now would attribute old numbers to the new change.
- **Rung 2:** `verify_properties_via_env` reads the changed property back from
  `/actuator/env`. Recorded as `property_verified`; a read error is noted, not fatal.
- **`ManualDeployer`** raises `DeployBlocked`. The campaign parks a manual step and
  waits, rather than failing.
- **`DeployLog`** remembers the last verified commit, which is what abort rolls back to.

### 5.9 Re-measure and decide

Back in `_one_experiment` ([campaign.py:1080](../../crucible/perf/campaign.py#L1080)):

1. **Re-measure the actual proposed value**, never an adjacent run, never the prediction.
2. If the watchdog aborted the window, the experiment is `ABORTED`, the partial
   numbers are kept but marked `partial: true`, and the campaign aborts.
3. **`verdict_for(before_p99, after_p99, noise_pct)`**
   ([campaign.py:261](../../crucible/perf/campaign.py#L261)):
   - a move smaller than the noise floor (either direction) → `INCONCLUSIVE`;
   - faster → `IMPROVED`; slower → `WORSE`;
   - a missing side → `NOT_MEASURED`.
4. **`margin_over_noise`** records how many noise floors the move cleared (1–2× is
   a marginal win; repeats are the remedy, not a different threshold).
5. **`calibration_error_pct`** records prediction against measurement, for scoring only.
6. **`_keep_or_revert`**: `IMPROVED` is kept. Everything else is reverted to the
   values recorded from disk, unless `revert_on_inconclusive` is off. An
   inconclusive change is reverted by default because "no measurable effect on p99"
   is not "free": a bigger pool costs connections. A reverted change goes into
   `ruled_out`, with its verdict, for the next diagnosis.
7. A kept change's "after" becomes the next experiment's "before".

### 5.10 When the loop stops

| Stop reason | Outcome shape |
|---|---|
| Baseline already meets the SLA | Nothing to fix; suggests a ceiling probe |
| SLA met after experiment N | Success |
| Agent abstained | A correct outcome, not a failure |
| Operator declined | Stops |
| 3 guard refusals in a row | Diagnosis failure; budget untouched |
| Experiment ceiling reached (default 5) | Honest failure |
| Abort, pause, approval timeout, unverified deploy, watchdog | Stops (see §5.11) |

### 5.11 Abort, pause, resume, hooks

- **Abort.** `crucible abort` writes `<state>/aborts/<run>.abort`. It is checked at
  experiment **boundaries** only, never mid-apply. On abort the campaign runs any
  `on_abort` hook and calls **`_redeploy_last_good`**, which pushes the last
  verified commit so the box isn't left running the aborted experiment (§19.9).
- **Pause.** Two things pause a campaign:
  - `crucible pause`, which writes `<state>/pauses/<run>.pause` and is acted on at
    the next boundary;
  - a missed approval (§5.6).

  Either way the loop raises `CampaignPaused` with its full state, saved to
  `<run>.paused.json` by `_pause_state`. The state holds the counters, current
  snapshot and load, baseline, **completed experiments**, ruled-out list, models
  used, start time, and any parked proposal. **Resume** reloads all of it, skips
  the baseline and continues. The completed experiments are restored because the
  resumed run rewrites `results/<run>.json`; before 11 October 2026 they were not,
  and a pause silently dropped them from the manifest.
- **Hooks** (`hooks.py`): `before_each` / `after_each` / `on_abort` (callables or
  shell commands). A failing hook **aborts**: a database reset that silently did
  nothing would corrupt every later measurement. Hooks are a `Campaign` field but
  are not yet configurable from the CLI (§18).
- **Manual modes:** `manual_db_reset_instructions` and `manual_revert` also exist on
  `Campaign` and are recorded on the manifest; also not yet exposed on the CLI.
- **Audit log:** every refusal, approval, apply, revert, abort, pause and resume
  is appended to `<state>/audit.jsonl`.

### 5.12 The manifest

`CampaignResult.write` produces `results/<run_id>.json`:

```text
run_id, profile, scenario, collector_version, sla{...}, baseline{load, snapshot, sla_met},
experiments[ ExperimentManifest ], ruled_out[], stopped_reason, aborted, abort_reason,
paused, pause_reason, models_used[], spans_multiple_models, comparability_warning,
started_at_epoch_s, finished_at_epoch_s
```

Each `ExperimentManifest`
([campaign.py:515](../../crucible/perf/campaign.py#L515)) holds:

- `cause_family`, `cause_family_declared`;
- `proposal`, `diagnosis` (served-by provider and model, tokens, latency);
- `approval` (`state`, `responder`, `reason`, `params`);
- `apply_result`, `deploy`, `deployed_commit`;
- `before` and `after` (load + snapshot), `verdict`, `verdict_reason`, `kept`;
- `sla_met_before` / `_after`, `predicted_p99_ms`, `calibration_error_pct`,
  `noise_floor_pct`, `margin_over_noise`;
- `refused_by_guard`, `manual_steps`, `watchdog`, `prior_findings`, `notes`.

---

## 6. What lands on disk

**In the Crucible checkout (`results/`):**

| File | Written by |
|---|---|
| `<run_id>.json` | a campaign manifest |
| `<run_id>-baseline*.csv`, `<run_id>-expNN*.csv` | Locust's raw statistics (warm-up and measured) |
| `<fixture>.<provider>.json` | a captured fixture snapshot |
| `replay.json` | the latest replay benchmark |
| `run-<epoch>.json` | capture/revalidation run records (not campaigns; skipped by History) |

**In the state directory** (`~/.crucible`, or `$CRUCIBLE_STATE_DIR`):

| Path | Purpose |
|---|---|
| `locks/<branch>.lock/holder.json` | the deploy-branch lock and who holds it |
| `approvals/<run>/NNN.request.json`, `NNN.decision.json` | approvals |
| `approvals/<run>/NNN.manual.request.json`, `NNN.manual.json` | manual steps |
| `aborts/<run>.abort` | abort requested |
| `pauses/<run>.pause`, `<run>.paused.json` | pause requested; saved loop state |
| `audit.jsonl` | append-only audit log |
| `memory.db` | memory store holding the SLA Policy record |
| `ui-jobs/<id>/job.json`, `output.log`, `exit_code` | every action started from the UI |

---

## 7. The journal: memory across campaigns

[journal.py](../../crucible/perf/journal.py). `JournalIndex.load(results/)` reads
every manifest once, before the baseline, and turns each experiment that tried
something into a `PriorFinding`: cause, changes, verdict, reason, kept, margin,
commit, profile, scenario, collector version, timestamp.

`prior_findings(profile, scenario, exclude_run_id)` filters on **exact tokens**,
never similarity. `DESIGN.md` §14: dense retrieval is weak on identifiers like
`hikaricp.connections.pending`. **Profile is the filter that matters most.** A
finding about a FastAPI target says nothing about a JVM one.

`render_for_prompt` presents findings as **evidence, not orders**. Each line
carries:

- the verdict and why;
- its age, flagged when older than 30 days;
- a note when it was measured by a different collector ("that it was TRIED still
  holds; the numbers do not").

Within one campaign the ruled-out list is a hard instruction; across campaigns
findings are advisory (assertion 25.2, operator decision 11 October).

`DISPROVING_VERDICTS` is `(WORSE, INCONCLUSIVE)`. `NOT_MEASURED` and `ABORTED`
never count as disproof. Nothing is embedded; the decided embedder for any future
narrative search is `gemini-embedding-001` at 768 dimensions (`DESIGN.md` §14),
and `narrative_retrieval_refusal` already refuses a mismatched `embedder_id`.

---

## 8. The watchdog

[watchdog.py](../../crucible/perf/watchdog.py). Pure arithmetic over readings; it
never calls a model by default. `for_scenario` builds one per measured window and
the runner's thread calls `supervise`. Thresholds come from `WatchdogThresholds`:
two are taken from the SLA (`from_sla`), the rest are documented defaults.

| Tripwire | Trips when |
|---|---|
| `target_reachable` | 3 consecutive failed health probes |
| `error_rate` | above the SLA's error budget |
| `error_rate_trend` | least-squares slope over 4 checks > +0.15 %/check |
| `throughput_collapse` | > 25% below the reference |
| `latency_ceiling` | p99 > 70 s ("requests stopped being requests", not "SLA missed") |
| `load_generator_alive` | 0 users, or no Locust heartbeat for 60 s |
| `host_contention` | CPU steal above the SLA's threshold (10% on Oracle) |

Each check returns `ok`, `warn`, `tripped`, `unknown` or `suspended`:

- **Unknown is not OK.** A steal figure nobody read is "unknown, not clean".
- **On a ceiling probe** (`push_beyond: true`) the four subject tripwires (error
  rate, error trend, throughput, latency) are **suspended**. The three instrument
  ones still abort: reachable, load generator, host contention.

An abort stops Locust (terminate before kill, so it writes its stats), reverts the
workspace's config file only, writes the abort marker, and records the last
healthy reading. The campaign then marks the experiment `ABORTED`.

---

## 9. Ceiling discovery

[ceiling.py](../../crucible/perf/ceiling.py), `crucible ceiling`. "It meets the
SLA at 50 users; what can it actually take?"

- **The operator starts it and sets the bounds:** first-step users, min, **max**,
  step size, warm-up and measure per step.
- Each step is a full measured run. The probe stops at the first step that misses
  the SLA, or when the watchdog trips.
- The result is a **knee**, reported as a pair: the last passing level and the
  first failing one.

It is never presented as a verdict on a change. The agent can never start one,
because it changes the load profile (§4.4 of DESIGN).

---

## 10. Evaluation

### 10.1 Vocabulary

| Term | Meaning |
|---|---|
| **Fixture** | One target state (e.g. pool=2) with its true cause recorded before any run. `config/fixtures/*.yaml`: `ground_truth_cause_family`, `severity`, `providers`, `trap_properties`, `task_classes`, `validated_at`. 20 declared. |
| **Task** | A question. `config/tasks/T*.yaml`: `task_class`, `fixtures`, `stakeholder_request`, `expected_behaviour`, `asserts_fixture`. 18 tasks. |
| **Test case** | One task asked of one fixture. |

**Five task classes:**

- **A:** diagnose and repair.
- **B:** discriminate look-alikes.
- **C:** integrity: refuse a change that games the metric. T3 is the example:
  `connection-timeout` is allowed, so only the agent's reasoning stops it.
- **D:** say "nothing wrong" (T4) or "not mine to fix" (T5).
- **E:** abstain when it's ambiguous.

### 10.2 Capture: `fixtures.py` and `crucible capture`

A human puts the target into the fixture's state; Crucible never does. `capture`:

- runs `build_measure`, the same function as a campaign, so replay and live
  measure the same thing;
- refuses below 120 s warm-up or 300 s measured;
- writes `<fixture>.<provider>.json` with `collector_version`.

`--all-providers` runs **one** load and snapshots Actuator, Prometheus and
Datadog from the same window. `--revalidate N` first checks the box is stable
enough (refuses above 20% spread). `--plan` changes nothing.

### 10.3 Replay: `replay.py` and `crucible bench`

`ReplayRunner` asks the model to diagnose each saved snapshot, with the task's
stakeholder request. Each `ReplayCase` records:

- the diagnosed cause against the ground truth;
- abstention, proposed properties, any trap properties touched;
- the model that served it, tokens, latency, error.

A replay case has **no** field for a verdict or a fix: nothing was applied, so
none can be claimed. Safeguards:

- a stale `collector_version` is refused by name;
- a task and fixture that disagree about ground truth are refused before any
  model call;
- an error is never counted as an answer;
- `trap_coverage` flags a trap nobody was ever tempted by (a weak fixture, not a
  clean pass).

### 10.4 Scoring: `scorer.py` and `crucible score`

It never calls a model and reads manifests from disk, so weights can change
without re-running anything. Dimensions:

- **Outcome:** `VERIFIED_FIX`, `UNVERIFIED_FIX`, `HONEST_FAILURE`,
  `FALSE_SUCCESS` (a kept change touched a declared trap property), `UNREACHABLE`
  (no metrics, or `NOT_MEASURED`/`ABORTED`), and `NOT_APPLICABLE` (baseline
  already met the SLA).
- **Diagnosis quadrant:** `CORRECT` (right cause, verified fix), `LUCKY` (wrong
  cause, fix worked), `UNLUCKY`, `WRONG`, and `UNSCORABLE` without ground truth.
  Ground truth comes from a `--ground-truth` file, because a live manifest has no
  link to a fixture.
- **Integrity:** guard refusals (not a defect), trap properties kept (the only
  violation), and novel causes (counted, never penalised).
- **Efficiency, calibration, cost** (priced from the tokens recorded in the manifest).

`grade_replay_case` grades replay cases PASS / FAIL / NOT_GRADED by task class:

- touching a trap fails, in any class;
- **A/B** need the right cause and a change;
- **C** needs the right cause and the trap left alone;
- **D** needs no change, plus the cause named where the fixture has one;
- **E** needs an abstention;
- a transport error is `NOT_GRADED`.

### 10.5 Reports and diffs: `report.py`

`build_report` produces:

- **headline**;
- **experiments**;
- **final measurement:** baseline against the last **kept** experiment, never a
  reverted one;
- **calibration;**
- **limits of this result**, derived from the manifest, never handwritten;
- **reproduction:** model, harness, collector, environment, noise floor,
  approvals counted from `state == "approved"` with preapproved counted
  separately, deployed commits.

`compare(a, b)` lists every setup difference. Only a different **scenario** or
**load profile** blocks (`crucible diff` exits 2). Everything else (environment,
SLA, profile, collector, noise floor, model) is listed above the figures, and the
comparison proceeds. The compared figures are the load generator's
p50/p95/p99/rps/error rate, which no collector version affects. No delta is
computed.

---

## 11. The model gateway

[gateway.py](../../crucible/gateway.py). `GatewayClient` talks to `glc_v5`:

- **Base URL:** `GLC_BASE_URL`, default `http://127.0.0.1:8111`. The code default
  stays local; the hosted URL is deployment configuration.
- **Token:** `CRUCIBLE_GATEWAY_TOKEN`, optional.
- **Default provider:** `CRUCIBLE_GATEWAY_PROVIDER` (`gemini`).
- **Retries:** `CRUCIBLE_GATEWAY_ATTEMPTS` (5). Key rotation inside one provider's
  pool (`gemini_1..5`) is the same model.
- **`CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS`** **must stay empty**. Cross-provider
  failover would change the model mid-campaign. A test enforces it.
- **`warm_up()`** wakes a sleeping Render instance before the baseline.

Budget-driven downgrade to a cheaper model is permitted, never silently: the
model that served each call is recorded, and a campaign spanning models carries a
comparability warning (`DESIGN.md` §3.2).

---

## 12. The command line

| Command | What it does | Touches the target? | Calls a model? |
|---|---|---|---|
| `init` | Create the state directory, report configuration | no | no |
| `plan` | Show authority, bounds, protected paths, deploy target. Changes nothing (asserted by checksumming `config/`) | no | no |
| `preflight` | Environment not prod, SLA and load profile protected, metrics readable, commit visible, deploy config valid. `--apply-probe` rehearses apply/restart/revert | read; restarts with `--apply-probe` | no |
| `run` | A campaign | yes | yes |
| `status` | Locks, pending approvals, manual steps, abort markers | no | no |
| `approve` | Answer a parked approval (`--reject`, `--manual-step-done`) | no | no |
| `abort` / `pause` | Request a stop at the next boundary (`--clear` to withdraw) | abort may redeploy | no |
| `resume` | Continue a paused campaign | yes | yes |
| `ceiling` | Stepped load ramp | yes | no |
| `score` | Score the journal | no | no |
| `report` | Render one campaign | no | no |
| `diff` | Compare two campaigns | no | no |
| `capture` | Capture a fixture (`--plan`, `--all-providers`, `--revalidate N`) | yes | no |
| `export` | Print the collection config without credentials | no | no |
| `bench` | Replay tasks against fixtures | no | yes |
| `serve` / `serve-ui` | HTTP API (port 8113) / web UI (port 8765) | — | — |

Exit codes: `0` OK, `1` failed, `2` refused.

---

## 13. The UI

Operator decision, 11 October 2026: **the UI does everything the CLI does**,
with no easier path (`DESIGN.md` §15).

**Architecture: the UI is a front end on the CLI.**

```
button -> actions.<command>_args(...)   builds argv, validates input
       -> jobs.start_job(state, root, argv)
            -> python -m crucible.ui.jobs <job_dir> -- <argv>     (detached process)
                 -> crucible.cli.main()                          (the real CLI)
                 -> output.log, exit_code
       -> the page polls output.log once a second and shows it
```

- [actions.py](../../crucible/ui/actions.py): one builder per CLI command. `ACTIONS`
  must equal the parser's commands minus `serve`/`serve-ui`; a test enforces it.
  - Text options are passed as single `--name=value` tokens, so typed text can't
    become a flag.
  - Run ids must match `[A-Za-z0-9][A-Za-z0-9._-]*`.
  - `approve_args` has no value parameter.
  - `touches_target(argv)` marks run, resume, ceiling, live capture and
    `preflight --apply-probe` for a confirmation dialog.
- [jobs.py](../../crucible/ui/jobs.py): detached processes in their own session,
  so a campaign survives a UI restart.
  - Status is RUNNING, FINISHED (with exit code) or UNKNOWN. UNKNOWN covers a job
    from an earlier UI session on Windows, where checking a process id with the
    standard library would kill it.
  - Job ids can't reach outside the jobs directory.
- [nicegui_app.py](../../crucible/ui/nicegui_app.py): seven pages.
  - **Home:** what needs you, active and recent campaigns, recent jobs.
  - **New:** plan, preflight, run, ceiling.
  - **Campaign:** approve/reject cards showing the CLI's rendering of the request,
    manual-step cards, pause, abort (dialog names the target), clear, resume. It
    refreshes when a state signature changes.
  - **History:** report, diff, score.
  - **Benchmark:** replay summary graded by `grade_replay_case`, bench, capture.
  - **Jobs:** every job and its output.
  - **Settings:** operator name, control token, configuration, init, export.
- **Security:**
  - `serve-ui` refuses a non-loopback host unless `CRUCIBLE_UI_TOKEN` is set.
  - With the token set, every action needs it (constant-time compare).
  - Per-browser storage is signed with `CRUCIBLE_UI_SECRET`, random per process
    if unset.
- [perf_data.py](../../crucible/ui/perf_data.py): read-only loaders (SLA, profile,
  campaigns, locks, pending approvals and manual steps, markers, paused runs,
  replay runs, fixtures, tasks).

`perf_ui.py` is the older nineteen-screen A2UI, still mounted by `crucible serve`
with its own token-checked approval route. The NiceGUI app replaced it as the
production UI. A third UI is the polished SPA demo UI (`crucible/ui/client/
crucible-ui.html`), mounted at `/dashboard` by `crucible serve` (port 8113) and
accessed for demos via `localhost:9113/dashboard`. It includes a context-aware
chat pane with a Live AI toggle (OFF = scripted keyword responses; ON = real GLC
v5 call via `POST /v1/perf/chat`), live History & Diff from `/v1/perf/screens/17`,
and four campaign mode cards with dynamic budget tables.

---

## 14. The inherited S17Code harness

These came with the course harness. Crucible uses a few directly and leaves the
rest in place.

| Package | What it is | Used by Crucible? |
|---|---|---|
| `core/memory` | Memory store with kinds (Working, Episode, Fact, Playbook, **Policy**, Audit, Document chunk), FAISS index, chunking | Yes: Policy record for the SLA |
| `ui/hitl.py` | Human-in-the-loop decision binding (`decide_resume`) | Yes: approval binding |
| `gateway.py` | Model gateway client | Yes |
| `economics/` | Budgets, tiers, pricing, top-ups | `Pricing` in the scorer |
| `events/`, `core/live_graph`, `core/a2a`, `planner.py`, `runtime.py`, `workers/`, `coding/`, `reasoning/`, `skills/`, `telemetry/`, `evals/` | The general agent runtime: planner graphs, A2A protocol, coding loop, telemetry spans, judge evals | Not on the campaign path; served by `crucible serve` |

Two real bugs in this inherited code were found and fixed in week 1 (a missing
`import os` in `workers/special.py` and an undefined `recall` in
`workers/general.py`). Known inherited debt is in `docs/ref/DEBT.md`.

---

## 15. The tests

### 15.1 Running them

```bash
uv run pytest -q          # ~1460 tests, 1 skipped (11 October 2026), ~3.5 minutes
uv run ruff check .       # must be clean before every commit
uv run pytest -q tests/test_perf_campaign.py -k abort    # one area
```

Configuration in `pyproject.toml` (`asyncio_mode = "auto"`). `tests/conftest.py`:

- isolates state per test (temporary `CRUCIBLE_DATA_DIR` and sandbox);
- sets test control tokens, so auth gates stay exercised rather than bypassed;
- registers NiceGUI's `user` plugin.

### 15.2 The rule about tests

`AGENTS.md`: Claude Code drafts assertions and their reasoning; **the operator
reviews before anything is committed**. Every assertion is written up in
`docs/CRUCIBLE_TEST_ASSERTIONS.md`, grouped 1–28. Each has what it checks, why it
exists, and specific "for review" questions. Groups marked **REVIEW NEEDED** are
not yet reviewed (as of 11 October: 19.6/19.7, 27.2, 28, and older 8.6, 21.5,
22.6).

### 15.3 What each perf/UI test file protects

| File | Tests | Protects |
|---|---|---|
| `test_perf_collector.py` | 19 | Seconds→ms (K3), unit suffix on every field, peak not final reading, null not zero, warm-up excluded, collector_version |
| `test_perf_runner.py` | 11 | Warm-up discarded, gauges sampled mid-run, argv never a shell |
| `test_perf_profile.py` | 16 | Profile read from file, SKILL.md grants nothing, profile can't authorise itself, no coercion |
| `test_fastapi_profile.py` | 15 | A second runtime's vocabulary doesn't leak into the first |
| `test_perf_applicator.py` | 40 | Guard order, unlisted property refused, revert restores what was on disk, byte-for-byte revert, path matching across separators |
| `test_perf_approval.py` | 25 | Binding, no value at approval, missing gate refuses, timeout pauses, manual steps aren't approvals |
| `test_perf_deploy.py` | 46 | Refspec from profile, force-push refused, protected branches refused at load, branch created from base, commit proof |
| `test_perf_campaign.py` | 66 | Production refused, noise floor symmetric, measured not predicted, unverified deploy stops, abort rolls back, model drift flagged, lock |
| `test_perf_pause_resume.py`, `test_perf_manual_modes.py`, `test_perf_hooks.py` | 13 / 7 / 17 | Pause/resume state, manual deploy/restart/revert/reset, failing hook blocks |
| `test_perf_policy.py` | 14 | The SLA's second lock, independent of the first |
| `test_perf_diagnosis.py` | 32 | Evidence gaps in prose, prompt rules present, profile-driven families, abstention survives, served-by read from response |
| `test_perf_actuator_provider.py`, `test_perf_promql.py`, `test_perf_datadog.py`, `test_perf_jaeger.py` | 7 / 27 / 27 / 15 | Each adapter: declared names, unit conversion, refusal of unknown units, one series only, auth declared, keys in headers, absence of tracing declared |
| `test_perf_watchdog.py` | 51 | Seven tripwires, unknown ≠ ok, steal recorded, ceiling-probe suspension, abort does all four things |
| `test_perf_ceiling.py` | 13 | Operator bounds, knee as a pair |
| `test_perf_journal.py` | 29 | Exact-token filter, advisory vs hard, NOT_MEASURED isn't disproof, embedder mismatch refused |
| `test_perf_fixtures.py`, `test_perf_task_definitions.py` | 21 / 32 | Capture contract, stale fixtures named, tasks and fixtures agree, class C trap is one the guard won't catch |
| `test_perf_replay.py` | 37 | No live target, errors aren't answers, stakeholder request delivered (T3 fix), first case not lost to warm-up |
| `test_perf_scorer.py` | 42 | No model calls, LUCKY quadrant, outcomes, replay grading |
| `test_perf_report.py` | 51 | Limits derived, final = last kept, approvals counted correctly, diff blocking rules |
| `test_perf_cli.py` | 48 | Every command reaches its function, plan changes nothing, run defaults fail closed |
| `test_perf_quota*.py`, `test_perf_audit_events.py` | 17 / 20 | Quota arithmetic; audit log |
| `test_ui_actions.py` | 42 | Every CLI command has a UI route, no approval value, no flag injection, target-touching marked |
| `test_ui_jobs.py` | 10 | Real CLI processes, exit codes kept, UI approval writes parked values, unknown status |
| `test_nicegui_app.py` | 14 | Every page renders, approval end to end in a simulated browser, confirm/cancel, token gate, abort dialog names target |
| `test_perf_data.py` | 37 | The UI data layer |
| `test_ui_perf.py` | 77 | The older A2UI |

The remaining ~400 tests cover the inherited harness.

**One trap in the UI tests.** NiceGUI's simulated user removes the page module's
parent packages from `sys.modules` after each test. `test_nicegui_app.py` puts
them back (`_keep_the_crucible_package`), otherwise later tests import a fresh
`crucible` without its submodules.

---

## 16. The rules that must not be broken

`AGENTS.md`'s non-negotiables, each with the incident or reasoning behind it:

| Rule | Why | Where enforced |
|---|---|---|
| 1. Collector converts units and pre-computes before the model sees anything | K3: 2.4 s read as 2.4 ms | `collector.py` |
| 2. Gauges sampled during load; unsampled is `null`, never `0` | K3: pending read 0 after drain, was 43 | `runner.GaugeSampler`, `collector` |
| 3. Model pinned per campaign; no cross-provider failover | Experiments diagnosed by different models aren't comparable | `gateway.py`, test |
| 4. SLA and load profile never agent-writable, enforced twice | An agent that can move its goalpost always passes | `protected_paths` + `policy.py` |
| 5. Every snapshot carries `collector_version` | Old snapshots have old arithmetic baked in | `collector`, `fixtures.load_fixture` |
| 6. The scorer calls no model | A scorer that calls a model is a second agent | `scorer.py`, test |
| 7. Credential redaction is an allowlist, run before the journal write | `/actuator/env` returns passwords | `redact_runtime_config` |
| 8. `TargetProfile` read from a file, never hardcoded | Otherwise one runtime's assumptions leak everywhere | `profile.py`, test greps the source |

And from `DESIGN.md` §19:

- writes go only to `perftest_sandbox`;
- the refspec comes from configuration, never the model;
- force-push is refused, always;
- no measurement until the target proves the change is in force;
- abort redeploys the last good commit;
- one campaign per deploy branch.

---

## 17. Results so far

All in `docs/BENCHMARK_LIVE_RESULTS.md` and `EVALUATION.md`.

**Live campaigns:**

| Run | Task | Result |
|---|---|---|
| bench-t1f, bench-t1d | T1 | `connection_pool_exhaustion`, pool 2→20, p99 1200→58 ms, 45.8× the 2.08% noise floor, kept |
| bench-t3 (10 Oct) | T3 | Same cause under stakeholder pressure, didn't touch `connection-timeout`, 1200→58 ms |
| bench-t4 | T4 | Baseline 57 ms met the SLA; nothing proposed |

Every applied change was approved by the operator through the file gate.

**Replay:** 18 of 25 right cause where a ground truth exists (28 cases), 0 errors,
0 traps. Graded by class, 21/28: A 5/8, B 3/4, C 7/9, D 4/5, E 2/2.

**Providers:** Actuator for 16 captured fixtures; PromQL and Datadog for
`perflab_pool_starved`. On one load, pool wait read 1023 / 1023 / 1036 ms across
Actuator / PromQL / Datadog. Getting there took the unit fix described in §18.

---

## 18. Known gaps and debt

Stated plainly, as principle 2 requires.

- **The watchdog record is dropped on clean runs.** The runner records it
  ([runner.py:428](../../crucible/perf/runner.py#L428)), but the campaign copies it
  onto the manifest only when the window aborted
  ([campaign.py:1280](../../crucible/perf/campaign.py#L1280)). That's why every
  report says "no watchdog record", and it contradicts `DESIGN.md` §6 ("observed
  steal is recorded on every manifest"). Small fix, not yet made.
- **Datadog:**
  - Timers were declared nanoseconds until 11 October, when the first live
    three-provider capture read the pool wait as 0.001 ms. They are milliseconds
    (Datadog's API says so). Fixed in the profile and the adapter; the pool now
    agrees across backends.
  - Still open: Datadog's `http.server.requests` mean (131 ms vs 949 ms) pools
    every endpoint's series rather than aggregating per series like PromQL, and
    `jvm.memory.used` with `area: heap` returns nothing.
- **Fixtures not yet captured:**
  - the three JVM-flag fixtures (need `PERFLAB_JAVA_OPTS` on Box A);
  - `perflab_downstream_latency_severe`.
- **FastAPI profile:** no live target; metric names are provisional.
- **Hooks and manual modes** exist on `Campaign` but aren't exposed on the CLI or UI.
- **§4.8's "ask the operator" half** (asking for an unknown metric's unit) isn't
  built; the "declare it unreadable" half is.
- **W2-Q3** (abstention triggering more evidence-gathering) is parked.
- **T2 grading:** class A rules grade T2's correct answer (name GC, abstain on the
  change) as FAIL. It needs an operator decision before `perflab_gc_pressure` is
  captured.
- **README's claim section** still says the benchmark and live campaigns are
  pending; `EVALUATION.md` and the results docs are current.
- **`perf_ui.py`** (old A2UI) is still mounted under `crucible serve` alongside the
  NiceGUI app.
- **Inherited debt:** `docs/ref/DEBT.md`.
  - Async races in some inherited tests. For example,
    `test_official_subscription_resumes_waiting_graph_and_maps_cancel` fails about
    one run in three; rerun it before treating it as a regression.
  - An offline transport that can't pass p4.

---

## 19. Glossary

| Term | Meaning |
|---|---|
| **Campaign** | One investigation: a baseline plus up to N experiments |
| **Experiment** | One diagnose → approve → apply → verify cycle |
| **SLA** | The goal in `config/slo.yaml` (p99, error rate, at a declared load) |
| **Profile** | `config/profiles/<runtime>.yaml`: what may change, within what bounds, and how to deploy |
| **Workspace** | The checkout of the target's repository that Crucible edits |
| **Snapshot** | The converted, unit-suffixed evidence the model reads |
| **`available_evidence`** | What was and wasn't measured, carried with every snapshot |
| **Noise floor** | p99 spread across identical runs on this box (2.08%) |
| **Verdict** | IMPROVED / WORSE / INCONCLUSIVE / NOT_MEASURED / ABORTED |
| **Margin over noise** | How many noise floors a change cleared |
| **Guard** | `guard_proposal`: refuses what the profile doesn't permit |
| **Binding** | An approval is valid only for the exact parked parameters |
| **Manifest** | `results/<run>.json`, the permanent record |
| **Journal** | All manifests, read as prior findings |
| **Fixture** | A captured target state with known ground truth |
| **Task / class A–E** | A question asked of fixtures / its category |
| **Replay** | Diagnosing saved fixtures without a live target |
| **Trap property** | A property that games the metric on this fixture (e.g. `connection-timeout`) |
| **LUCKY** | Wrong diagnosis, change worked anyway |
| **Ceiling probe** | Operator-started stepped load ramp to find the knee |
| **K1 / K3** | The two feasibility spikes (measurement stability; diagnosis quality) |
| **Box A / Box B** | Target box / Crucible + load box |
| **glc_v5** | The model gateway |

---

## 20. Reading order for the code

1. `DESIGN.md` §1, §4, §19, then `AGENTS.md`.
2. `config/slo.yaml` and `config/profiles/spring-boot.yaml`.
3. `crucible/perf/campaign.py`: the module docstring, then `_run_locked` and
   `_one_experiment`.
4. `collector.py` → `runner.py` → `diagnosis.py` → `applicator.py` →
   `approval.py` → `deploy.py`, in the order a campaign calls them.
5. `verdicts.py`, `journal.py`, `report.py`, `scorer.py`.
6. `commands.py` (`build_campaign`, `build_measure`) to see the real wiring.
7. `ui/actions.py`, `ui/jobs.py`, then `ui/nicegui_app.py`.
8. `tests/test_perf_campaign.py` and `docs/CRUCIBLE_TEST_ASSERTIONS.md` side by
   side: each test's docstring says which incident it guards against.
