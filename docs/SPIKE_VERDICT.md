# Feasibility Spike — Verdict

**Date:** 2026-09-03
**Branch:** `capstone/perf-agent`

## Gates

| Gate | Question | Result |
|---|---|---|
| K1 — measurement stability | Is the load test stable enough to detect a real tuning change? | **PASS** |
| K3 — diagnosis quality | Can one glc_v5 call name the real cause from a metrics snapshot and propose the right fix? | **PASS** |

### K1 — PASS

Three baseline runs (pool=10) held p99 within 14.3% of each other. Injecting the
connection-pool bottleneck (pool=2) raised p99 8.7× (150 ms → 1300 ms), pushed mean
connection-acquire time from ~21 ms to ~1120 ms, and produced a stable mid-run queue of
~42 waiting threads. Bottleneck runs were internally stable (0% p99 spread). Full numbers
in `docs/K1_RESULT.md`.

### K3 — PASS

First probe attempt misdiagnosed the bottleneck as a thread-pool problem — it read the
raw acquire-time metric (which Micrometer reports in seconds) as milliseconds, so a 1-second
wait looked like a 2 ms wait and the pool looked healthy. After the snapshot was corrected
(acquire time converted to ms, mid-run `pending` peak added), the model named the
connection pool with high confidence and proposed raising `maximum-pool-size` from 2 to 20.

That proposal was tested: a fresh Locust run at pool=20 measured p99 **93 ms** (down from
~1300 ms), well under the 200 ms SLA, with mean acquire time 0.11 ms and no pending queue.
All four K3 criteria met. Full detail in `docs/K3_RESULT.md`.

## Three findings that shape the agent build

1. **The collector must sample gauges during load, not after.**
   `hikaricp.connections.pending` and `hikaricp.connections.active` both read 0 once the
   load drains. Post-run polling always shows an empty pool. The load runner needs a
   background sampler that records the mid-run peak of every gauge the diagnosis depends
   on.

2. **The collector must convert units and pre-compute derived values before the LLM
   sees them.** Raw Micrometer timer tuples (`COUNT` / `TOTAL_TIME` / `MAX`, in seconds)
   caused a confident wrong diagnosis. The snapshot should carry `acquire_mean_ms`
   directly, not the raw numbers. The LLM reasons well; it cannot guess units.

3. **The model's `predicted_p99_ms` runs conservative.** It predicted 140 ms; the
   verification run measured 93 ms — an over-estimate of about 1.5×. This is the safe
   direction for a keep/revert decision, but the scorer should treat predicted-vs-measured
   as its own signal, and verification must always be a fresh run of the *actual proposed
   value* — a nearby baseline at a different value (pool=10 sits at ~150 ms, right next to
   the prediction) would have recorded the prediction as accurate by coincidence.

## What the prototype can already do

- Plant a known bottleneck deterministically ✓
- Run a reproducible load profile (fixed Locust params, unmodified `locustfile.py`) ✓
- Collect Micrometer metrics per run, including mid-run gauge peaks ✓
- Send a metrics snapshot to glc_v5 (Gemini, pinned, no failover) and get back valid,
  structured JSON naming the cause and a bounded config change ✓
- Apply the proposed change and re-test to confirm the direction ✓

That is the core loop. The four-week plan builds on it.

## Decision

**Both gates pass. Proceed with Route C.** Submit the S19 form before Fri 2026-09-04 1:00 PM.

## Evidence

- `docs/K1_RESULT.md`, `docs/K3_RESULT.md`
- `locust/results/` — all baseline, bottleneck, `k3-source`, and `k3-verify-01` run JSONs
- `k3_probe/snapshot.json`, `snapshot_k3a.json`, `result_*.json` (all four probe replies,
  including the first wrong one)
- Commit `6fcb929` on `capstone/perf-agent`
