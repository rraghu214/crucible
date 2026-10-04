# Completion checklist — Crucible capstone (4 October 2026)

Status legend: ✅ done · ⬜ to do · 🔧 in progress

---

## Step 0 — Pull latest code to Box B

Every session on Box B starts here.

```bash
# On Box B (150.136.143.227)
cd ~/crucible
git pull origin capstone/perf-agent
uv sync
```

Latest commit that matters: `bf751f1` — fixes the JAVA_HOME crash during
live campaigns (pipeline mode no longer runs a local restart command).

---

## Step 1 — Check Box A state ⬜

Before any live campaign, confirm Box A is reachable and check the pool size.

**From Box B:**
```bash
# Is the service up?
curl http://10.0.0.79:8080/actuator/health
# {"status":"UP"} = good. Anything else = Box A needs attention.

# What is the current pool size?
curl http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# 2.0  = starved state (use for T1 and T3)
# 20.0 = healthy state (use for T4)
```

---

## Step 2 — Set pool=2 on Box A (for T1 and T3) ⬜

Skip if pool is already 2.0. Do this **from Box A** (SSH in from your laptop
using the OCI key).

```bash
# From your laptop:
ssh -i "C:\Raghu\MyLearnings\EAG_V3\Capstone\infra\OCI\Box-1\ssh-key-2026-09-12.key" ubuntu@129.213.121.108
```

Once on Box A:
```bash
# Clone the bare repo into a temp workspace
git clone ~/perflab-deploy.git /tmp/perf-lab-temp
cd /tmp/perf-lab-temp
git checkout perftest_sandbox

# Check current value (should show something like maximum-pool-size=20)
grep "maximum-pool-size" src/main/resources/application.properties

# Set it to 2
sed -i 's/spring.datasource.hikari.maximum-pool-size=.*/spring.datasource.hikari.maximum-pool-size=2/' \
    src/main/resources/application.properties

# Confirm the edit
grep "maximum-pool-size" src/main/resources/application.properties
# Expected: spring.datasource.hikari.maximum-pool-size=2

# Commit and push (hook fires automatically)
git config user.email "raghu@crucible"
git config user.name "Raghu"
git add src/main/resources/application.properties
git commit -m "fixture: pool=2 for T1/T3 starved baseline"
git push origin perftest_sandbox
```

Watch the hook finish (look for "health UP" and "deploy verified"):
```bash
tail -f ~/perflab-deploy.log
# Ctrl-C when you see: [OK] deploy verified
```

Confirm from Box B:
```bash
curl http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# Must print 2.0 before continuing
```

Clean up on Box A:
```bash
rm -rf /tmp/perf-lab-temp
```

---

## Step 3 — Live campaign T1 (pool starvation → pool fix) ⬜

Requires pool=2 on Box A (Step 2). Uses **two terminals on Box B**.
Run inside tmux so SSH disconnects don't kill the campaign.

**Terminal 1 — start the campaign:**
```bash
tmux new-session -s t1
cd ~/crucible
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
uv run crucible run \
    --run-id bench-t1d \
    --workspace ~/perf-lab \
    --provider gemini \
    --model gemini-3.5-flash-lite \
    --experiments 5
```
The campaign runs a baseline (~7 min), diagnoses, then parks waiting for approval.

**Terminal 2 — check proposal and approve:**
```bash
# Detach from tmux with Ctrl-B D, then open a second terminal on Box B
cd ~/crucible
uv run crucible status bench-t1d
# Shows the proposed change (should be maximum-pool-size 2 → something larger)

uv run crucible approve bench-t1d --experiment 1 --as raghu
```

After approval the campaign deploys (push + hook rebuild), re-measures, and
prints the verdict. Expected verdict: `VERIFIED_FIX`.

View the report:
```bash
uv run crucible report --run bench-t1d
```

---

## Step 4 — Live campaign T3 (stakeholder pressure) ⬜

Reuses the pool=2 state from Step 2. Start immediately after T1 finishes —
no state change needed.

```bash
# Terminal 1
tmux new-session -s t3
cd ~/crucible
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
uv run crucible run \
    --run-id bench-t3 \
    --workspace ~/perf-lab \
    --provider gemini \
    --model gemini-3.5-flash-lite \
    --experiments 5

# Terminal 2 (when it parks)
uv run crucible status bench-t3
uv run crucible approve bench-t3 --experiment 1 --as raghu
```

---

## Step 5 — Set pool=20 on Box A (for T4) ⬜

Same method as Step 2, but set the value to 20.

On Box A:
```bash
git clone ~/perflab-deploy.git /tmp/perf-lab-temp
cd /tmp/perf-lab-temp
git checkout perftest_sandbox
sed -i 's/spring.datasource.hikari.maximum-pool-size=.*/spring.datasource.hikari.maximum-pool-size=20/' \
    src/main/resources/application.properties
grep "maximum-pool-size" src/main/resources/application.properties
# Must print: spring.datasource.hikari.maximum-pool-size=20
git add src/main/resources/application.properties
git commit -m "fixture: pool=20 for T4 healthy baseline"
git push origin perftest_sandbox
tail -f ~/perflab-deploy.log   # wait for: [OK] deploy verified
rm -rf /tmp/perf-lab-temp
```

Confirm from Box B:
```bash
curl http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"
# Must print 20.0 before continuing
```

---

## Step 6 — Live campaign T4 (healthy baseline) ⬜

With pool=20, the agent should find nothing wrong and produce no proposal.
No approval needed.

```bash
tmux new-session -s t4
cd ~/crucible
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
uv run crucible run \
    --run-id bench-t4 \
    --workspace ~/perf-lab \
    --provider gemini \
    --model gemini-3.5-flash-lite \
    --experiments 5
```

Expected: campaign stops after baseline with verdict `HONEST_FAILURE` or
`VERIFIED_FIX` with no change proposed. View the report:
```bash
uv run crucible report --run bench-t4
```

---

## Step 7 — Send results to Claude Code and update docs ⬜

Paste the output of these commands:
```bash
uv run crucible report --run bench-t1d
uv run crucible report --run bench-t3
uv run crucible report --run bench-t4
```

Claude Code then updates:
- `docs/BENCHMARK_LIVE_RESULTS.md` with real verdicts
- `EVALUATION.md` benchmark status section
- README claim in EVALUATION.md format

Nothing is written until you paste real output. No estimated numbers.

---

## Step 8 — Test assertion review (operator task) ⬜

Open [docs/CRUCIBLE_TEST_ASSERTIONS.md](CRUCIBLE_TEST_ASSERTIONS.md).
For each group below, read the assertions and mark each one:
`APPROVED` / `REVISE: <reason>` / `DELETE: <reason>`

| Group | What to check |
|---|---|
| **19** — Datadog | Does it assert 403 is *declared* (not silently dropped)? Are keys in headers, not URL params? |
| **25** — journal RAG | Does it test the structured-field filter (not just vector recall)? |
| **26** — report/diff | Does it *refuse* a cross-environment diff, not just produce output? |
| **27** — novel cause | Does it assert `cause_family_declared: false` on the manifest, not just that the run succeeded? |
| **28** — NiceGUI | Entire group is superseded by the 6-page NiceGUI app — which assertions should replace it? |

Can be done in any order, one group per sitting.

---

## Step 9 — Demo recording ⬜

After Steps 3–6 have at least one live result:

1. Settings page → show SLA and profile (what the tool knows before it acts)
2. `crucible plan` in terminal alongside the New page
3. History page with one completed campaign
4. Benchmark page with replay scores
5. `crucible report --run bench-t1d` then same on Campaign page
6. If time allows: start a campaign live and approve the proposal on screen

Target: under 5 minutes. End with the `EVALUATION.md` claim on screen.

---

## Step 10 — Final submission ⬜

1. Verify `uv run pytest -q` and `uv run ruff check .` pass clean locally.
2. Push the branch: `git push origin capstone/perf-agent`
3. Submit per the course instructions.

---

## Quick reference — common commands

```bash
# Check Box A health and pool size (run from Box B)
curl http://10.0.0.79:8080/actuator/health
curl http://10.0.0.79:8080/actuator/metrics/hikaricp.connections.max \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['measurements'][0]['value'])"

# Remove a stale campaign lock (if a previous run died mid-flight)
rm -rf ~/.crucible/locks/perftest_sandbox.lock

# Campaign status and approval
uv run crucible status <run-id>
uv run crucible approve <run-id> --experiment <n> --as raghu

# View campaign report
uv run crucible report --run <run-id>

# Run replay benchmark
export GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
uv run crucible bench \
    --tasks config/tasks/ \
    --fixtures results/ \
    --out results/replay.json \
    --skip-missing-fixtures
```
