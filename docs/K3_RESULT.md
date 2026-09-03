# K3 — Diagnosis Quality

**Date:** 2026-09-03
**Target:** PerfLab (`perf-lab-0.1.0.jar`), H2 in-memory, pool size = 2 (bottleneck injected).
**Model:** `gemini-3.5-flash-lite` via glc_v5, provider `gemini_1`, `auto_route` off, no failover.
**Question K3 asks:** given only a metrics snapshot, can one glc_v5 call name the real
cause (connection pool) and propose the right fix, as valid JSON?

Snapshot: `k3_probe/snapshot.json`
Prompt: `k3_probe/diagnosis_prompt.txt`
Probe script: `k3_probe/run_probe.py`
Raw model replies kept: `k3_probe/result_1788406908.json`, `result_1788410280.json`,
`result_1788411142.json`, `result_1788412001.json`

---

## What happened

We ran the probe three times. The first answer was wrong. The next two were right.

| # | result file | snapshot given | primary cause the model chose | proposed change | confidence | correct? |
|---|---|---|---|---|---|---|
| 1 | `result_1788406908.json` | raw metrics only | Tomcat request thread pool too small | `server.tomcat.threads.max` 200 → 500 | high | **NO** |
| 2 | `result_1788410280.json` | metrics + `hikaricp_derived` (units fixed) | HikariCP pool capped at 2 | `spring.datasource.hikari.maximum-pool-size` 2 → 20 | high | **YES** |
| 3 | `result_1788412001.json` | above + `runtime_config` (K3b) | HikariCP pool constrained | `spring.datasource.hikari.maximum-pool-size` 2 → 20 | high | **YES** |

(`result_1788411142.json` is a repeat of run 2 to check the answer was stable. It was —
same cause, same fix, same numbers.)

---

## Why the first answer was wrong

The model reasoned correctly from bad data.

1. **Unit mistake.** Micrometer reports timer values in **seconds**. The raw snapshot
   showed `hikaricp.connections.acquire` as `TOTAL_TIME: 3499`, `MAX: 2.4`. The model
   read those as milliseconds. In milliseconds, a 2.4 ms connection wait looks perfectly
   healthy, so the model decided the pool was fine. The true mean wait was
   `3499 / 3186 = 1098 ms` — over one second per request.

2. **Drained gauge.** `hikaricp.connections.pending` is an instantaneous gauge. The
   snapshot was taken after the load test finished, so it read `0`. The model saw
   "0 requests waiting for a connection" and crossed the pool off its list. During the
   load test the real queue held ~43 (from K1).

With the pool ruled out on both counts, the model picked the next plausible story:
"latency is high, so the web server must be starved of threads." It proposed raising the
Tomcat thread pool — the wrong knob — with high confidence.

This is the same lesson as K1 and as S18: a clean-looking zero and a
never-measured zero are not the same thing, and the LLM cannot tell them apart.

---

## The fix

We did **not** change the prompt. We enriched the snapshot so the numbers meant what
they looked like. Added one block:

```json
"hikaricp_derived": {
  "note": "acquire Timer is in SECONDS — mean = TOTAL_TIME/COUNT",
  "acquire_mean_ms": 1098,
  "acquire_max_ms": 2406,
  "pending_peak_during_load": 43
}
```

- converted the acquire timer to milliseconds and did the division for the model
- carried the mid-run peak of `pending` (43) in from the K1 run artifacts
- also corrected `locust_summary.rps` (was a wrong 1400, real value ~35) and filled in `p99_ms`

On the next run the model saw a 1098 ms average connection wait and a queue of 43, and
named the pool immediately.

---

## K3b — does giving the model the config help?

K3b adds a `runtime_config` block to the **same** snapshot (no metric re-pull), listing
the seven tunable properties and their current values pulled from `/actuator/env`:

```json
"runtime_config": {
  "spring.datasource.hikari.maximum-pool-size": "2",
  "spring.datasource.hikari.minimum-idle": "1",
  "spring.datasource.hikari.connection-timeout": "30000",
  "server.tomcat.threads.max": "200",
  "server.tomcat.threads.min-spare": "10",
  "spring.task.execution.pool.core-size": "8",
  "spring.task.execution.pool.max-size": "16"
}
```

| | K3a (metrics only) | K3b (+ runtime_config) |
|---|---|---|
| cause | pool capped at 2 | pool capped at 2 |
| confidence | high | high (no change) |
| proposed change | `maximum-pool-size` 2 → 20 | `maximum-pool-size` 2 → 20 |
| predicted p99 | 140 ms | 140 ms |
| cost | $0.00186 | $0.00186 |
| first piece of evidence cited | `hikaricp.connections_max.VALUE: 2.0` (a metric) | `spring.datasource.hikari.maximum-pool-size: 2.0` (the property) |

**What K3b did not do:** raise confidence or change the answer. The fixed-up metrics
snapshot already had everything the model needed. The thing that rescued K3 was the unit
conversion, not the config.

**What K3b did do:** the model now points at the exact property name it wants changed,
instead of guessing the property name from a metric name. That is useful later — the
diagnosis output already speaks the same language as the change applicator's allowed-list
(DESIGN.md §5), so there is no name-translation step that could go wrong.

**Side note (Spring Boot quirk):** `/actuator/env` hides **every** value as `******` by
default in Spring Boot 3, not just passwords. K3b only worked after starting PerfLab with
`--management.endpoint.env.show-values=ALWAYS`. The real collector will need that line in
PerfLab config, or it should read the pool size from the Micrometer gauge
(`hikaricp.connections.max`), which is never hidden.

---

## Ground truth

- Planted cause: connection pool exhaustion (`maximum-pool-size=2`).
- Expected diagnosis: connection pool / HikariCP / connection wait.
- Expected fix: increase `maximum-pool-size`.
- Model delivered all three (after the snapshot fix).

---

## K3 PASS criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Reply parses as valid JSON | **PASS** (`parse_ok: true` on every run) |
| 2 | `primary_cause` names connection pool / HikariCP / connection wait, not GC/memory/threads | **PASS** (runs 2 and 3) |
| 3 | `proposed_change.parameter` contains `pool-size` | **PASS** — `spring.datasource.hikari.maximum-pool-size` |
| 4 | Verification load test shows p99 improves after the change | **PASS** — measured, see below |

### Criterion 4 — verification

The model proposed `maximum-pool-size = 20`. That exact value was tested: PerfLab was
restarted with pool=20 and one Locust campaign (`RUN_ID=k3-verify-01`, same profile as
every other run — 50 users, spawn-rate 10, 90 s, `/api/db`) was run against it.

| state | pool size | p50 | p95 | p99 | rps | mean acquire | source |
|---|---|---|---|---|---|---|---|
| bottleneck (before) | 2 | ~1200 ms | ~1300 ms | ~1300 ms | ~35 | 1098 ms | `locust/results/bottleneck-0{1,2,3}.json` |
| verification (after) | 20 | 63 ms | 78 ms | **93 ms** | 186 | 0.11 ms | `locust/results/k3-verify-01.json`, `_hikari.json` |

p99 dropped from ~1300 ms to **93 ms** — about 14× — and mean connection-acquire time
fell from 1098 ms to 0.11 ms. `hikaricp.connections.pending` sat at 0 during the run: at
pool=20 the pool never queues. 0 request failures. The direction of the model's proposal
is confirmed by a test of the proposal itself, and the SLA (p99 ≤ 200 ms) is now met with
wide margin.

**Calibration of `predicted_p99_ms`:** the model predicted **140 ms**; measured p99 at
pool=20 was **93 ms**. The model was conservative — it over-estimated post-change p99 by
about 1.5×. That is a safe direction to be wrong in for an SLA prediction, but it is not
precise, and it is worth noting *why* the number looked better than it was: the K1
pool=10 baseline is ~150 ms, so if that had been used as the "after" figure the prediction
would have looked near-perfect. It was a coincidence. The real proposal (pool=20) does not
saturate the pool at this load at all, so p99 lands well below the pool=10 figure.

**Supporting context (not the verification):** the K1 baseline runs at pool=10 gave p99
~150 ms. Those runs predate the diagnosis and test a different pool size, so they are
corroborating evidence for "a bigger pool helps", not a test of the pool=20 proposal.

---

## Verdict

```
K3: PASS
Reason: After the metrics snapshot was corrected for units (acquire timer is in
        seconds) and given the mid-run pending peak, one glc_v5 call named the
        connection pool as the cause and proposed raising maximum-pool-size from
        2 to 20, as valid JSON, with high confidence. Repeated twice with the
        same result. The proposal was then tested at pool=20: p99 fell from
        ~1300 ms to 93 ms, meeting the SLA.
Blocker (if FAIL): none
All four PASS criteria met (criterion 4 by a measured pool=20 run, not adjacent evidence).
Attempt count: 2 (first attempt misdiagnosed due to a unit error in the snapshot,
        not the prompt)
```

---

## Findings for the agent build

1. **The collector must convert units and pre-compute derived values before the LLM
   sees them.** Never hand raw Micrometer timer tuples (`COUNT` / `TOTAL_TIME` / `MAX`
   in seconds) to the model. Give it `acquire_mean_ms` directly. A wrong unit produced a
   confident wrong diagnosis.

2. **Gauges must be sampled during load, not after.** `hikaricp.connections.pending`
   *and* `hikaricp.connections.active` both read 0 once the load drains (confirmed again
   in the pool=20 verification run). The load runner needs a background sampler that
   records the peak of every gauge it cares about, not just `pending`. (Same finding as
   K1, now confirmed to actually break the diagnosis.)

3. **Runtime config is worth including**, not for accuracy but for precision: the
   proposed change comes back as a real property name that the applicator can act on
   directly.

4. **`/actuator/env` needs `show-values=ALWAYS` in PerfLab**, or the collector should
   take config values from Micrometer gauges instead.

5. **The model's `predicted_p99_ms` runs conservative.** Predicted 140 ms, measured
   93 ms — over-estimated by ~1.5×. For the campaign's keep/revert decision this is the
   safe direction (it will not claim success it did not get), but the scorer should
   compare predicted vs measured as its own signal rather than trusting the prediction.
   Verification must always be a fresh run of the *actual proposed value* — an
   adjacent baseline at a different value would have hidden this.
