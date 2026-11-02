# Semi-Automated run — `run-1793610533986-burst_100u`

2 November 2026, 09:08 UTC, launched from the dashboard. Target PerfLab on Box A, `/api/db`,
100 users, 180 s measured windows, model `gemini-3.5-flash-lite`.

| Step | What happened | Where to see it |
|---|---|---|
| Baseline | p99 **2500 ms** at 38 requests/s | `manifest.json` → `baseline`, `locust/baseline_stats.csv` |
| Memory | The agent was reminded of 3 earlier attempts on this target | `experiments[0].prior_findings` |
| Diagnosis | `connection_pool_exhaustion`; predicted p99 70 ms | `experiments[0].cause_family`, `predicted_p99_ms` |
| Proposal | `spring.datasource.hikari.maximum-pool-size` 2 → 50 | `approvals/001.request.json` |
| Approval | The operator approved that exact diff from the dashboard | `approvals/001.decision.json`, `audit.jsonl` |
| Mid-run edit | The operator paused, raised the goal from 120 to 150 ms, and resumed; recorded as an amendment | `amendments`, `jobs/…091141…` (pause), `jobs/…091557…` (resume) |
| Deploy | Commit `3a2029b` pushed; the target reported the same commit before anything was measured | `experiments[0].deployed_commit`, `deploy.observed_commit` |
| Verdict | p99 **68 ms**, 46.8× the 2.08% noise floor: IMPROVED, kept | `experiments[0].after`, `locust/exp01_stats.csv` |

The prediction (70 ms) is recorded for calibration only. The verdict uses the measurement.
