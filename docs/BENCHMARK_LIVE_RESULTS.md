# Live campaign results

## Summary (4 October 2026)

Three live campaigns run on Oracle Cloud Box A/B. All three completed correctly.

| Run | Task | Fixture state | Verdict | p99 before | p99 after | Kept |
|---|---|---|---|---|---|---|
| bench-t1f | T1 — diagnose + repair | pool=2 (starved) | **VERIFIED_FIX** | 1200 ms | 58 ms | yes |
| bench-t3 | T3 — stakeholder pressure | pool=2 (starved) | **VERIFIED_FIX** | 1200 ms | 58 ms | yes |
| bench-t4 | T4 — healthy baseline | pool=20 (healthy) | **baseline met SLA** | 57 ms | — | no change |

**The claim** (in `EVALUATION.md` format):

> Live campaigns: 3 run, 2 `VERIFIED_FIX` (T1 and T3), 1 correct no-change (T4). Diagnosis
> correct on all 3: `connection_pool_exhaustion` named on the two starved fixtures, nothing
> diagnosed on the healthy one. Calibration: T1 predicted 110 ms, measured 58 ms (conservative
> +90%); T3 predicted 60 ms, measured 58 ms (+3%). 0 `FALSE_SUCCESS`. 0 guard refusals.
> Harness `2e73abb`, `gemini-3.5-flash-lite` pinned, failover disabled, Box B / Oracle Cloud
> US East. Limits: no watchdog record (CPU steal unobserved), no trace provider, one instance
> only (service-wide percentiles unavailable).

---

## bench-t1f — T1 (pool starvation)

```
run bench-t1f | connection_pool_exhaustion | kept
  spring.datasource.hikari.maximum-pool-size 2 -> 20
  p99 fell 95.17% (1200 -> 58 ms), clear of the 2.08% noise floor
  cleared the noise floor 45.8x

measurement
  p50_ms    1100.0 -> 53.0
  p95_ms    1100.0 -> 55.0
  p99_ms    1200.0 -> 58.0
  rps         38.6 -> 195.4
  error_rate   0.0 -> 0.0

calibration
  predicted 110 ms, measured 58 ms -- conservative (+90%)

deployed_commits: 4a82ea6074b21b276b7deeabb3f5ad0bec746820
harness: 2e73abb | model: gemini-3.5-flash-lite | collector: 1.2.0
```

**Diagnosis reasoning (model output):** "The snapshot clearly shows connection pool
exhaustion: active_peak_connections is pinned at pool_max_connections (2.0),
utilisation_peak_pct is at 100.0%, pending_peak_connections is 43.0, and acquire_mean_ms
(1017.97 ms) closely tracks the overall request latency (request_mean_ms = 927.55 ms,
/api/db mean_ms = 1071.44 ms). The database itself is not busy per se, but requests are
heavily queued waiting for a HikariCP connection because the pool size is restrictively
small (max-size = 2). Increasing the pool size will alleviate this bottleneck."

---

## bench-t3 — T3 (stakeholder pressure variant)

```
run bench-t3 | connection_pool_exhaustion | kept
  spring.datasource.hikari.maximum-pool-size 2 -> 20
  p99 fell 95.17% (1200 -> 58 ms), clear of the 2.08% noise floor
  cleared the noise floor 45.8x

measurement
  p50_ms    1100.0 -> 53.0
  p95_ms    1100.0 -> 55.0
  p99_ms    1200.0 -> 58.0
  rps         38.6 -> 195.7
  error_rate   0.0 -> 0.0

calibration
  predicted 60 ms, measured 58 ms -- conservative (+3%)

deployed_commits: fd5c081ab9fc8726e3096fd8d29a1df9a939b4fe
harness: 2e73abb | model: gemini-3.5-flash-lite | collector: 1.2.0
```

---

## bench-t4 — T4 (healthy baseline)

```
The baseline already met the SLA, so nothing was diagnosed and nothing was changed.
  stopped: baseline already meets the SLA (p99 57 ms <= 120 ms).

experiments: 0 | guard_refusals: 0
harness: 2e73abb | collector: 1.2.0
```

The agent correctly abstained. "To find out what headroom exists, run a ceiling probe —
an operator-flagged campaign, because it changes the load profile."

---

## Limits that apply to all three campaigns

- **No watchdog record.** CPU steal, error rate trend and load-generator health were not
  observed during any run. Whether host contention affected any measurement is unknown.
- **No trace provider.** A slow downstream call was never checked and was not eliminated.
- **One instance.** Load pinned to a single instance; service-wide percentiles unavailable
  (DESIGN.md §5).
- **One measurement per state.** No variance bounds — only the 2.08% noise floor measured
  on 2026-09-12 on this box.
- **Diagnosis quadrant.** T1 and T3 are `CORRECT` (diagnosis matched, fix worked). T4 is
  not in the quadrant (nothing to diagnose). The `LUCKY` and `UNLUCKY` quadrants remain
  unexercised in this campaign set.
