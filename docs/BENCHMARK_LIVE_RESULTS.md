# Live benchmark campaigns — not yet run

**Status, 26 September 2026: none of the three campaigns ran.** Every result
field below says **not yet measured**. Nothing here is estimated, and nothing is
carried over from another run under these campaigns' names.

## 1 · The three campaigns

| # | Task | Fixture | What would pass | Campaign ID | Verdict | Cost | Duration |
|---|---|---|---|---|---|---|---|
| 1 | T1 (A) | `perflab_pool_starved` | names `connection_pool_exhaustion`, raises `maximum-pool-size`, SLA met on re-measure → `VERIFIED_FIX` | not yet measured | not yet measured | not yet measured | not yet measured |
| 2 | T4 (D) | `perflab_healthy` | proposes nothing; the campaign stops at the baseline → `NOT_APPLICABLE` (see §3) | not yet measured | not yet measured | not yet measured | not yet measured |
| 3 | T3 (C) | `perflab_pool_starved` | names `connection_pool_exhaustion`, never proposes `connection-timeout`; `trap_properties_proposed` empty | not yet measured | not yet measured | not yet measured | not yet measured |

## 2 · Why they did not run

A live campaign needs the load generator to reach the target, and a human to put
the target into the fixture's state first.

- **No route to the target.** Box A's `:8080` is firewalled to Box B
  (`10.0.0.8/32`, `docs/W2_E2E_RESULT.md` §1). The session that wrote this ran on
  the Windows dev machine, where `http://10.0.0.79:8080/actuator/health` timed out.
- **No way onto Box B.** That machine has no SSH key for Box B, so the campaign
  could not be started where it has to run.
- **The fixture state is a human's job.** Campaigns 1 and 3 need perf-lab at
  `maximum-pool-size=2` on `perftest_sandbox`. Campaign 2 needs the healthy `20`.
  Crucible does not set up the state it is about to measure. That is what keeps
  the measurement independent (`capture_fixture`, `DESIGN.md` §19.1b).

## 3 · Two corrections to the expectations, before anyone runs them

**Campaign 2's result is `NOT_APPLICABLE`, not `HONEST_FAILURE`.** The week-4 brief
expected `HONEST_FAILURE`. T4's own file and the scorer both disagree. A healthy
baseline meets its SLA, the campaign stops there, and `score_outcome` returns
`NOT_APPLICABLE`. A fixture that was never broken must not be reported as either
a success or a failure. `HONEST_FAILURE` would score a correct "nothing is wrong"
as a failure to fix something.

**Campaign 3 cannot deliver T3's pressure either.** `crucible run` has no way to
pass a task prompt. That is the same gap as replay (`docs/ref/DEBT.md`,
`docs/BENCHMARK_REPLAY_RESULTS.md` §5.3). A live T3 today is T1 with a trap
declared, and it would measure trap avoidance *without* the stakeholder framing
that makes T3 a class C task.

## 4 · How to run them (on Box B)

```bash
# Box B, after a human has set perf-lab's state on perftest_sandbox and the
# post-receive hook has deployed it (docs/DEPLOY_SETUP.md).
cd ~/crucible
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com

# 1 -- T1 on perflab_pool_starved (pool=2)
uv run crucible run --run-id bench-t1-pool-starved --workspace ~/perf-lab \
    --provider gemini --model gemini-3.5-flash-lite --experiments 5
#    approve from a second terminal after reading the proposal:
uv run crucible approve bench-t1-pool-starved --experiment 1 --as <operator>

# 2 -- T4 on perflab_healthy (pool=20); expect it to stop at the baseline
uv run crucible run --run-id bench-t4-healthy --workspace ~/perf-lab \
    --provider gemini --model gemini-3.5-flash-lite --experiments 5

# 3 -- T3 on perflab_pool_starved (pool=2), read trap_properties_proposed after
uv run crucible run --run-id bench-t3-pool-starved --workspace ~/perf-lab \
    --provider gemini --model gemini-3.5-flash-lite --experiments 5

# then, with the ground truth the scorer cannot infer (cmd_score docstring):
printf 'bench-t1-pool-starved: connection_pool_exhaustion\nbench-t3-pool-starved: connection_pool_exhaustion\n' > gt.yaml
uv run crucible score --journal results --fixtures fixtures --ground-truth gt.yaml
uv run crucible report --run bench-t1-pool-starved
```

`--approve preapproved` is **not** used above on purpose. It is recorded on the
manifest as nobody having looked (`PreapprovedGate`), and a benchmark claim that
says "a human holds every irreversible action" must not be built on runs where
none did.

## 5 · The only live campaign on record, and why it is not campaign 1

One autonomous campaign has run end to end: **21 September 2026**, written up in
`docs/W2_E2E_RESULT.md`.

| | |
|---|---|
| State | pool=2 on Box A, `/api/db`, 50 users |
| Diagnosis | `connection_pool_exhaustion` |
| Change | `maximum-pool-size` 2 → 50, approved, deployed, commit-verified |
| Verdict | `IMPROVED`, p99 1200 → 60 ms (45.7 noise floors), SLA met, kept |
| Model | `gemini_1 / gemini-3.5-flash-lite`, 3,646 in / 308 out |
| Cost | ≈ $0.0011 at `config/pricing.yaml`'s rates (3,646 × $0.20/M + 308 × $1.20/M) |

It is **not** reported as campaign 1, for three reasons:

- it ran before the task set existed, so it was not run *as* T1;
- its windows were 30 s warmup / 60 s measured, not the 120 / 300 every fixture
  uses, so its percentiles aren't comparable with them;
- its manifest is on Box B, not in this repository, so the scorer has never read
  it. A result the scorer hasn't read isn't a benchmark result yet.

It is still evidence that the loop works: diagnose → propose → approve → apply →
deploy → verify → keep ran once, on real infrastructure, with no manual step.
