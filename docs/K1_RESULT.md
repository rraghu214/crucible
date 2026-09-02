# K1 — Measurement Stability

**Date:** 2026-09-02
**Target:** PerfLab (`perf-lab-0.1.0.jar`), H2 in-memory, `/api/db` with `@Transactional` + `Thread.sleep(50)` connection hold.
**Load:** Locust 2.46.4, 50 users, spawn-rate 10, 90s, `locust/locustfile.py` (unmodified spike profile).
**Runner:** each PerfLab instance launched with an explicit `--spring.datasource.hikari.maximum-pool-size` / `--minimum-idle` override; all three baseline runs share one instance, all three bottleneck runs share a second instance. App not restarted between runs of the same phase.

## Results

| run_id        | p50 ms | p95 ms | p99 ms | rps   | hikari.pending        | hikari.acquire_mean ms |
|---------------|--------|--------|--------|-------|-----------------------|------------------------|
| baseline-01   | 89     | 120    | 140    | 169.9 | 0 (post-run)          | 21.1                   |
| baseline-02   | 89     | 130    | 150    | 167.1 | 0 (post-run)          | 20.7                   |
| baseline-03   | 89     | 130    | 160    | 169.9 | 0 (post-run)          | 20.9                   |
| bottleneck-01 | 1200   | 1300   | 1300   | 34.8  | 43 (peak, mid-run)    | 1103                   |
| bottleneck-02 | 1200   | 1300   | 1300   | 34.9  | 42 (peak, mid-run)    | 1133                   |
| bottleneck-03 | 1200   | 1300   | 1300   | 35.0  | 43 (peak, mid-run)    | 1124                   |

- `p50/p95/p99/rps` — from `locust/results/<run_id>.json` (Locust's own percentile approximation).
- `hikari.acquire_mean` — per-run delta of the cumulative `hikaricp.connections.acquire` timer
  (`TOTAL_TIME` delta / `COUNT` delta), from `locust/results/<run_id>_hikari.json`.
- `hikari.pending` — instantaneous gauge. Baseline: only sampled after the run (load already
  drained → 0). Bottleneck: sampled every 5s during the run; value shown is the peak.
- 0 request failures in all six runs (30s `connection-timeout` absorbs the queue wait).

## K1 PASS criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Baseline p99 spread ≤ 20%: `(160-140)/140 = 14.3%` | **PASS** |
| 2 | Bottleneck p99 ≥ 3× baseline p99 median: `1300 / 150 = 8.7×` | **PASS** |
| 3 | Bottleneck p99 internally stable: spread `0%` (1300/1300/1300) | **PASS** |
| 4 | `hikaricp.connections.pending > 0` in all bottleneck runs: 43 / 42 / 43 | **PASS** |

## Verdict

```
K1: PASS
Reason: Baseline p99 holds within 14.3% across three runs; the pool=2 bottleneck
        raises p99 8.7× with connection-acquire time up ~53× and a stable
        mid-run pending queue of ~42, confirming the symptom is connection wait.
Blocker (if FAIL): none
```

## Notes for K2/K3

- Baseline is **not** perfectly idle: acquire_mean ~21 ms and rps ~170 against a
  ~200 rps ceiling (pool=10 / 50 ms hold) mean the healthy state already has mild
  pool queueing. This does not threaten K1 (all four criteria pass with margin)
  and is arguably realistic, but the K3 snapshot's `baseline_reference` should use
  these real numbers, not an assumed ~50 ms floor.
- `hikaricp.connections.pending` is a gauge — it must be scraped *during* load, not
  after. The agent's collector needs to sample mid-run or the bottleneck signal
  reads as 0.
- Bottleneck `hikari.acquire` MAX hit 2.36 s; `http.server.requests` MAX 2.43 s.
