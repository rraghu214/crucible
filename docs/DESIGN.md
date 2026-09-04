# DESIGN.md — Crucible Architecture

## Decision log — read before proposing alternatives

| Decision | Chosen | Rejected | Reason |
|---|---|---|---|
| Metrics source | Micrometer/Actuator | Datadog, Prometheus scraping | No APM licence; Actuator JSON pull is zero-config |
| Load generator | Locust (Python) | JMeter, k6 | Python-native, integrates cleanly with agent |
| Solver for diagnosis | LLM via glc_v5 | Rule engine, threshold alerts | Competing hypotheses need reasoning, not rules |
| EM sim solver | N/A — not in scope | openEMS, NEC2 | RF antenna idea was ruled out |
| LLM provider | Gemini (pinned) | Groq, NVIDIA, auto_route | Free, best-tested in course, no model drift |
| Target language | Spring Boot (Java) | Node, FastAPI | Domain expertise; Micrometer is native |
| DB | H2 in-memory | Postgres | Zero-config for spike; Postgres later if needed |
| Deployment | Oracle Always Free or Hetzner CX32 | MacBook + ngrok | Thermal throttle risk on laptop under load |
| Scope ceiling | Config changes only | Source rewrites | Four weeks; config is reversible; source is not |
| Framework | S17Code (extended) | LangChain, CrewAI | Route C rule; S17Code already has all layers |

---

## Component map

### 1. PerfLab (target service)

`perf-lab/` — a Spring Boot service the agent runs experiments against.

Not a real production service. Designed for control:
- Three endpoints: `/api/fast` (no DB), `/api/db` (DB read), `/api/version`
- H2 in-memory by default; Postgres supported via profile
- Three injectable bottlenecks (change one property to activate each):

| Bottleneck | Property | Value | Symptom |
|---|---|---|---|
| Connection pool | `hikari.maximum-pool-size` | 2 | p99 spike; hikari.pending nonzero |
| GC / allocation | JVM `-Xmx` | 128m + allocating endpoint | Periodic p99 spikes; GC pause time high |
| Thread pool | `server.tomcat.threads.max` | 4 | p99 spike under concurrency; threads.active high |

Micrometer metrics exposed at `/actuator/prometheus` and `/actuator/metrics/{name}`.
Always expose: `hikaricp.*`, `jvm.gc.*`, `jvm.memory.*`, `jvm.threads.*`,
`http.server.requests`, `executor.*`.

`/api/db` is `@Transactional` and sleeps 50ms inside the transaction: H2
in-memory answers in microseconds, so without a held connection the pool never
queues regardless of size. The sleep simulates realistic Postgres round-trip
latency and is what makes pool size a tunable variable.

**Resolved (K3 verification, 2026-09-03):** the healthy baseline is
`maximum-pool-size=20`, not 10. K1's pool=10 baseline ran at ~85% of pool capacity
(~170 rps against a ~200 rps ceiling, `acquire_mean` ~21ms) — not fully idle. The
K3 verification run at pool=20 measured 186 rps and p99 93ms with `acquire_mean`
0.11ms and no pending queue — a genuinely idle baseline. New campaigns use pool=20
and set the K3 `baseline_reference` from a measured pool=20 p99 (~90ms), not an
assumed floor. K1 and K3 themselves were run with a pool=10 baseline and their
result stands (bottleneck p99 is 8.7× baseline either way; see `docs/K1_RESULT.md`,
`docs/K3_RESULT.md`).

### 2. Metrics collector

`crucible/perf/collector.py`

Pulls a structured snapshot from Actuator after each Locust run.

```python
class MetricsSnapshot:
    experiment_id: str
    run_id: str
    timestamp: float
    locust: LocustSummary      # p50, p95, p99, rps, error_rate
    hikaricp: HikariMetrics    # pool_max, active, pending, acquire_ms, timeout
    jvm: JvmMetrics            # gc_pause_ms, heap_used, heap_max, threads
    http: HttpMetrics          # server.requests timer
    runtime_config: dict       # /actuator/env filtered (no secrets)
```

Two probe modes:
- `MetricsSnapshot.from_actuator(base_url)` — live pull after a run
- `MetricsSnapshot.from_json(path)` — replay from saved journal (scorer use)

**Sampling note:** `hikaricp.connections.pending` is a gauge — it reads
instantaneous state. Post-run polling returns 0 because load has drained.
The metrics collector must sample pending mid-run (e.g., every 5s during
the Locust campaign) and report the peak value, not the post-run value.
The LoadRunner will trigger a background sampler thread for gauge metrics.

This is an architectural decision, not a nice-to-have. Without it the agent
sees `pending=0` in every snapshot and cannot confirm pool exhaustion — the
"clean zero vs untested zero" problem from S18. Confirmed empirically in K1
(`docs/K1_RESULT.md`): post-run pending read 0 on every bottleneck run while
mid-run sampling showed a stable queue of ~42.

### 3. Load runner

`crucible/perf/runner.py`

Wraps Locust as a subprocess. Identical parameters for every run in a campaign.

```python
class LoadRunner:
    def run(self, run_id: str, campaign: CampaignConfig) -> LocustSummary:
        # Launches locust --headless with fixed params
        # Starts a background gauge sampler (see collector §2 sampling note)
        # Saves raw CSV and summary JSON to journals/{campaign_id}/
        # Stops the sampler; hands its per-gauge peaks to the collector
        # Returns LocustSummary
```

The Locust file (`locust/locustfile.py`) is read-only during a campaign.
The runner never modifies it.

### 4. Diagnosis engine

`crucible/perf/diagnosis.py`

Single call to glc_v5. No agent loop — one prompt, one structured response.

```python
class DiagnosisEngine:
    def diagnose(
        self,
        snapshot: MetricsSnapshot,
        history: list[ExperimentManifest],
        runtime_config: dict,
    ) -> DiagnosisResult:
        # Builds context: snapshot + history + config
        # Calls glc_v5 via gateway.py (inherited from S17Code)
        # Parses JSON response
        # Returns DiagnosisResult
```

`DiagnosisResult`:
```python
@dataclass
class DiagnosisResult:
    primary_cause: str
    evidence: list[str]
    ruled_out: list[dict]
    proposed_change: ProposedChange
    predicted_p99_ms: float
    confidence: str   # low | medium | high
    confidence_reason: str
    raw_response: str
    cost_usd: float
```

Context layers sent to LLM (in order of importance):
1. Current metrics snapshot (always)
2. `/actuator/env` filtered config (always — key inputs visible)
3. Last 3 experiment manifests (if exist — prevents re-proposing disproven hypotheses)
4. Relevant source file snippets (week 2 addition — identify hot paths)

### 5. Change applicator

`crucible/perf/applicator.py`

Applies a `ProposedChange` to the PerfLab application.properties and restarts
the service. Uses the S17Code coding sandbox for guarded file editing.

```python
class ChangeApplicator:
    def apply(self, change: ProposedChange) -> ApplyResult:
        # 1. Validate: parameter in allowed list
        # 2. Validate: change is within bounds (e.g., pool <= 50)
        # 3. Human approval via S17Code HITL (blocks until approved)
        # 4. Edit application.properties via S17Code edit capability
        # 5. Restart PerfLab (maven spring-boot:run or process restart)
        # 6. Health check: /actuator/health must return UP within 30s
        # Returns ApplyResult (applied | rejected | failed)

    def revert(self, change: ProposedChange) -> ApplyResult:
        # Applies the inverse change
        # Same approval flow — revert is not automatic
```

**Authority boundary (non-negotiable):**
The applicator may only change properties in this list:
```
spring.datasource.hikari.maximum-pool-size
spring.datasource.hikari.connection-timeout
server.tomcat.threads.max
server.tomcat.threads.min-spare
spring.task.execution.pool.core-size
spring.task.execution.pool.max-size
```

It may never touch: Locust files, test files, AGENTS.md, docs/, .env, anything
outside `CRUCIBLE_SANDBOX_ROOT`.

### 6. Campaign orchestrator

`crucible/perf/campaign.py`

The top-level loop. Coordinates all components. Writes the manifest.

```
START campaign
  └─ SET baseline (3 runs, pool=20) → compute p99 median
  └─ INJECT bottleneck (change one property)
  └─ CONFIRM SLA breach (1 run → p99 > PERFLAB_SLA_P99_MS)
  └─ LOOP (max 5 experiments per campaign):
       ├─ Collect MetricsSnapshot
       ├─ DiagnosisEngine.diagnose(snapshot, history, config)
       ├─ Human approval (HITL)
       ├─ ChangeApplicator.apply(change)
       ├─ Run load test (1 run, same params as baseline)
       ├─ Collect post-change MetricsSnapshot
       ├─ Compare: did p99 improve? SLA met?
       ├─ Write ExperimentManifest to journals/
       ├─ KEEP if improved, REVERT if not, STOP if SLA met
       └─ Budget check: if LLM spend > CRUCIBLE_CAMPAIGN_BUDGET_USD, abort
  └─ Write campaign summary
END
```

### 7. Scorer

`crucible/perf/scorer.py`

Reads manifests from `journals/`. Produces the four-field score table.
Does not call the LLM. Does not re-run experiments.

```python
def score_campaign(campaign_id: str, journal_dir: Path) -> CampaignScore:
    manifests = load_manifests(campaign_id, journal_dir)
    return CampaignScore(
        experiments=len(manifests),
        verified_pass=sum(1 for m in manifests if m.verified and m.verdict == "KEPT"),
        unverified_pass=sum(1 for m in manifests if not m.verified and m.verdict == "KEPT"),
        honest_failure=sum(1 for m in manifests if m.verdict == "REVERTED"),
        integrity_violations=[m for m in manifests if m.integrity_violated],
        total_cost_usd=sum(m.cost_usd for m in manifests),
        total_duration_seconds=sum(m.duration_seconds for m in manifests),
    )
```

Scoring weights can change without re-running. That's the point.

---

## Directory structure (target state after spike)

```
crucible/
├── AGENTS.md                      ← master rules (read first, always)
├── DESIGN.md                      ← this file
├── pyproject.toml                 ← package: crucible
├── .env.example                   ← CRUCIBLE_* env vars
├── crucible/                      ← renamed from s17code/
│   ├── gateway.py                 ← glc_v5 seam (inherited, agent=crucible_agent)
│   ├── economics/                 ← budget, pricing (inherited)
│   ├── events/                    ← event engine (inherited)
│   ├── ui/                        ← HITL (inherited)
│   ├── telemetry/                 ← spans (inherited)
│   └── perf/                      ← NEW — all performance agent logic
│       ├── __init__.py
│       ├── collector.py           ← MetricsSnapshot
│       ├── runner.py              ← LoadRunner (Locust wrapper)
│       ├── diagnosis.py           ← DiagnosisEngine
│       ├── applicator.py         ← ChangeApplicator
│       ├── campaign.py            ← Campaign orchestrator
│       ├── scorer.py              ← Scorer (reads journals, no LLM)
│       └── models.py              ← ExperimentManifest, DiagnosisResult, etc.
├── perf-lab/                      ← Spring Boot target service
│   └── src/main/
│       ├── java/.../              ← 3 endpoints + Item entity
│       └── resources/
│           └── application.properties
├── locust/
│   └── locustfile.py              ← read-only during campaigns
├── journals/                      ← experiment manifests (gitignored)
│   └── {campaign_id}/
│       ├── manifest_exp-001.json
│       └── ...
├── docs/
│   ├── DESIGN.md                  ← this file
│   └── ref/
│       └── PERF_AGENT_SPIKE_PLAN.md
├── tests/                         ← inherited + new perf/ tests
├── proofs/                        ← inherited S17Code proofs
└── skills/                        ← inherited
```

---

## Sequence diagram — one experiment

```
User          Orchestrator    DiagnosisEngine    ChangeApplicator    PerfLab    Locust
  │                │                 │                  │               │          │
  │  start()       │                 │                  │               │          │
  │───────────────>│                 │                  │               │          │
  │                │──── run(90s) ──────────────────────────────────────────────-->│
  │                │<─── LocustSummary ──────────────────────────────────────────── │
  │                │──── /actuator/* ──────────────────────────────────>│          │
  │                │<─── MetricsSnapshot ──────────────────────────────-│          │
  │                │──── diagnose(snapshot, history) ─>│                │          │
  │                │<─── DiagnosisResult ──────────────│                │          │
  │<── approval? ──│                                                    │          │
  │──── approved ─>│                                                    │          │
  │                │──── apply(change) ─────────────────────────────── >│          │
  │                │<─── ApplyResult ───────────────────────────────── -│          │
  │                │──── run(90s) ──────────────────────────────────────────────-->│
  │                │<─── LocustSummary ──────────────────────────────────────────── │
  │                │──── write_manifest() ──────────────────────────────│          │
  │<── result ─────│                                                    │          │
```

---


Evaluation design: see docs/EVALUATION.md. Flow diagrams: see docs/FLOW.md

## Week-by-week plan

**Week 1 (Sep 7–13):** Spike + rename + PerfLab + Locust + K1/K3 gates
**Week 2 (Sep 14–20):** DiagnosisEngine + ChangeApplicator + HITL + one full campaign end-to-end
**Week 3 (Sep 21–27):** Experiment history context + scorer + second/third bottleneck type
**Week 4 (Sep 28–Oct 4):** Deploy to cloud + record demo + S20 pitch prep

One week buffer is not assumed. Scope cuts happen at week 3.
