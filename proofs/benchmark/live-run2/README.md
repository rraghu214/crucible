# Live benchmark, run 2 — 1 November 2026

The six manifests behind the live half of the benchmark of record: one Fully Automated
campaign per fault the agent can fix, model `gemini-3.5-flash-lite`, blinded (the model
never saw the fault's name). Write-up: [`docs/BENCHMARK_LIVE_RESULTS.md`](../../../docs/BENCHMARK_LIVE_RESULTS.md).

| Manifest | Fault (true cause) | p99 before → after | Quadrant |
|---|---|---|---|
| `run2-01.json` | pool starved (connection_pool_exhaustion) | 1200 → 59 ms | CORRECT |
| `run2-02.json` | thread starved (thread_pool_saturation) | 510 → 59 ms | CORRECT |
| `run2-03.json` | N+1 query (inefficient_query) | 1800 → 80 ms | LUCKY, then CORRECT |
| `run2-04.json` | cache off (cache_miss) | 46 → 9 ms | CORRECT |
| `run2-05.json` | small heap (gc_pressure) | 2500 → 230 ms | CORRECT |
| `run2-06.json` | small heap and small pool (gc_pressure) | 2500 → 230 ms | CORRECT, CORRECT |

The true cause is in each manifest's `ground_truth` field, written when the run started and
never shown to the model. The scorer reads it to place each experiment in a quadrant.
