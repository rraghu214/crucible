# Fully Automated run — `run-1793611818995-fully_auto_burst_100u`

2 November 2026, 09:30 UTC, launched from the dashboard. Target PerfLab on Box A, `/api/db`,
100 users, 180 s measured windows, goal p99 ≤ 120 ms, model `gemini-3.5-flash-lite`.

| Step | What happened | Where to see it |
|---|---|---|
| Baseline | p99 **2500 ms** at 38 requests/s | `manifest.json` → `baseline`, `locust/baseline_stats.csv` |
| Diagnosis | `connection_pool_exhaustion`; predicted p99 110 ms | `experiments[0].cause_family`, `predicted_p99_ms` |
| Approval | None asked. The change was inside the profile's bounds, so it was recorded as `preapproved` | `experiments[0].approval`, `audit.jsonl` |
| Change | `spring.datasource.hikari.maximum-pool-size` 2 → 20 | `experiments[0].apply_result` |
| Deploy | Commit `46ca47c` pushed; the target reported the same commit before anything was measured | `deployed_commit`, `deploy.observed_commit` |
| Verdict | p99 **100 ms**, 46.2× the noise floor: IMPROVED, kept | `experiments[0].after`, `locust/exp01_stats.csv` |

No human was asked, and nothing else was relaxed: the guard, the bounds and the measured
verdict decided exactly as in Semi-Automated mode.
