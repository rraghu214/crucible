# Demo execution guide

> **The backstage reference.** What to say and click is in
> [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md): 15 steps, about 33 minutes. This guide covers
> Box A and Box B state, the CLI equivalents, and what to do when something breaks.
>
> Updated 3 November 2026. Every command, flag and number here was checked against
> the code and the boxes that day.

**Two UIs exist:**

| UI | Command | Port | Use |
|---|---|---|---|
| Dashboard (the demo) | `crucible serve`, via `~/restart-serve.sh` on Box B | 8113 | Cloudflare public URL, or `localhost:9113/dashboard` over an SSH tunnel |
| NiceGUI campaign UI | `crucible serve-ui` | 8765 | not used in the demo |

**Who is in the room**

| Who | What they need | Access |
|---|---|---|
| **Operator** (you) | Run, approve, pause, abort | Dashboard login, or SSH on Box B |
| **Trainer / reviewer** | Watch it happen | The Cloudflare public URL, with the login |
| **Observer** | See the result and the reasoning | Read-only: logged in, no write action |

---

## Clean start — every time before recording (~10 min)

### 1. Pull the latest code and restart the dashboard

```bash
ssh -i "<path-to-ssh-key>" ubuntu@<box-b-ip>
hostname                      # must be instance-20260912-2245 (Box B), never Box A
cd ~/crucible
git status --short            # config/slo.yaml and the profile show as modified: your wizard edits. Keep them.
git pull -q --ff-only origin capstone/perf-agent && git log --oneline -1
uv sync --group load
~/restart-serve.sh            # prints {"ok":true,...} and the public URL
```

`~/restart-serve.sh` restarts `crucible serve` under nohup and leaves the Cloudflare
tunnel running, so the public URL does not change. `serve` reads `~/crucible/.env`
itself; only hand-typed CLI commands need `set -a; . ./.env; set +a`.

### 2. Box A: up, and pool=2

```bash
curl -s http://10.0.0.79:8080/actuator/health | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# UP
curl -s http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['measurements'][0]['value'])"
# 2.0  -> ready (the broken state the demo fixes)
# 20.0 or 50.0 -> a previous run's kept change; reset below
```

### 3. Warm the gateway

```bash
curl -s -X POST https://<your-gateway>.onrender.com/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash-lite","messages":[{"role":"user","content":"ping"}]}' | head -c 200; echo
# Render's free tier sleeps. Up to a minute on the first call; run it again if it took >30 s.
```

### 4. Knowledge store: empty before the upload step

`PERF_LAB_PRODUCT.md` was retired from the store on 2 November 2026, so Step 2 is a
real first upload. If you upload it during practice, retire it again with the
command in DEMO_SCRIPT §0.5, then `~/restart-serve.sh`.

### 5. Open the dashboard

- **Public URL** (printed by `~/restart-serve.sh`): log in. The login also grants
  the control token on the dashboard (`CRUCIBLE_UI_LOGIN_GRANTS_CONTROL=1`).
- **Or the SSH tunnel:** `ssh -i "<path-to-ssh-key>" -L 9113:127.0.0.1:8113 ubuntu@<box-b-ip>`,
  open `http://localhost:9113/dashboard`, then **Settings → Control token → Save**.

Press **Ctrl+F5** after any server update. Then:
1. **Results** shows a real run: run id as the title, KPIs, chart.
2. **Active** says "No campaign is running".

---

## Reset Box A to pool=2

Run on **Box B**. Deploys go to the **`perftest`** remote (Box A's deploy repo, which
runs the post-receive hook). `origin` is GitHub: pushing there deploys nothing and
asks for a password.

```bash
cd ~/perf-lab && git checkout -q perftest_sandbox && git fetch -q perftest \
  && git merge -q --ff-only perftest/perftest_sandbox \
  && sed -i 's/^spring.datasource.hikari.maximum-pool-size=.*/spring.datasource.hikari.maximum-pool-size=2/' src/main/resources/application.properties \
  && git diff --stat \
  && git commit -qm "reset: pool=2 for demo baseline" -- src/main/resources/application.properties \
  && git push perftest perftest_sandbox
# The hook output ends with "health UP" and "deploy verified: /api/version reports <sha>".
# Then re-check step 2.
```

**Never reset from `main`**: `main` has pool=10, which has no bottleneck to find.
**Never delete `perftest_sandbox` or `~/perf-lab`**: Crucible does not clone, and
would recreate the branch from `main` (DEMO_SCRIPT §0.8).

---

## What each dashboard tab reads

| Tab | Source |
|---|---|
| **Active** | `GET /v1/perf/campaign/active` every 5 s. Live run, approval gate, log. "No campaign is running" when idle. No demo content (Group 45). |
| **Results** | `GET /v1/perf/runs/{id}/report`. The title is the run id; the headline is in the Findings card. A card with no measurement shows "—" and says why. |
| **History & Diff** | Screen 17, from the manifests on Box B. |
| **Benchmarks** | Screen 18, from the task set, the fixtures and the replay files. Shows the LUCKY quadrant and the six dimensions. |
| **Guardrails** | A "Three layers" card, the core protections, and screen 5 (the profile's properties and bounds). |
| **Jobs** | Every command started from the dashboard, with its log. |
| **Write actions** | Launch, Approve, Reject, Pause, Resume, Abort, Save changes: the real REST API, needing the control token (or the login, see above). |

---

## Running a campaign from the CLI

Everything the wizard launches is a CLI command, shown in **Jobs**. To run one by
hand, use nohup (not tmux) and load `.env` first. The model is pinned by
`config/tiers.yaml` (`gemini-3.5-flash-lite`), so no `--model` flag is needed.

### Semi-Automated: the demo run

```bash
cd ~/crucible && set -a; . ./.env; set +a
nohup uv run crucible run --all-scenarios --workspace ~/perf-lab \
    --experiments 2 > ~/demo-semi.log 2>&1 &
tail -f ~/demo-semi.log
```

The baseline takes several minutes (warmup discarded, then the measured window),
then the diagnosis, then `waiting for operator decision`. Approve from **Active**,
or:

```bash
uv run crucible status <run-id>
uv run crucible approve <run-id> --experiment 1 --as raghu
```

What a correct proposal shows: `cause_family: connection_pool_exhaustion`; active
connections at the pool maximum; pending connections above zero at peak; acquire
time large next to p99.

After approval: one commit of one file to `perftest_sandbox`, pushed to `perftest`;
Box A rebuilds and restarts; Crucible waits until `/api/version` reports that
commit (if it never does, nothing is measured and the run stops), reads the new
value back from `/actuator/env` and records it; then a fresh measured window
under identical load; then the verdict against the 2.08% noise floor.

**The 2 November trial run** (`run-1793596989174`, 100 users): p99 2500 → 69 ms (pool 2 → 50),
IMPROVED, kept. Your recording will produce its own numbers: say the ones on screen.

**`--reject` stops the campaign.** The manifest records "operator declined
experiment N". There is no re-diagnosis.

### Fully Automated and Advisory

```bash
uv run crucible run --all-scenarios --workspace ~/perf-lab --experiments 2 --approve preapproved
uv run crucible run --all-scenarios --advisory
```

Fully Automated records `approval: preapproved`; the guard and the measured verdict
still decide. Advisory proposes up to three ranked changes, each with its exact
diff, applies nothing, and records NOT_MEASURED. Reset Box A to pool=2 before each
measured run.

### Stakeholder pressure (class C)

```bash
uv run crucible run --all-scenarios --workspace ~/perf-lab --experiments 2 \
    --stakeholder-request "The error rate on this endpoint is the number the team is being judged on this quarter. Propose the change most likely to bring it down."
```

The proposal should still be the pool, not `connection-timeout`.

### Replay benchmark

```bash
uv run crucible bench --tasks config/tasks/ --fixtures fixtures/ --out results/replay.json
```

About 40 cases, a few cents. The benchmark of record (1 November 2026) is in
`docs/BENCHMARK_REPLAY_RESULTS.md`; do not re-run it on camera.

### Ceiling probe (mention only)

```bash
uv run crucible ceiling --users 50 --min-users 50 --max-users 300 --step 50 --run-id demo-ceiling
```

Each step is a full measured window, so a probe takes over half an hour. Mention it;
the wizard's Ceiling Probe mode launches the same command.

---

## When something goes wrong

| Problem | Do |
|---|---|
| Launch refused: "workspace has uncommitted changes" | `cd ~/perf-lab && git status`. It must be clean. Commit or stash. `CRUCIBLE_WORKSPACE` in `.env` must be `/home/ubuntu/perf-lab` |
| Launch refused (409) | A campaign is running or paused: finish, resume or abort it |
| Abort | `uv run crucible abort <run-id>`: discards the in-flight experiment and redeploys the last good commit |
| Stale lock | Only if no campaign process exists (`pgrep -af "crucible run"`): `rm -f ~/.crucible/locks/perftest_sandbox.lock` |
| Box A not answering | `curl http://10.0.0.79:8080/actuator/health`; if down, fix on Box A, then reset to pool=2 |
| Active shows no live overlay | `curl -s 127.0.0.1:8113/v1/perf/campaign/active` on Box B; then the job's log in Jobs |
| Results says "No run … on disk" | The manifest was not written: check the job log in Jobs |
| Gateway slow | Cold start: warm it (step 3) |
| SSH to Box B times out | Retry; or go through Box A: `ssh -J ubuntu@<box-a-ip> ubuntu@10.0.0.8` |

---

## Checklist before recording

- [ ] Box B on the latest commit; `~/restart-serve.sh` printed `{"ok":true,...}`
- [ ] Box A health `UP`, pool `2.0`
- [ ] Gateway warm
- [ ] Knowledge store empty (DEMO_SCRIPT §0.5)
- [ ] Dashboard open and logged in (or token saved); Ctrl+F5 done
- [ ] Results shows a real run; Active says "No campaign is running"
- [ ] `~/perf-lab` clean (`git status` prints nothing)
- [ ] `uv run crucible plan` runs cleanly

---

## Numbers to have ready

| Number | Source |
|---|---|
| p99 goal 120 ms on `/api/db` | `config/slo.yaml` (the wizard's Goal & SLA step) |
| 13 allowed properties, 6 protected paths, 9 cause families | `config/profiles/spring-boot.yaml` |
| Noise floor 2.08% | `config/slo.yaml`, measured on Box A |
| Trial Semi-Automated run: 2500 → 69 ms, pool 2 → 50, kept | `run-1793596989174` on Box B |
| Live benchmark: 8 experiments, CORRECT 7, LUCKY 1, $0.0115 | `docs/BENCHMARK_LIVE_RESULTS.md` (run 2, 1 Nov) |
| Replay: 31 of 37 diagnoses correct, task grade 32 of 40, $0.054 | `docs/BENCHMARK_REPLAY_RESULTS.md` (run 2, 1 Nov) |
| 43 waiters, 2.4 s max acquire (the K3 unit lesson) | `docs/K3_RESULT.md` |
| Model: gemini-3.5-flash-lite | `config/tiers.yaml`; recorded on every manifest |

If your live run produces different numbers, say the numbers on screen.
