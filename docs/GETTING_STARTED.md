# Getting started with Crucible

Crucible load-tests a service, works out why it misses its latency goal, proposes
**one** bounded configuration change, waits for approval, deploys it, waits until the
service proves the change is running, measures again, and keeps the change only if the
measurement says it helped.

This guide covers three things: trying it on your own machine, running it on a server
against a real target, and running a campaign. Updated 3 November 2026.

---

## 1 · On your own machine

### What you need

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A clone of this repository
- A `.env` in the repository root: `cp .env.example .env`. Set `GLC_BASE_URL` to your
  model gateway and `CRUCIBLE_CONTROL_TOKEN` to a long random string.

### Start the dashboard

```bash
uv sync --group load        # 'load' brings Locust, needed for live runs
uv run crucible serve       # http://127.0.0.1:8113/dashboard
```

Write actions (launch, approve, pause, abort, upload) need the control token: paste it
into **Settings → Control token** once.

### What works without a target

| Do | Command |
|---|---|
| See what a campaign would change, and where. Changes nothing. | `uv run crucible plan` |
| Replay the benchmark against the saved snapshots (needs the gateway only) | `uv run crucible bench --tasks config/tasks/ --fixtures fixtures/` |
| Read a run that happened elsewhere | copy its `results/<run-id>.json` into `results/`, then open **Results** |
| Upload documents and ask the Live AI chat about them | **New Test Run → Knowledge Docs** |

---

## 2 · On a server, against a real target

The reference setup is two machines: the **target** (here PerfLab, a Spring Boot
service) and the **runner** (Crucible plus the Locust load generator). Keeping them
apart means the load generator never competes with the service it measures.

### 2.1 The target

- It must expose Spring Boot Actuator (`health`, `metrics`, `env`) to the runner.
- It must report the commit it is running. PerfLab does this at `/api/version`.
- Deploys are a git push to a sandbox branch; a post-receive hook builds and restarts.
  Hook and setup: [`DEPLOY_SETUP.md`](DEPLOY_SETUP.md) and `scripts/box_a_post_receive.sh`.

### 2.2 The runner

```bash
git clone https://github.com/rraghu214/crucible.git ~/crucible
cd ~/crucible && uv sync --group load
cp .env.example .env        # then edit it, see below
```

In `~/crucible/.env`:

| Variable | What it is |
|---|---|
| `GLC_BASE_URL` | the model gateway |
| `CRUCIBLE_CONTROL_TOKEN` | the secret every write action needs |
| `CRUCIBLE_WORKSPACE` | a clean checkout of the target's repository, on its sandbox branch |
| `CRUCIBLE_UI_USER`, `CRUCIBLE_UI_PASSWORD` | optional dashboard login; turns on the public HTTPS link below |

Today the workspace checkout is made once by hand. Cloning it automatically from the
target's base branch is planned ([`ref/DEBT.md`](ref/DEBT.md)).

### 2.3 Keep the dashboard running

```bash
cp scripts/box_b_restart_serve.sh ~/restart-serve.sh && chmod +x ~/restart-serve.sh
~/restart-serve.sh          # prints {"ok":true,...} and, with a login set, a public URL
```

The script restarts `crucible serve` under `nohup`, so it survives logging out. Run it
again after every `git pull`. The server stays bound to `127.0.0.1`.

### 2.4 Open it

| Way | When | How |
|---|---|---|
| **Public HTTPS link** | Others need to watch | With `CRUCIBLE_UI_USER` / `CRUCIBLE_UI_PASSWORD` set, the script opens a Cloudflare quick tunnel and prints its URL. Everyone logs in; the login grants nothing to write without the token, unless `CRUCIBLE_UI_LOGIN_GRANTS_CONTROL=1` |
| **SSH tunnel** | Only you | `ssh -L 9113:127.0.0.1:8113 <user>@<runner>`, then `http://localhost:9113/dashboard` |

No inbound port is opened either way. Never send the login over plain HTTP.

---

## 3 · Running a campaign

1. **Put the target in the state you want to investigate.** Crucible measures what it
   finds. It never creates the fault.
2. **Check first:** `uv run crucible plan`, then `uv run crucible preflight`. Fix any
   `FAIL` before going on.
3. **Launch** from **New Test Run** (goal, scenarios, mode, budget, review), or:

   ```bash
   cd ~/crucible && set -a; . ./.env; set +a      # hand-typed commands need .env loaded
   uv run crucible run --all-scenarios --workspace "$CRUCIBLE_WORKSPACE"
   ```

   A measurement is a warmup (thrown away) plus a measured window, then the diagnosis.
4. **Approve.** In Semi-Automated mode each proposal waits. Approve it on the **Active**
   page, or:

   ```bash
   uv run crucible status <run-id>
   uv run crucible approve <run-id> --experiment 1 --as <you>            # apply exactly what was proposed
   uv run crucible approve <run-id> --experiment 1 --as <you> --reject   # decline; the run stops
   ```

   An approval is bound to the exact values shown. There is no field for a different
   value.
5. **Read the result** on **Results**, or `uv run crucible report --run <run-id>`.
   Compare two runs with `uv run crucible diff --a <run-a> --b <run-b>`.

| Need to | Do |
|---|---|
| Stop and roll the target back | `uv run crucible abort <run-id>` (redeploys the last good commit) |
| Pause and continue later | **Pause** on Active, then **Resume**; or `uv run crucible resume <run-id>` |
| Change the goal mid-run | edit it in the wizard and **Save changes**; it applies at the next experiment and is recorded |

---

## 4 · When something does not work

| Symptom | Check |
|---|---|
| Launch refused: "workspace has uncommitted changes" | `git status` in the workspace must be clean |
| Launch refused with 409 | A campaign is already running or paused |
| A write action says **401** | The control token in Settings does not match `.env` |
| A write action says **503** | `CRUCIBLE_CONTROL_TOKEN` is missing from the server's `.env`; add it and restart |
| `curl` to the API says "login required" | The dashboard login is on: add `-u user:password` |
| The run stops after deploy with NOT_MEASURED | The target never reported the pushed commit: check the deploy hook's output |
| The first model call is slow | The gateway was asleep; the first call wakes it (up to a minute) |
