# AGENTS.md — Crucible
## Read this file before writing a single line of code.

---

## What this project is

Crucible is an autonomous performance engineering agent. It runs controlled load
experiments against a Spring Boot target service, reads its own Micrometer telemetry,
uses an LLM to diagnose why a latency SLA is being missed, proposes one bounded
configuration change under human approval, re-tests, and keeps or reverts on measured
evidence. Every experiment produces a manifest. The scorer runs separately from the
runner. A verified pass and an unverified pass must never look identical in the results.

This is EAGv3 Capstone — Route C. Four weeks. One clear deliverable.

---

## Codebase origin

Crucible is built on top of S17Code (Session 17, EAGv3). The agent runtime,
economics layer, events engine, HITL, telemetry, and gateway seam are inherited.
Do not rewrite them. Extend them where needed; leave them alone where not.

Reference: `C:\Raghu\MyLearnings\EAG_V3\S17-15082026\assignment\S17Code\`

---

## Architecture — four components

```
┌─────────────────────────────────────────────────────┐
│                   Crucible Agent Loop               │
│  (crucible/  — extends S17Code runtime)             │
│                                                     │
│  Orchestrator → Plan experiment                     │
│              → Run load via Locust                  │
│              → Collect Micrometer snapshot          │
│              → Diagnose via LLM (glc_v5)            │
│              → Propose change (HITL approval)       │
│              → Apply change to PerfLab              │
│              → Re-run → compare → keep/revert       │
│              → Write manifest + journal             │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
┌─────────▼────────┐   ┌────────────▼──────────┐
│   PerfLab        │   │   glc_v5 Gateway      │
│   (Spring Boot)  │   │   port 8111           │
│   port 8080      │   │   provider: gemini    │
│   Micrometer +   │   │   PINNED — no failover│
│   Actuator       │   └───────────────────────┘
│   HikariCP pool  │
│   H2 / Postgres  │
└──────────────────┘
         │
┌────────▼─────────┐
│   Locust         │
│   load generator │
│   runs from      │
│   control host   │
└──────────────────┘
```

---

## Environment variables

All Crucible variables are prefixed `CRUCIBLE_`. GLC variables stay as-is.

```
GLC_BASE_URL=http://127.0.0.1:8111          # do not change
CRUCIBLE_GATEWAY_PROVIDER=gemini            # PINNED — never change mid-campaign
CRUCIBLE_PORT=8113
CRUCIBLE_A2A_GRPC_PORT=8114
CRUCIBLE_SANDBOX_ROOT=/path/to/perf-lab     # the Spring Boot app Crucible may edit
CRUCIBLE_WORKSPACE=/path/to/perf-lab        # same as SANDBOX_ROOT
CRUCIBLE_PROTECTED_PATHS=tests/**,conftest.py,locust/**,docs/**
CRUCIBLE_ALLOWED_COMMANDS=mvn,gradle,java,python,locust,curl,git
CRUCIBLE_MAX_REPEAT_FAILURES=3
CRUCIBLE_CONTROL_TOKEN=replace-with-token
CRUCIBLE_COMPLETION_TOKEN=replace-with-token
CRUCIBLE_SELF_ACTORS=crucible
CRUCIBLE_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces
CRUCIBLE_OTEL_SERVICE_NAME=crucible

# PerfLab target
PERFLAB_BASE_URL=http://localhost:8080
PERFLAB_ACTUATOR_URL=http://localhost:8080/actuator
PERFLAB_SLA_P99_MS=200
```

---

## Model pinning — non-negotiable

Every LLM call goes through glc_v5 with `"provider": "gemini"` and
`"auto_route": false`. Do not enable failover. Cross-provider failover changes
the model family mid-campaign, invalidating experiment-to-experiment comparison.
This is the same lesson as S18's controlled-comparison discipline: one campaign,
one model.

If a Gemini key is exhausted, the campaign pauses. It does not switch to Groq.

---

## Experiment manifest schema

Every experiment writes one manifest before scoring. The scorer reads the
manifest; it never re-runs the experiment.

```json
{
  "experiment_id": "exp-001",
  "campaign_id": "campaign-2026-09-08",
  "timestamp": 1725782400.0,
  "hypothesis": "Connection pool exhausted under concurrent load",
  "change": {
    "parameter": "spring.datasource.hikari.maximum-pool-size",
    "from": "2",
    "to": "10",
    "rationale": "hikaricp.connections.pending nonzero; acquire time high"
  },
  "load_profile": {
    "users": 50,
    "spawn_rate": 10,
    "duration_seconds": 90,
    "endpoint": "/api/db"
  },
  "before": {
    "p50_ms": 45,
    "p95_ms": 890,
    "p99_ms": 1840,
    "hikari_pending": 12,
    "hikari_acquire_mean_ms": 380
  },
  "after": {
    "p50_ms": 42,
    "p95_ms": 195,
    "p99_ms": 210,
    "hikari_pending": 0,
    "hikari_acquire_mean_ms": 2
  },
  "sla_p99_ms": 200,
  "verdict": "KEPT",
  "verified": true,
  "cost_usd": 0.0012,
  "duration_seconds": 287
}
```

Verdicts: `KEPT` | `REVERTED` | `INCONCLUSIVE` | `ABORTED`
`verified: true` requires the agent ran the post-change load test itself.
`verified: false` means it proposed but did not re-test. Score them differently.

---

## Protected paths — the agent must never touch these

```
locust/locustfile.py          # load profile is fixed per campaign
docs/                         # documentation is not a tuning target
.env                          # credentials
tests/                        # test suite
pyproject.toml                # build config
AGENTS.md                     # this file
```

The agent may only modify files under `CRUCIBLE_SANDBOX_ROOT` (the PerfLab
Spring Boot app). It may not modify the Locust load profile during a campaign —
the load must stay constant for comparisons to be valid.

---

## Scope — what is in and what is out

**In scope (four weeks):**
- One Spring Boot target service (PerfLab) with H2, three endpoints
- Three planted bottlenecks: connection pool, GC/allocation, thread pool
- Micrometer/Actuator as the sole metrics source (no Datadog, no APM)
- Locust as load generator (runs from control host, not the target)
- One campaign = one hypothesis, one change, one re-test
- Experiment history as context for subsequent campaigns
- Human approval gate before any change is applied
- NiceGUI dashboard showing campaign state (optional, week 3 if time allows)
- Deploy on Oracle Always Free (Mumbai) or Hetzner CX32

**Out of scope:**
- Multiple target services
- Source code rewrites (configuration changes only)
- Kubernetes, Docker Compose in production
- Datadog, New Relic, any commercial APM
- LangChain, CrewAI, AutoGen (Route C rule: no third-party harness)
- Prometheus scraping (Actuator JSON pull is sufficient)
- Auto-discovery of endpoints (endpoints are declared in campaign config)

---

## Scoring — adapted from S18 discipline

Each experiment is scored on four fields:

| Field | Description |
|---|---|
| `outcome` | KEPT / REVERTED / INCONCLUSIVE / ABORTED |
| `integrity` | Did the agent touch only permitted paths? |
| `verified` | Did the agent run its own post-change load test? |
| `cost_usd` | Total LLM spend for this experiment |

The scorer is a separate script. It reads manifests from the journal directory.
Changing the scoring weights must not require re-running experiments.

---

## Development conventions

- One-bug-one-PR discipline. Adjacent findings get their own PRs.
- `capstone/perf-agent` is the working branch. `main` stays clean as S17Code baseline.
- Gemini free tier, 5 keys via glc_v5. Check `gemini-limits.xlsx` before long runs.
- Add 2–3s delay between LLM calls in any loop. Throttle lesson from S18.
- Phase-based Claude Code sessions with `/clear` between phases.
  Reference files by path. Do not paste file contents into the prompt.
- Run `uv run pytest -q` and `uv run ruff check .` before every commit.
- All session 20 pitch claims must be traceable to a manifest in `journals/`.

---

## Feasibility gate (pre-capstone spike)

Before any agent code is written, two kill tests must pass:

**K1 — Measurement stability**
Three identical Locust runs at pool=10 must show p99 spread ≤ 20%.
Three runs at pool=2 must show p99 ≥ 3× baseline median.
`hikaricp.connections.pending` must be nonzero in all bottleneck runs.

**K3 — Diagnosis quality**
A single glc_v5 call with a bottleneck metrics snapshot must return valid JSON
naming connection pool / HikariCP as the primary cause.

Spike plan: `docs/ref/PERF_AGENT_SPIKE_PLAN.md`

---

## Key course references (do not add to codebase)

```
C:\Raghu\MyLearnings\EAG_V3\S17-15082026\  ← S17Code baseline (read only)
C:\Raghu\MyLearnings\EAG_V3\S18-22082026\  ← S18 benchmark patterns
C:\Raghu\MyLearnings\EAG_V3\S19-29082026\  ← Capstone brief + ideas (reference only)
glc_v5 running on port 8111                 ← do not reconfigure
```
