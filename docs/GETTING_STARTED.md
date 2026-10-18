# Getting started with Crucible

Crucible is an autonomous performance engineer. It runs a controlled load test
against a service, works out why the service misses its latency goal, proposes
**one** configuration change, waits for a human to approve it, applies it,
measures again, and keeps the change only if the measurement proves it helped.

You can use it in two places:

| | Where | What you can do there |
|---|---|---|
| **Locally** | your own machine | Browse the UI, read benchmark results, replay saved snapshots against the model. Live campaigns require the target host to be reachable. |
| **Runner host** | the machine running Crucible alongside the load generator | Everything: live campaigns, approvals, and the UI other people can open. |

**Two UIs are available:**

| UI | Command | Port | URL | Use for |
|---|---|---|---|---|
| **SPA demo UI** | `uv run crucible serve` | 8113 | `localhost:9113/dashboard` | Demo recording, launching runs, approvals, Jobs log, History & Diff, AI chat |
| **NiceGUI production UI** | `uv run crucible serve-ui` | 8765 | `localhost:8765` | Alternative campaign UI with richer live charts |

The SPA's write actions (launch, approve, pause, abort, ceiling probe, bench) call
the same REST API as the CLI. Set `CRUCIBLE_CONTROL_TOKEN` in your `.env` and paste
the same value in the SPA's **Settings → Control token** field before using them.

The UI does everything the command line does, by running the same `crucible`
commands for you. A step-by-step walkthrough is in
[`docs/myfolder/UserGuide.md`](myfolder/UserGuide.md).
For the demo recording script, see
[`docs/myfolder/DEMO_SCRIPT.md`](myfolder/DEMO_SCRIPT.md).

---

## 1 · Locally

### 1.1 What you need

- Python and [uv](https://docs.astral.sh/uv/) installed
- A checkout of this repository on the working branch
- A `.env` file in the repository root. Copy `.env.example` if you do not have
  one. The UI starts without any model gateway.

### 1.2 Start the app

**SPA demo UI** (for demos and presentations):
```powershell
cd <path-to>\crucible
git pull
uv sync
uv run crucible serve
```
Open **http://127.0.0.1:8113/dashboard**.

**NiceGUI production UI** (for live campaigns and full workflow):
```powershell
uv run crucible serve-ui
```
Open **http://127.0.0.1:8765**. Press Ctrl+C in that terminal to stop it.

If port 8765 is taken, run `uv run crucible serve-ui --port 8766` instead.

### 1.3 What you will see locally

| Page | What it shows on your machine |
|---|---|
| **Home** | Service config, any active campaigns, recent results |
| **New** | The exact `crucible run` command for the current config |
| **Campaign** | Live state, proposals awaiting approval |
| **History** | Campaign manifests; copy `results/<run>.json` from the runner host to populate it |
| **Benchmark** | Replay results, task table, fixture table |
| **Settings** | Workspace config, SLA, profile, collector version, CLI reference |

### 1.4 Things that do work locally

**See what a campaign would do.** This reads config and changes nothing:

```powershell
uv run crucible plan
```

**Replay the benchmark against saved snapshots.** This needs the model gateway
but not the target:

```powershell
$env:GLC_BASE_URL = "https://<your-gateway-url>"
uv run crucible bench --tasks <folder-with-task-yamls> --fixtures <fixtures-folder> `
    --provider gemini --model gemini-2.5-flash --out results/replay.json
```

Pass a folder holding only the tasks whose fixtures have been captured. The
`--skip-missing-fixtures` flag skips tasks whose fixture files are not found.

**View a campaign that ran on the runner host.** Copy its `results/<run-id>.json`
from the runner host into `results/` on your machine and refresh the page.
Report, History, Plan graph and Watchdog then show it.

---

## 2 · Runner host: the app always running at a public URL

Everything in this section runs **on the runner host** over SSH, unless it says
otherwise. Replace `<user>` with your login user and `<runner-host-ip>` with
the public IP of the runner host.

### 2.1 Get the latest code

```bash
cd ~/crucible
git fetch origin
git checkout <working-branch>
git pull
uv sync
```

The `.env` on the runner host should contain:

- `GLC_BASE_URL=https://<your-gateway-url>`
- `CRUCIBLE_UI_TOKEN=<a-secret-you-choose>`, if the UI is to listen on anything
  but 127.0.0.1 (see §2.5 for why)

### 2.2 Run the UI as a service, so it survives logouts and reboots

Create `/etc/systemd/system/crucible-ui.service`. Check the uv path with
`which uv`.

```ini
[Unit]
Description=Crucible UI
After=network-online.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/crucible
EnvironmentFile=/home/<user>/crucible/.env
ExecStart=/home/<user>/.local/bin/uv run crucible serve-ui --host 0.0.0.0 --port 8765 --root /home/<user>/crucible
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

**Cloud firewall (security group / network security group):** add an ingress rule:

- Protocol: TCP
- Destination port: `8765`
- Source: `0.0.0.0/0` for anyone, or **`<your-ip>/32` to allow only yourself**.
  Prefer the second.

**On the host itself:**

```bash
sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT
sudo netfilter-persistent save
```

### 2.4 Open it in a browser

**http://\<runner-host-ip\>:8765**

Share that link with anyone you want to watch, for example your trainer.

### 2.5 What a public URL exposes

- **Viewers can read every page.** That includes the SLA, profile and target
  host addresses. If these are sensitive, restrict the source IP (§2.3) or
  use the tunnel (§2.6).
- **Viewers cannot change anything without the control token.** Every write
  action (start, approve, abort) is refused until the token has been entered
  on the Settings page.
- **The URL is plain HTTP.** Do not paste the control token into the page over
  the public URL: it would cross the internet unencrypted. Approve from your
  SSH session instead (§3.3), or open the UI via an SSH tunnel (§2.6) where
  the connection is encrypted.

### 2.6 Private alternative: an SSH tunnel

Use this when nobody else needs to see the UI. It needs no open port and no
control token, because the connection never leaves the encrypted SSH channel.
Run this **on your own machine**:

**SPA demo UI (port 8113 on Box B → local port 9113):**
```powershell
ssh -i <path-to-runner-host-key> -L 9113:127.0.0.1:8113 <user>@<runner-host-ip>
```
Browse to **http://localhost:9113/dashboard**.

**NiceGUI production UI (port 8765 on Box B → local port 8765):**
```powershell
ssh -i <path-to-runner-host-key> -L 8765:127.0.0.1:8765 <user>@<runner-host-ip>
```
Browse to **http://localhost:8765**. Over the tunnel it is safe to paste the
control token into the sidebar.

---

## 3 · Running a campaign (on the runner host)

### 3.1 Before you start

1. **Put the target into the state you want to investigate.** Crucible measures
   the state it finds and never creates it.
2. **Check the workspace.** `<workspace>` should be the checkout of the target's
   repository on its sandbox branch.
3. **Run the two read-only checks:**

   ```bash
   cd ~/crucible
   uv run crucible plan         # what it may change, what it may never touch
   uv run crucible preflight    # can it reach the target, read metrics, see the deployed commit
   ```

   Fix anything preflight marks `FAIL` before you go on.

### 3.2 Start the run

```bash
uv run crucible run --run-id demo-1 --workspace <workspace> \
    --provider gemini --model gemini-2.5-flash
```

Each measurement takes about 7 minutes: 2 minutes of warm-up thrown away, then
5 minutes measured. A campaign is one baseline plus up to 5 experiments, each
re-measured.

While it runs, the **Live campaign** screen shows it. When the agent has a
proposal, the screen shows a card with the cause, the evidence and the exact
change.

### 3.3 Approve, reject, abort

Use a second SSH session on the runner host for these:

```bash
uv run crucible status demo-1                                      # what is waiting
uv run crucible approve demo-1 --experiment 1 --as <your-name>    # apply what was proposed
uv run crucible approve demo-1 --experiment 1 --as <your-name> --reject
uv run crucible abort demo-1                                       # stop; roll the target back
```

You can also approve or reject from the Live campaign card in the UI. If using
the public URL (§2.4), read §2.5 first. Over the SSH tunnel (§2.6) it is safe
to paste the control token and use the UI directly.

Approval is bound to the exact values shown. There is no field to type a
different value; a different value means a new proposal.

### 3.4 Afterwards

```bash
uv run crucible report --run demo-1           # why it changed what it changed, and the limits of the result
uv run crucible diff --a demo-1 --b demo-2    # compare two runs; refuses if their setups differ
```

In the UI, the same results are on the **Campaign** and **History** pages.

---

## 4 · Where things live in the UI

| Page | Use it to | CLI commands it runs |
|---|---|---|
| **Home** | See what needs you, active and recent campaigns, recent jobs | — |
| **New** | Check first, start a campaign, start a ceiling probe | `plan`, `preflight`, `run`, `ceiling` |
| **Campaign** | Approve or reject, confirm manual steps, pause, abort, resume | `status`, `approve`, `pause`, `abort`, `resume` |
| **History** | Read a report, compare two campaigns, score the journal | `report`, `diff`, `score` |
| **Benchmark** | Replay scores; run the replay benchmark; capture a fixture | `bench`, `capture` |
| **Jobs** | The output of everything started from the UI | — |
| **Settings** | Your name, the control token, configuration, init, export | `init`, `export` |

---

## 5 · When something does not work

| Symptom | Check |
|---|---|
| Page will not load on the public URL | On the runner host, `curl localhost:8765`. If that fails, run `systemctl status crucible-ui` and `journalctl -u crucible-ui -n 50`. |
| Loads on the runner host but not from outside | `sudo iptables -L INPUT -n \| grep 8765`, then the cloud firewall ingress rule, including its source IP. |
| Locally, `address already in use` | Another server holds 8765. Use `--port 8766`. |
| Approve says **401** | Wrong control token — check Settings → Control token matches `CRUCIBLE_CONTROL_TOKEN` in the server `.env`. |
| Approve says **503** | No `CRUCIBLE_CONTROL_TOKEN` in the server's `.env`. Add it and restart `crucible serve`. |
| Launch / Preflight / Abort returns **401** | Same as above — all write actions use the same token. |
| Approve says **409** | Already decided, or the values did not match the proposal. |
| `crucible run` fails at the baseline | The target is not reachable, or not in the expected state. Run `crucible preflight`. |
| `crucible bench` refuses the task set | A task names a fixture not yet captured. Add `--skip-missing-fixtures` to skip those tasks. |
| The first model call is slow | The gateway spins down when idle. The first call waits for it to wake, which can take up to about a minute. |
