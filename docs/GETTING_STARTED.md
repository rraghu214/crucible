# Getting started with Crucible

Crucible is an autonomous performance engineer. It runs a controlled load test
against a service, works out why the service misses its latency goal, proposes
**one** configuration change, waits for a human to approve it, applies it,
measures again, and keeps the change only if the measurement proves it helped.

You can use it in two places:

| | Where | What you can do there |
|---|---|---|
| **Locally** | your own machine | Browse the six UI screens, read the benchmark results, replay saved snapshots against the model. You **cannot** run a live campaign: the target is not reachable from outside Box B. |
| **Box B** | the Oracle cloud box next to the target | Everything: live campaigns, approvals, and the UI other people can open. |

The UI is a window onto the work. Starting a run, approving a change and
aborting are done from the command line, on purpose. See §4.

---

## 1 · Locally

### 1.1 What you need

- Python and [uv](https://docs.astral.sh/uv/) installed
- A checkout of this repository on the `capstone/perf-agent` branch
- A `.env` file in the repository root. Copy `.env.example` if you do not have
  one. The UI starts without any model gateway.

### 1.2 Start the app

```powershell
cd <path-to>\crucible
git checkout capstone/perf-agent
git pull
uv sync
uv run crucible serve-ui
```

Open **http://127.0.0.1:8765**. Press Ctrl+C in that terminal to stop it.

If port 8765 is taken, run `uv run crucible serve-ui --port 8766` instead.

### 1.3 What you will see locally

| Page | What it shows on your machine |
|---|---|
| **Home** | Service config, any active campaigns (none locally), recent results |
| **New** | The exact `crucible run` command for the current config |
| **Campaign** | "No campaign running" — correct, campaigns run on Box B |
| **History** | Campaign manifests if you copy results from Box B |
| **Benchmark** | Replay results, task table (18 tasks), fixture table (20 fixtures) |
| **Settings** | Workspace config, SLA, profile, collector version, CLI reference |

### 1.4 Things that do work locally

**See what a campaign would do.** This reads config and changes nothing:

```powershell
uv run crucible plan
```

**Replay the benchmark against saved snapshots.** This needs the model gateway
but not the target. Point it at the hosted gateway, in the same terminal:

```powershell
$env:GLC_BASE_URL = "https://glc-v5-rraghu214.onrender.com"
uv run crucible bench --tasks <folder with T1.yaml, T3.yaml, T4.yaml> --fixtures fixtures `
    --provider gemini --model gemini-3.5-flash-lite --out results/replay.json
```

Pass a folder holding only the tasks whose fixtures exist today (T1, T3, T4).
The full `config/tasks/` folder is refused, and the refusal names the two
fixtures still missing. That is intended behaviour, not a fault
(`docs/BENCHMARK_REPLAY_RESULTS.md` §1.1).

**View a campaign that ran on Box B.** Copy its `results/<run-id>.json` from Box B
into `results/` on your machine and refresh the page. Report, History, Plan graph
and Watchdog then show it.

---

## 2 · Box B: the app always running at a public URL

Everything in this section runs **on Box B** over SSH, unless it says otherwise.
Replace `ubuntu` with your Box B login user and `<BoxB-public-IP>` with its
address.

### 2.1 Get the latest code

```bash
cd ~/crucible
git fetch origin
git checkout capstone/perf-agent
git pull
uv sync
```

Box B's `.env` should contain:

- `GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com`
- a `CRUCIBLE_CONTROL_TOKEN`

The token is what lets you approve changes from the browser. Keep it to
yourself.

### 2.2 Run the UI as a service, so it survives logouts and reboots

Create `/etc/systemd/system/crucible-ui.service`. Check the uv path with
`which uv`.

```ini
[Unit]
Description=Crucible UI
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/crucible
EnvironmentFile=/home/ubuntu/crucible/.env
ExecStart=/home/ubuntu/.local/bin/uv run crucible serve-ui --host 0.0.0.0 --port 8765 --root /home/ubuntu/crucible
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crucible-ui
systemctl status crucible-ui          # should say "active (running)"
```

After every `git pull`, run `sudo systemctl restart crucible-ui`.

### 2.3 Open port 8765

It has to be opened in two places.

**Oracle Console:** go to Networking, then your VCN, then the security list (or
network security group) for Box B. Add an ingress rule:

- Protocol: TCP
- Destination port: `8765`
- Source: `0.0.0.0/0` for anyone, or **`<viewer-ip>/32` to allow a single
  person**. Prefer the second.

**On the box:**

```bash
sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT
sudo netfilter-persistent save
```

### 2.4 Open it

**http://<BoxB-public-IP>:8765**

Share that link with anyone you want to watch, for example your trainer.

### 2.5 Know what a public URL exposes

- **Viewers can read every page.** That includes the SLA and profile, which
  mention Box A's private IP. Box A is not reachable from outside, so this is low
  risk, but it is visible.
- **Viewers cannot change anything.** Every write action needs the control
  token, and without it the server refuses.
- **The URL is plain HTTP.** Do not type the control token into the page over
  the public URL: it would cross the internet unencrypted. Approve from your SSH
  session instead (§3.3), or over an SSH tunnel (§2.6).

### 2.6 Private alternative: an SSH tunnel

Use this when nobody else needs to see the UI. It needs no open port. Run it
**on your own machine**:

```powershell
ssh -i <your-BoxB-key> -L 8765:127.0.0.1:8765 ubuntu@<BoxB-public-IP>
```

Keep that window open, then browse to **http://localhost:8765**. Over the
tunnel it is safe to paste the control token into the sidebar.

---

## 3 · Running a campaign (on Box B)

### 3.1 Before you start

1. **Put the target into the state you want to investigate.** Crucible measures
   the state it finds and never creates it. For example, the pool-starved case
   is `spring.datasource.hikari.maximum-pool-size=2` on perf-lab's
   `perftest_sandbox` branch, deployed to Box A.
2. **Check the workspace.** `~/perf-lab` should be the perf-lab checkout that
   tracks `perftest_sandbox`.
3. **Run the two read-only checks:**

   ```bash
   cd ~/crucible
   uv run crucible plan         # what it may change, what it may never touch
   uv run crucible preflight    # can it reach the target, read metrics, see the deployed commit
   ```

   Fix anything preflight marks `FAIL` before you go on.

### 3.2 Start the run

```bash
uv run crucible run --run-id demo-1 --workspace ~/perf-lab \
    --provider gemini --model gemini-3.5-flash-lite
```

Each measurement takes about 7 minutes: 2 minutes of warm-up thrown away, then
5 minutes measured. A campaign is one baseline plus up to 5 experiments, each
re-measured.

While it runs, the **Live campaign** screen shows it. When the agent has a
proposal, the screen shows a card with the cause, the evidence and the exact
change.

### 3.3 Approve, reject, abort

Use a second SSH session on Box B for these:

```bash
uv run crucible status demo-1                                  # what is waiting
uv run crucible approve demo-1 --experiment 1 --as <your-name>  # apply exactly what was proposed
uv run crucible approve demo-1 --experiment 1 --as <your-name> --reject
uv run crucible abort demo-1                                   # stop; roll the box back to the last good commit
```

You can also approve or skip from the Live campaign card. Paste the control
token into the sidebar first, and only do that over the SSH tunnel.

Approval is bound to the exact values shown. There is no way to approve a
different value; a different value means a new proposal.

### 3.4 Afterwards

```bash
uv run crucible report --run demo-1           # why it changed what it changed, and the limits of the result
uv run crucible diff --a demo-1 --b demo-2    # compare two runs; refuses if their setups differ
```

In the UI, the same results are on the **Campaign** and **History** pages.

---

## 4 · Where things live in the UI

| Page | Use it to |
|---|---|
| **Home** | See active campaigns, recent results, quick-start buttons |
| **New** | Review configuration, copy the `crucible run` command |
| **Campaign** | Watch a live run; approve or reject proposals; pause or abort |
| **History** | Browse all past campaigns; compare two with `crucible diff` |
| **Benchmark** | See replay scores, task list, fixture list |
| **Settings** | Workspace config, SLA, profile, CLI reference |

Everything else is exposed through the CLI (run `crucible --help`).

---

## 5 · When something does not work

| Symptom | Check |
|---|---|
| Page will not load on the public URL | On Box B, `curl localhost:8765`. If that fails, run `systemctl status crucible-ui` and `journalctl -u crucible-ui -n 50`. |
| Loads on Box B but not from outside | `sudo iptables -L INPUT -n \| grep 8765`, then the Oracle ingress rule, including its source IP. |
| Locally, `address already in use` | Another server holds 8765. Use `--port 8766`. |
| Approve says **401** | Wrong control token. |
| Approve says **503** | No `CRUCIBLE_CONTROL_TOKEN` in the server's `.env`. |
| Approve says **409** | Already decided, or the values did not match the proposal. |
| `crucible run` fails at the baseline | The target is not reachable, or not in the expected state. Run `crucible preflight`. |
| `crucible bench` refuses the task set | A task names a fixture not yet captured. Add `--skip-missing-fixtures` to skip those tasks. |
| The first model call is slow | The hosted gateway spins down when idle. The first call waits for it to wake, which can take up to about a minute. |
