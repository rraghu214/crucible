# Overnight work — 1 November 2026

Written by Claude Code while you were away (about 02:30–08:30 IST). Everything
below is on the **draft branch `capstone/live-quadrants`**. `capstone/perf-agent`
has only one docs commit (`75c8e12`, the trial checklist). Nothing merges until
you've reviewed Groups 46 and 47.

## What changed

| | Where | Status |
|---|---|---|
| Live runs record their true cause (`crucible run --fixture`) | Crucible, draft branch | Group 46, **NOT YET REVIEWED** |
| Scorer and Benchmarks quadrant read it | Crucible, draft branch | Group 46 |
| GC heap lever `perflab.jvm.heap-max-mb` (128–1536 MB) | profile, SKILL.md, fixtures, T2/T12 | Group 46 |
| Box A deploy hook reads the heap line, passes `-Xmx` | `scripts/box_a_post_receive.sh`; **installed on Box A** (old hook kept as `~/post-receive.bak-20261101`) | Group 46 |
| One SLA per live benchmark fixture | `config/bench/`, protected | Group 46; **targets chosen by me, please check** |
| Library and Overview name the service: `perf-lab`, `perf-lab-py` | profiles, `/v1/perf/services`, dashboard | Group 47, **NOT YET REVIEWED** |
| Trial checklist: steps 8–14 still to run, plus a Fully Automated step | `docs/myfolder/trial-run-31102026.md` | on `capstone/perf-agent` |

## Live campaigns (all Fully Automated, `--approve preapproved`)

**Not a valid measure of diagnosis (found 1 November 2026).** These runs' SLA was named
after the fault (`bench-cache-miss-severe`), as were the SLA file and the run id, and
the model saw all three; two replies quote the name. Each run could also read the
earlier runs' findings in the journal. Re-run blinded (Group 46.6), with neutral ids
and an empty journal per run: results to follow. Kept as recorded.

Run from a separate worktree on Box B (`~/crucible-draft`), so the public dashboard
stayed on approved code. Manifests are in `~/crucible-draft/results/bench46-*.json`;
the runner's log is `~/bench46/run.log`.

| Fixture (true cause) | p99 before → after (SLA) | What the agent did | Quadrant | Spend |
|---|---|---|---|---|
| `pool_starved` (connection_pool_exhaustion) | 1200 → **59 ms** (120) | pool 2 → 20, IMPROVED, kept | CORRECT | $0.0013 |
| `thread_starved`, Tomcat metrics **off** (thread_pool_saturation) | 510 → — (120) | declined: thread gauges were null | abstained (not scored) | $0.0013 |
| `thread_starved`, Tomcat metrics **on** | 510 → **59 ms** (120) | threads.max 4 → 200, IMPROVED, kept | CORRECT | $0.0013 |
| `inefficient_query_severe` (inefficient_query) | 1600 → **79 ms** (400) | exp 1: pool → 50, INCONCLUSIVE, reverted; exp 2: batch fetch → 50, IMPROVED, kept | WRONG, then CORRECT | $0.0028 |
| `cache_miss_severe` (cache_miss) | 46 → **8 ms** (20) | cache back on, IMPROVED, kept | CORRECT | $0.0013 |
| `gc_pressure` (gc_pressure) | 2000 → **220 ms** (300) | heap 128 → 1024 MB (the new lever), IMPROVED, kept | CORRECT | $0.0014 |
| `gc_and_pool`, lookalike (gc_pressure) | 2300 → **220 ms** (300) | heap 128 → 1536 MB; the pool (the trap) untouched | CORRECT | $0.0014 |

**Quadrants over 7 scored experiments: CORRECT 6, LUCKY 0, UNLUCKY 0, WRONG 1**, plus one
abstention. The six runs where the agent acted met their SLA; the abstained run (Tomcat
metrics off) did not, since nothing was changed. (Corrected 1 November 2026: the first
version of this note said CORRECT 7 over 8 and "every SLA was met", counting the abstention
as scored.) Total model spend $0.0107, one model throughout
(`gemini-3.5-flash-lite`), every change approved as `preapproved` (Fully Automated).
LUCKY is 0 measured over 7, not "not measured": the GC-plus-pool lookalike was the run
built to tempt it, and the agent took the heap, not the pool.

### Replay, after re-capturing the changed fixtures

`perflab_gc_pressure` (Actuator, Datadog) and both `thread_starved` fixtures were
re-captured, because their old snapshots carried the old GC spec or no thread
metrics, and the bench correctly refused to grade against them. PromQL could not be
re-captured (corrected 1 November 2026: Prometheus was running; the capture script
never passed `--promql-url`, so the capture had no address); the stale PromQL snapshot was
dropped, not reused. The refreshed set is in `~/crucible-draft/fixtures-46/`, and
`~/crucible/results/` is unchanged.

| | 4 October (v2) | 1 November |
|---|---|---|
| Diagnosis correct (cases with a true cause) | 18 / 25 | **35 / 35** (31 / 31 in classes A-C, the scope the Benchmarks page counts) |
| Task grade (all cases) | — | **37 / 38**: A 9/9, B 12/12, C 10/10, D 4/5, E 2/2 |
| Errors / traps taken / refused fixtures | 0 / 0 / 0 | 0 / 0 / 0 |
| Cost | $0.0179 | $0.048 |

The one fail is **T17 on `perflab_near_sla`**: just inside its SLA, the agent raised
the pool where the right answer is to change nothing. Caveats: one replay pass, not
three repeats; 8 trap fixtures were never tempted (an untested zero, as the tool says).

## Findings

- **Tomcat thread metrics were never published.** `server.tomcat.mbeanregistry.enabled`
  was not set in PerfLab, so Actuator exposed only session metrics. On
  `thread_starved` the agent saw `threads_busy_peak_threads: null` and correctly
  declined to act (principle 2). The fixture captured on 3 October has the same
  nulls. The re-run with the setting on is in the table above (CORRECT). **To decide:** add the
  setting to perf-lab permanently and re-capture the thread fixtures.
- **First attempt at the runner failed safely three times:** an untracked
  `PERF_LAB_PRODUCT.md` in the perf-lab workspace (the campaign refuses a dirty
  workspace, by design), Locust missing from the draft environment, and the draft
  venv not on `PATH`. Each failed before any measurement. Your file was moved
  aside for the run and put back.
- `pkill -f` over SSH killed my own session twice (the pattern matched the SSH
  command line). Process control now lives in small scripts on the box.

## Box A and Box B now

- Box A: back at the demo baseline (pool=2), deployed `1a9e9f9`; the new deploy hook is
  installed and logs the heap it started with.
- Box B: the dashboard (`~/crucible`, `878c834`) was never touched. The draft runs live in
  `~/crucible-draft`. `PERF_LAB_PRODUCT.md` is back in `~/perf-lab`.
- The scripts used are in Box B's home directory: `run_bench46.sh`, `after_bench46.sh`,
  `recapture46.sh`, plus `*_ctl.sh` helpers.

## For you to review

1. Group 46 and Group 47 in `docs/CRUCIBLE_TEST_ASSERTIONS.md`, each with its
   "For review" questions. New since you left: should the approved scorer gain an
   ABSTAINED outcome (it calls the thread abstention WRONG; the page counts it apart)?
2. Whether to add `server.tomcat.mbeanregistry.enabled=true` to perf-lab permanently.
3. The bench SLA targets in `config/bench/`.
4. Then merge `capstone/live-quadrants` into `capstone/perf-agent` and deploy.
5. Then the trial, steps 8–14 (including Fully Automated) in the UI.
