# Advisory run — `run-1793613354320-fully_auto_burst_100u`

2 November 2026, 09:55 UTC, launched from the dashboard (the scenario keeps the name it was
saved under). Target PerfLab on Box A, `/api/db`, 100 users, model `gemini-3.5-flash-lite`.

| Step | What happened | Where to see it |
|---|---|---|
| Baseline | p99 **2500 ms** at 38 requests/s | `manifest.json` → `baseline`, `locust/baseline_stats.csv` |
| Proposals | One ranked proposal: `connection_pool_exhaustion`, `maximum-pool-size` 2 → 20, predicted p99 100 ms, with the exact diff | `experiments[0].advisory.alternatives` |
| Applied | Nothing. No commit, no deploy, no second measurement | `apply_result`, `deploy` are empty |
| Verdict | **NOT_MEASURED**: an unapplied proposal never looks like a verified one | `experiments[0].verdict` |

Advisory mode needs no repository or deploy pipeline. The operator applies a proposal by
hand, then runs again to measure it. `audit.jsonl` is empty because nothing was applied.
