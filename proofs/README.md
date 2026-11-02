# Proofs

Real evidence from real runs, copied from the runner box (Box B) on 3 November 2026.
Nothing here is edited except one thing: the model gateway's hostname is replaced
with `<your-gateway>` in the job logs, because that gateway accepts unauthenticated
requests.

## Latest run of each type

| Folder | Run | What it shows |
|---|---|---|
| [`runs/semi-automated/`](runs/semi-automated/) | `run-1793610533986`, 2 Nov 2026 | p99 2500 → 68 ms. The operator approved the exact diff, paused, raised the goal mid-run and resumed; the edit is recorded as an amendment |
| [`runs/fully-automated/`](runs/fully-automated/) | `run-1793611818995`, 2 Nov 2026 | p99 2500 → 100 ms with no human prompt; recorded as `preapproved` |
| [`runs/advisory/`](runs/advisory/) | `run-1793613354320`, 2 Nov 2026 | One ranked proposal with its exact diff. Nothing applied, deployed or measured: NOT_MEASURED |
| [`runs/ceiling-probe/`](runs/ceiling-probe/) | none yet | The folder for the first ceiling probe |

Each run folder holds:

| File | What it is |
|---|---|
| `manifest.json` | The run's full record: goal, baseline, diagnosis, change, approval, commit deployed and the commit the target reported, measurement after, verdict, model |
| `jobs/<id>/output.log` | The dashboard job's log, exactly as the Jobs page showed it |
| `jobs/<id>/job.json` | The exact `crucible` command the dashboard ran |
| `locust/*_stats.csv` | Locust's own statistics for each measured window (warmups are discarded and not kept) |
| `audit.jsonl` | The append-only audit lines for this run: approvals and applies |
| `approvals/` | The parked proposal (`.request.json`) and the operator's decision (`.decision.json`): the human gate, on disk |

## Benchmark of record

[`benchmark/live-run2/`](benchmark/live-run2/) holds the six manifests of the live benchmark
(run 2, 1 November 2026): 8 experiments, CORRECT 7, LUCKY 1. The replay evidence (40 cases)
is [`docs/bench/replay-2026-11-01-run2.json`](../docs/bench/replay-2026-11-01-run2.json), which
the dashboard's Benchmarks page reads.

## What else is here

The S17Code harness's own proofs, kept because CI or the tests use them:

| File | Used by |
|---|---|
| `harness.py`, `p2_budget_holds.py`, `p3_denial_of_wallet.py`, `p4_trace_export.py` | CI (`.github/workflows/ci.yml`) |
| `channel_prompts.py` | `tests/test_channel_prompt_catalog.py` |
| `p1_cost_per_task.py` | `tests/test_p1_strategies.py` |
| `tasks/autonomous_20.jsonl`, `tasks/mixed.jsonl`, `pairs/paraphrases.jsonl` | the evals tests |
| `tasks/perf_replay_v1.yaml` | `tests/test_perf_replay.py` (Crucible's first replay task set) |

The rest of the inherited proofs were moved to the operator's archive on 3 November 2026.
