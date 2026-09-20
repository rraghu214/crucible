# Week 2 — one full campaign, end to end on the cloud boxes

**Date:** 2026-09-21
**Verdict: PASS.** A fully autonomous campaign measured a bottleneck, diagnosed
it, proposed a bounded change, applied it, deployed it to a separate pre-prod
box, proved the target was running the deployed commit, re-measured, and kept the
change on the evidence. No manual steps.

This is `DESIGN.md` §16's week-2 item 5, and the last one outstanding.

---

## 1 · Topology

| | Box A — target | Box B — Crucible + Locust | Gateway |
|---|---|---|---|
| Host | Oracle Ashburn, `10.0.0.79` | Oracle Ashburn, `10.0.0.8` | Render, free plan |
| Runs | PerfLab on Postgres/Redis/httpbin/Prometheus | campaign, diagnosis, Locust | `glc_v5` |
| Reached by | Box B only, `:8080` firewalled to `10.0.0.8/32` | — | HTTPS |

The load generator does not share a CPU with the target (§1). Crucible holds
**no shell credential** for Box A: it pushes to a bare repo over a git-only
deploy key, and Box A's own `post-receive` hook deploys (§19.8).

Scenario: 50 users, spawn rate 10, `/api/db`, **30 s warmup discarded, 60 s
measured**. The shipped defaults are 120/300; this run traded precision for wall
clock and changes which numbers are quotable, not which code paths execute.

## 2 · Result

| | Baseline | After experiment 1 |
|---|---|---|
| p99 | **1200 ms** | **60 ms** |
| rps | 38.3 | 189.3 |
| `pending` peak | 42 | **0** |
| `active` peak | 2 / 2 | 17 / 50 |
| acquire mean | 974.52 ms | **0.17 ms** |

```
cause family     : connection_pool_exhaustion
change           : spring.datasource.hikari.maximum-pool-size  '2' -> 50
verdict          : IMPROVED   (p99 fell 95.00%, 1200 -> 60 ms)
margin over noise: 45.7 noise floors   (floor 2.08%, measured on this box)
SLA met          : False -> True       (objective: p99 <= 120 ms)
kept             : True
manual steps     : none
served by        : gemini_1 / gemini-3.5-flash-lite
tokens           : 3646 in / 308 out
deployed commit  : 4e5319e5e7a6a7ed…   verified
```

The agent's diagnosis was correct and its evidence was the converted fields —
`acquire_mean_ms`, `pending_peak_connections`, `utilisation_peak_pct` — not raw
Micrometer tuples. That is §4.1 holding on a live run: the K3 failure was reading
`acquire MAX: 2.4` as 2.4 ms when the true value was 2406 ms.

## 3 · Observations worth keeping

**3.1 The bottleneck fixture reproduced on a third environment.** `pending` peaked
at **42**. K1 recorded 43, 42, 43 on this box; the original K1 recorded 43, 42, 43
on a Windows laptop with H2; a local rehearsal on 13 September recorded 43. Four
environments, three CPU architectures, two databases, the same number. The queue
depth is set by the load profile and the pool size, not by the host — which is
what makes it a fixture rather than a property of one machine.

**3.2 The agent was conservative by about 2x, again.** It predicted 115 ms and
measured 60 ms. K3's agent predicted 140 ms and measured 93 ms — conservative by
~1.5x. Two data points, same direction. Worth watching in week 3's calibration
scoring: an agent that consistently under-promises is not well calibrated even
though it is never embarrassed by a miss.

**3.3 It chose 50, not 20.** `active` peaked at 17 of 50, so the pool was larger
than the workload needed. Not harmful here, and the verdict is about the measured
outcome rather than about elegance — but it is the kind of thing §4.7's separate
diagnosis score exists to catch. Being right for a reason you cannot defend is
not the same as being right.

**3.4 `deploy verified` in 0.0 s is genuine, and looks alarming.** `git push`
blocks until the `post-receive` hook finishes, and the hook does not exit until
`/api/version` reports the new commit — so by the time Crucible polls, the target
has already confirmed itself. The shape is identical to a stale read, which is
why it was checked against the hook log and the live target rather than
believed.

**3.5 Nothing in the campaign depends on the gateway persisting anything.**
Render's free plan has an ephemeral filesystem, so `glc_v5`'s own cost history
does not survive a redeploy. Crucible prices each call from the usage returned in
the response and records it on the manifest (`3646 in / 308 out` above). §4.6
already requires the scorer to read manifests from disk rather than query a
service; this is that rule paying for itself.

## 4 · What this run does NOT show

- **One experiment, not a campaign of several.** The SLA was met immediately, so
  the loop stopped as designed. Ruled-out feedback, re-diagnosis and the
  experiment ceiling were exercised in unit tests, not here.
- **No INCONCLUSIVE or WORSE path.** The change was decisive — 45.7 noise floors.
  Revert-on-regression has never run on live infrastructure.
- **The verification gate used the commit sha**, which is the top rung of §19.6's
  ladder and the rung real applications usually cannot reach. The property
  read-back path is designed and documented but not yet built.
- **Short windows.** 30/60 rather than 120/300, so these percentiles are not
  directly comparable with K1's.

## 5 · Reproducing it

Deploy path, keys and the two failures found while building it:
`docs/DEPLOY_SETUP.md`.

```bash
# Box B
cd ~/perf-lab && git fetch perftest && git checkout -B perftest_sandbox perftest/perftest_sandbox
cd ~/crucible && uv run python ~/cloud_campaign.py      # GLC_BASE_URL from .env
```

The three things that are easy to get wrong, all of which we hit:

1. **Do not pass a tunable on the JVM command line.** A Spring argument overrides
   `application.properties`, which is the file the agent edits — so every
   experiment would be applied and then silently ignored.
2. **Kill by pattern, not just by pid file, and verify the port is free.** A
   stale JVM holding `:8080` makes the new build fail to bind while the old one
   keeps answering, and the deploy looks healthy.
3. **The workspace is the target's repo, and one writer only.** Box B's checkout
   tracks `perftest_sandbox`; a second writer produces non-fast-forward
   rejections at the least useful moment.
