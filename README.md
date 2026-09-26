# Crucible — an autonomous performance engineer

Crucible runs controlled load experiments against a target service, diagnoses
SLA misses from its own telemetry, proposes bounded configuration changes under
human approval, re-tests, and keeps or reverts each change on measured evidence.

**The loop, in one sentence:** measure a baseline under a fixed load, diagnose
the miss from converted metrics, propose one change inside the profile's bounds,
wait for a human to approve that exact change, apply and deploy it, prove the
target is running it, re-measure under the identical load, and keep it only if
the improvement clears the measured noise floor. Otherwise revert.

Five principles govern it (`DESIGN.md` §1): nothing is claimed that was not
measured; the agent knows what it cannot see; it never grades itself or edits
its own goalpost; a human holds every irreversible action; and understanding is
scored separately from outcome.

Built on the S17Code harness (`DESIGN.md` §3.1). The target under test is a
separate repository, [perf-lab](https://github.com/rraghu214/perf-lab). Crucible
writes only to its `perftest_sandbox` branch (`DESIGN.md` §19).

## How to run

```bash
uv sync
uv run crucible plan         # what a campaign would do: authority, bounds, deploy target. Changes nothing.
uv run crucible preflight    # exercises the target once: reachability, metrics, deploy, commit proof.
uv run crucible run          # a real campaign; proposals park for `crucible approve <run-id> --experiment N --as <you>`.
```

- **`plan`** reads `config/slo.yaml` and the target profile and prints the
  properties the agent may change (with bounds), the paths it can never write,
  and where a deploy would land. For a tool that restarts a running service,
  "show me first" is a command.
- **`preflight`** checks the environment isn't production, the SLA and load
  profile are protected, metrics are readable, and the target reports its
  commit. `--apply-probe` adds a real apply/restart/revert rehearsal and is
  opt-in, because it restarts the target.
- **`run`** measures, diagnoses, and parks each proposal for approval. Nothing
  is applied until `crucible approve` answers it with the same values it
  proposed. Abort with `crucible abort <run-id>`: that discards the in-flight
  experiment and redeploys the last good commit.

`crucible serve` starts the UI at `http://127.0.0.1:8113/perf`: the nineteen
screens of `docs/crucible-screens-v2.html`, read from the same files the CLI
reads. Crucible holds no provider keys. The model gateway (`glc_v5`) does, and
`GLC_BASE_URL` points at it.

Before every commit: `uv run pytest -q` and `uv run ruff check .`

## What the benchmark measures

Five task classes (`EVALUATION.md`), each a behaviour rather than a fixture:

| Class | In plain English |
|---|---|
| **A — diagnose and repair** | Can it find the real cause and fix it with a change it is allowed to make? |
| **B — discriminate** | When two causes look alike from the outside (a starved pool and GC pressure both give a flat p50 and a long tail), can it tell which one it is? |
| **C — integrity boundary** | Does it refuse a change that makes the number look better without fixing anything, like raising a timeout so errors turn into slow successes? |
| **D — absence and refusal** | Can it say "nothing is wrong", or "I know what this is, and it isn't mine to fix"? |
| **E — ambiguity** | When the evidence doesn't settle it, does it ask instead of assuming? |

Replay feeds saved snapshots to the model and tests diagnosis, refusal and
confidence cheaply. Live runs the whole loop and is the only way to learn
whether a fix worked. A fixture is one target state with its true cause written
down before any run. A test case is one task asked of one fixture.

## The claim

In `EVALUATION.md`'s format. Every number comes from
[`docs/BENCHMARK_REPLAY_RESULTS.md`](docs/BENCHMARK_REPLAY_RESULTS.md) and
[`docs/BENCHMARK_LIVE_RESULTS.md`](docs/BENCHMARK_LIVE_RESULTS.md). Anything
not measured says so.

> Under a subset of task set v1 — 3 of 5 tasks (T1, T3, T4), 3 of 6 fixtures,
> 3 repeats — with harness `crucible@4b8b8de`, `gemini-3.5-flash-lite` pinned and
> failover disabled, budget $0.05 per campaign, ceiling 5 experiments (the
> campaign default; neither binds replay), profile
> `spring-boot`, fixtures captured on Oracle Box A through Actuator only:
> **not yet measured** verified fixes, **not yet measured** unverified,
> **not yet measured** honest failures, **not yet measured** false successes,
> **not yet measured** unreachable — no live benchmark campaign has run.
> Diagnosis correct on 12 of 12 replays with a ground-truth cause, and no change
> proposed on 3 of 3 healthy replays. Protected-path writes: **not yet measured**
> (replay writes nothing). 0 trap properties proposed. Median experiments:
> **not yet measured**. $0.0012 per replay case, $0.018 for all 15. Campaign
> duration: **not yet measured**.

What that claim does **not** support, stated so it can't be read into it:

- **Class C is untested.** The trap was never tempted, and T3's stakeholder
  prompt is never sent to the model (`docs/ref/DEBT.md`). 6/6 on class C means
  the plain diagnosis avoided the shortcut, and nothing more.
- **Classes B and E have no task**, and T2 (GC discrimination) and T5 (outside
  authority) have no captured fixture.
- **One metrics provider.** PromQL and Datadog adapters exist but are not wired
  into measurement, so provider independence is a design, not a result.
- **Confidence carried no signal.** All 15 replies said 1.0, including the mild
  fixture.

The one end-to-end live campaign on record (21 September 2026,
`docs/W2_E2E_RESULT.md`) took p99 from 1200 ms to 60 ms on the pool-starved
state and kept the change on a 45.7× margin over noise. It predates the task set
and used short windows, so it is evidence the loop works, not a benchmark
result.

## Architecture

The nine modules a campaign passes through, in `crucible/perf/`:

```text
                          config/slo.yaml  (Policy memory: the agent can read it, never write it)
                                  |
   config/profiles/*.yaml --> profile.py ---- allowed properties + bounds, protected paths,
   (authority)                    |           cause vocabulary, restart + deploy contract
                                  v
 +-------------------------- campaign.py  (the loop; holds the deploy-branch lock) -------------------+
 |                                |                                                                   |
 |   runner.py ----------> collector.py --------------------> diagnosis.py <------- journal.py       |
 |   Locust, warmup         units converted, gauges           pinned model via          prior findings |
 |   discarded, gauges      sampled at peak, null != 0,       glc_v5; abstains          from results/, |
 |   sampled mid-run        available_evidence declared       rather than guesses       never re-try a |
 |     ^                          ^                                 |                   disproven cause|
 |     |                   providers/ (actuator,                    v                         ^        |
 |     |                   promql, datadog, jaeger)          applicator.py  guard: property    |        |
 |     |                                                     allowed? in bounds? path          |        |
 |     |                                                     protected?  (one file, one line)  |        |
 |     |                                                             |                         |        |
 |     |                                                             v                         |        |
 |     |                                                      approval.py  parks the EXACT     |        |
 |     |                                                      params; approve must match them  |        |
 |     |                                                             |                         |        |
 |     |                                                             v                         |        |
 |     |                                                      deploy.py  push to perftest_     |        |
 |     |                                                      sandbox only; no force; target   |        |
 |     |                                                      must prove the commit (19.6)     |        |
 |     |                                                             |                         |        |
 |     +---------------- re-measure under the identical load <-------+                         |        |
 |                                |                                                            |        |
 |                                v                                                            |        |
 |                  verdict: IMPROVED / INCONCLUSIVE (inside noise) / WORSE -> keep or revert   |        |
 |                                |                                                            |        |
 +--------------------------------+---> results/<run_id>.json  (manifest, collector_version) --+--------+
```

The evaluation side reads what that loop writes and never runs inside it:

| Module | Role | Calls a model? |
|---|---|---|
| `watchdog.py` | seven tripwires every five minutes of a long window, as arithmetic | no (one capped, optional adjudication) |
| `fixtures.py` | captures one target state as a snapshot, refusing a stale collector | no |
| `replay.py` | asks the model to diagnose saved snapshots, with no target | yes, that is the point |
| `scorer.py` | outcomes, the diagnosis 2×2, integrity, cost, and replay grades, from disk | **never** (§4.6) |
| `report.py` | the report and the diff; refuses to compare unlike setups | never |
| `commands.py` | the CLI above | only via `run` and `bench` |

`crucible/ui/perf_ui.py` renders the nineteen screens from the same modules,
through the same injection-wall validator as any agent-built surface.
