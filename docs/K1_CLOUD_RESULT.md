# K1 (cloud) — Measurement Stability on the Oracle box

**Date:** 2026-09-12
**Purpose:** week 1, item 3 of `DESIGN.md` §16 — re-run K1 on the cloud box,
because the original 14.3% p99 spread came from the local Windows dev machine
and every later "is this change real, or is it noise?" verdict is calibrated
against this number.

**Verdict: PASS on all three criteria. The noise threshold for this
environment is a 2.08% p99 spread.**

---

## 1 · Environment

| | Box A — target | Box B — load generator |
|---|---|---|
| Provider | Oracle Cloud Always Free, Ashburn | same |
| Shape | ARM64 (`aarch64`), **1 core**, 5.8 GB RAM | 1 core |
| Private IP | `10.0.0.79` | `10.0.0.8` |
| Runs | PerfLab `0.2.0` (JVM, not containerised), Postgres 16, Redis 7, go-httpbin | Locust 2.46.4 |
| JDK | OpenJDK 21.0.12 | — |

- Target started as `java -jar perf-lab-0.1.0.jar --spring.profiles.active=lab`
  with `--spring.datasource.hikari.maximum-pool-size` set explicitly per phase.
  The pool size is passed on the command line rather than read from a file so
  the value that was actually in force is recorded, not inferred.
- **Locust runs on a separate box.** `DESIGN.md` §1 requires this: a load
  generator sharing a CPU with the target makes the measured p99 partly about
  the load generator.
- Postgres, Redis and httpbin bind to `127.0.0.1` only. Box A's `:8080` is
  reachable from Box B alone, enforced in two places (Oracle NSG, and host
  iptables).

### Not a controlled comparison with the original K1

Four variables changed at once, so this is **this environment's** noise
threshold and **not** evidence that the cloud box is "more stable than the
laptop". Saying otherwise would be a claim nobody measured.

| | original K1 (2026-09-02) | this run |
|---|---|---|
| Machine | local Windows dev machine, in use for development | Oracle ARM, otherwise idle |
| Database | **H2 in-memory** | **Postgres 16** |
| Load generator | **co-located with the target** | separate box |
| App | perf-lab 0.1.0, 3 endpoints | perf-lab 0.2.0, 8 endpoints |

---

## 2 · Results

All six runs: 50 users, spawn-rate 10, 90 s, `/api/db`, **0 request failures**.

### Baseline — `maximum-pool-size=10`

| run_id | p50 ms | p95 ms | p99 ms | rps | pending peak | active peak | acquire mean ms |
|---|---|---|---|---|---|---|---|
| baseline-01 | 63 | 86 | 98 | 189.3 | 9 | 10 | 11.54 |
| baseline-02 | 62 | 85 | 98 | 188.2 | 8 | 10 | 11.28 |
| baseline-03 | 62 | 86 | 96 | 188.1 | 9 | 10 | 11.15 |

### Bottleneck — `maximum-pool-size=2`

| run_id | p50 ms | p95 ms | p99 ms | rps | pending peak | active peak | acquire mean ms | acquire max ms |
|---|---|---|---|---|---|---|---|---|
| bottleneck-01 | 1100 | 1200 | 1200 | 38.4 | 43 | 2 | 1010.17 | 2178.71 |
| bottleneck-02 | 1100 | 1100 | 1200 | 38.8 | 42 | 2 | 1003.40 | 2172.15 |
| bottleneck-03 | 1100 | 1100 | 1200 | 38.8 | 43 | 2 | 998.12 | 2119.27 |

**Provenance of each column**

- `p50/p95/p99/rps` — Locust's own percentile approximation, from its summary.
- `pending` / `active` peak — `scripts/k1_sample.py`, sampling the gauges once
  per second **during** the run. Raw per-second samples are committed under
  `docs/k1-cloud/`.
- `acquire mean` — delta of the cumulative `hikaricp.connections.acquire` timer
  across the measured window (`TOTAL_TIME` delta / `COUNT` delta), converted
  from seconds to milliseconds.
- `acquire max` — the end-of-window reading. Micrometer's `MAX` is a rolling
  ~2-minute maximum that decays, so it is **not** the maximum over the window.

---

## 3 · Criteria

Same formulae as `docs/K1_RESULT.md`, so the two are directly comparable.

| # | Criterion | Result | |
|---|---|---|---|
| 1 | Baseline p99 spread ≤ 20% | `(98-96)/96 = ` **2.08%** | **PASS** |
| 2 | Bottleneck p99 ≥ 3× baseline p99 median | `1200/98 = ` **12.24×** | **PASS** |
| 3 | Bottleneck p99 internally stable | spread **0%** (1200/1200/1200) | **PASS** |

Supporting: connection-acquire time rises **88.7×** between phases
(11.28 ms → 1003.40 ms, medians), and `pending` rises **4.8×** (9 → 43).

**The new noise threshold is 2.08%.** A measured change smaller than that
cannot be distinguished from run-to-run variation on this box, and assertion
3.5 ("a change inside measurement noise is inconclusive, not a win") should be
calibrated against it.

---

## 4 · Observations worth keeping

**4.1 The bottleneck fixture is reproducible across wildly different hardware.**
`pending` peaked at **43, 42, 43**. The original K1 recorded **43, 42, 43** — the
same three numbers, on a different CPU architecture, a different database, and
with the load generator moved to another machine. The queue depth is set by the
load profile and the pool size, not by the host, which is what makes this a
usable fixture rather than a property of one laptop.

**4.2 The baseline is already saturating its pool, and that was invisible
before.** At `pool=10`, `active` pegged at **10 of 10** and `pending` peaked at
**9**. At 189 rps with a ~50 ms hold, connections needed ≈ `189 × 0.05 ≈ 9.5`
against a pool of 10. So "baseline" is marginal, not comfortable — a later
experiment proposing a pool above 10 would have a real case.

The original K1 recorded baseline `pending` as `0 (post-run)` and therefore
could not see this. That is the K3 failure in miniature: the same measurement,
taken after the pool drained, says the opposite of the truth.

**4.3 Warmup matters more than the noise it would be measured against.** A cold
JVM measured p99 **150 ms**; warm, the same load measured **98–100 ms**. That
gap is ~50%, against a 2.08% noise threshold — so warmup contamination would
swamp the signal entirely. Confirmed independently in `vmstat`: idle went
22% → 58% over the first ~25 s at constant throughput, i.e. the work got cheaper
as JIT compilation completed. Every phase here therefore ran one discarded
warmup before the three measured runs.

**4.4 CPU steal sits at 4–6% under load, against a tripwire set at 5%.**
Idle steal is 0–1%; under load it is consistently 4–6%. `DESIGN.md` §6 aborts a
run when steal exceeds 5%, so **as specified, the week-3 watchdog would abort
most runs on this box.** Either that threshold needs per-environment
calibration — exactly like the noise threshold beside it — or Oracle free tier
cannot host runs that satisfy it. Flagged now rather than discovered in week 3.

**4.5 One core was enough.** Peak utilisation was ~72% (58 us + 15 sy) with the
run queue mostly at 1–2. No resize was needed. This holds only because
`/api/db` spends its time in `Thread.sleep(50)` holding a connection rather
than burning CPU; a CPU-bound fixture would not fit.

---

## 5 · Reproducing this

Procedure, gates and the failures encountered along the way are recorded in
`docs/CLOUD_PROVISIONING.md` §7–§10. The three things that are easy to get
wrong, all of which we hit:

1. **Start load only after the target is ready.** Our first warmup showed 873
   failures (8.34%) purely because Locust began 6 seconds before Tomcat bound
   to 8080. Poll `/actuator/health` *and* wait for the seed log line.
2. **Open the host firewall, not just the cloud one.** Box B got "No route to
   host" instantly — Box A's iptables ends in
   `REJECT --reject-with icmp-host-prohibited`, and SSH worked only because
   port 22 is accepted above it.
3. **Sample gauges during the run.** A post-run reading of `pending` is 0 and
   means nothing.
