# Crucible User Guide

This guide takes you from nothing to a finished tuning campaign using the
Crucible web UI. It assumes you own a service, you know roughly how it is
deployed, and you have never used Crucible.

What Crucible does, in one paragraph: it puts your service under a fixed load,
reads the service's own metrics, works out why it misses its latency goal,
proposes **one** configuration change, waits for **you** to approve that exact
change, applies and deploys it, checks that your service is really running it,
measures again under the same load, and keeps the change only if the new
measurement is better by more than normal run-to-run noise. Otherwise it puts the
old value back.

Contents:

1. [Before you start: what Crucible needs](#1-before-you-start-what-crucible-needs)
2. [Onboarding your app (one-time setup)](#2-onboarding-your-app-one-time-setup)
3. [Opening the UI](#3-opening-the-ui)
4. [A tour of the pages](#4-a-tour-of-the-pages)
5. [Your first campaign, step by step](#5-your-first-campaign-step-by-step)
6. [While a campaign runs: approve, pause, abort, resume](#6-while-a-campaign-runs)
7. [After a campaign: reports, comparisons, scores](#7-after-a-campaign)
8. [Going further: ceiling probes, captures and the benchmark](#8-going-further)
9. [Troubleshooting](#9-troubleshooting)
10. [What Crucible will never do](#10-what-crucible-will-never-do)

---

## 1. Before you start: what Crucible needs

| You need | Why | Example in this project |
|---|---|---|
| A **pre-production** copy of your service | Crucible changes configuration and restarts the service. It refuses to run against anything declared `production`. | PerfLab on Box A (Oracle Cloud) |
| A **git repository** for that service, and a branch Crucible may push to | Every change is a commit. Crucible writes to one sandbox branch and nothing else. | `github.com/rraghu214/perf-lab`, branch `perftest_sandbox` |
| A **deploy path** from a push to a running service | So a change can be put in force without you. Manual deploy also works; Crucible then waits for you. | A git post-receive hook on Box A that rebuilds and restarts |
| A way for the service to **say which commit is running** (or to read the changed setting back) | Crucible will not measure until it can prove the change is live. | `GET /api/version` returns `{"commit": "<sha>"}` |
| **Metrics** Crucible can read | Diagnosis needs evidence. | Spring Boot Actuator (also Prometheus or Datadog) |
| A **machine to run Crucible and the load generator**, close to the service | Load must come from somewhere stable, and not from the service's own box. | Box B |
| Access to a **model gateway** | Diagnosis is the one step that uses a language model. | `glc_v5` at `https://glc-v5-rraghu214.onrender.com` |

Crucible currently understands two kinds of service: **Spring Boot**
(`config/profiles/spring-boot.yaml`) and **FastAPI** (`config/profiles/fastapi.yaml`;
declared, not yet used against a live target).

---

## 2. Onboarding your app (one-time setup)

Onboarding is mostly writing down facts about your service in two files. The UI
reads these files and shows them to you; it does not let you or the agent edit
them, because they are the rules the agent runs under.

### 2.1 Install Crucible on the load box

```bash
git clone https://github.com/rraghu214/crucible.git ~/crucible
cd ~/crucible
git checkout capstone/perf-agent
uv sync --group load      # --group load installs Locust, the load generator
```

### 2.2 Check out your service's repository next to it

```bash
git clone <your service repo> ~/perf-lab
cd ~/perf-lab
git remote add perftest <user>@<target-box>:perflab-deploy.git   # where pushes deploy from
```

This checkout is Crucible's **workspace**. Crucible edits one config file in it,
commits, and pushes to the `perftest` remote.

### 2.3 Describe the goal: `config/slo.yaml`

This file is **the SLA**. It says what "good enough" means and where the service
lives. The agent can read it but can never change it.

```yaml
name: perflab-db-latency
environment:
  name: oracle-ashburn-boxa
  kind: pre-prod                     # production is refused
  target_base_url: http://10.0.0.79:8080
objective:
  endpoint: /api/db
  p99_ms: 120                        # the goal
  error_rate_pct: 1.0
  at_load: { users: 50, spawn_rate: 10, think_time_s: [0.1, 0.3] }
noise:
  p99_spread_pct: 2.08               # measured on THIS box (see below)
  measured_on: 2026-09-12
host_contention:
  cpu_steal_abort_pct: 10.0          # shared cloud CPUs: abort if a neighbour steals more
```

**The noise floor is the one number you must measure yourself.** Run the same
load three times with nothing changed and work out how much p99 varies. On the
Oracle box it was 2.08%; on a Windows laptop it was 14.3%. A later change that
moves p99 by less than this is reported as **inconclusive** and undone, because
it can't be told apart from noise. `crucible capture --revalidate 3` runs those
three measurements for you.

### 2.4 Describe the service: the target profile

`config/profiles/spring-boot.yaml` is **the profile**. It grants the agent its
authority:

| Section | What it says |
|---|---|
| `cause_families` | The names of problems this kind of service can have (pool exhaustion, GC pressure, slow queries...). |
| `config_file` | The one file Crucible edits, relative to the workspace. |
| `allowed_properties` | The only settings the agent may change, each with a type and a range. |
| `protected_paths` | Files the agent can never write (the SLA, the profiles, the load profile, the tests). |
| `deploy` | How a change reaches the service: `mode` (`pipeline` or `manual`), the remote, the sandbox branch, the URL that reports the running commit. |
| `restart` | How to restart locally, if there is no pipeline. |
| `gauges`, `snapshot_metrics`, `promql`, `datadog` | Which metrics to read, under each backend's names. |
| `redaction_allowlist` | The only configuration values allowed into a snapshot. Everything else (passwords, keys) is dropped before the model or the journal sees it. |

The sandbox branch can't be `main`, `master`, `develop`, `trunk`, `release/*` or
`hotfix/*`, and it can't be the same as `base_branch`. A profile that breaks
this rule is rejected when it's loaded.

A sibling file, `spring-boot.SKILL.md`, describes how this kind of service fails.
It goes into the model's instructions as background, and it grants nothing.

### 2.5 Point Crucible at the model gateway

Put this in `~/crucible/.env`:

```bash
GLC_BASE_URL=https://glc-v5-rraghu214.onrender.com
```

Optional, only if you'll use them:

```bash
DATADOG_API_KEY=...        # for Datadog captures; never typed into the UI
DATADOG_APP_KEY=...
CRUCIBLE_WORKSPACE=~/perf-lab     # pre-fills the workspace box in the UI
CRUCIBLE_CONTROL_TOKEN=...        # required for all write actions (launch, approve, abort, pause)
```

---

## 3. Opening the UI

Two UIs are available. Use the SPA for demos; use NiceGUI for live campaign operations.

| UI | Command | Port | Best for |
|---|---|---|---|
| **SPA demo UI** | `uv run crucible serve` | 8113 | Demo recordings, overview, History & Diff |
| **NiceGUI production UI** | `uv run crucible serve-ui` | 8765 | Live campaigns, approvals, benchmark |

### SPA demo UI (polished, at `/dashboard`)

```bash
# On Box B, inside tmux
cd ~/crucible
set -a; . ./.env; set +a
uv run crucible serve

# On your laptop — tunnel to port 8113
ssh -i <key> -L 9113:127.0.0.1:8113 ubuntu@<load-box-ip>
```

Open **http://localhost:9113/dashboard**. The chat pane has a **Live AI** toggle
in its header: when OFF responses are scripted (instant, reliable); when ON the
chat calls GLC v5 live. Toggle to ON only when GLC v5 is warm.

### NiceGUI production UI (seven pages, on SSH tunnel)

```bash
# On Box B, inside tmux
cd ~/crucible
set -a; . ./.env; set +a
uv run crucible serve-ui --port 8765

# On your laptop
ssh -i <key> -L 8765:127.0.0.1:8765 ubuntu@<load-box-ip>
```

Open **http://127.0.0.1:8765**.

### On your own machine

`uv run crucible serve-ui` works locally too, for reading results and replaying
the benchmark. A live campaign needs a load box that can reach the service.

### Open on a network address

`serve-ui --host 0.0.0.0` is **refused** unless `CRUCIBLE_UI_TOKEN` is set. That's
deliberate: this UI can start campaigns and approve changes, so anyone who can
reach it must prove who they are. With a token set, enter it on the **Settings**
page before any button will work. Plain HTTP carries the token unencrypted, so
prefer the tunnel.

### First thing to do

Go to **Settings → You**, type your name, and click **Save**. Your name is
recorded on every approval you make, and the approval dialogs pre-fill it.

---

## 4. A tour of the pages

The bar at the top has seven pages. Every button on them runs a real `crucible`
command; the command line is shown next to its output, so you always know what
ran.

**SPA demo UI pages** (at `localhost:9113/dashboard`):

| Tab | What it's for |
|---|---|
| **Overview** | Four KPIs: runs, best p99, improvement %, AI accuracy. Architecture diagram. |
| **New Test Run** | Wizard: target & repo (doc upload for Knowledge RAG), goal & SLA, scenarios, mode & budget, review. |
| **Active** | Polls `GET /v1/perf/campaign/active` every 5 s. When a real campaign is running: live run-id, status, elapsed, real approval gate, job log. When idle: TR-004 demo view. |
| **Results** | Loads latest run from `GET /v1/perf/runs/latest/report` on open. Shows real KPIs, Chart.js Before/After bar chart, experiment table. ↺ refreshes; ▼ Pick run switches campaigns. |
| **History & Diff** | Live campaign history table (fetches from `/v1/perf/screens/17`), compare two runs. |
| **Benchmarks** | Replay accuracy, fixture table, cause-family breakdown, LUCKY quadrant. |
| **Guardrails** | Always-enforced rules + property bounds from profile.yaml. |
| **Preflight** | Pre-flight checklist, CLI command twin. |

**NiceGUI production UI pages** (at `localhost:8765`):

| Page | What it's for |
|---|---|
| **Home** | Anything waiting on you, campaigns running now, recent campaigns, recent jobs. |
| **New** | Check the setup (Plan, Preflight), start a campaign, start a ceiling probe. |
| **Campaign** | Decisions waiting on you, running campaigns with Pause and Abort, paused campaigns with Resume, the latest campaign's experiments. Refreshes on its own. |
| **History** | Every campaign; read a report, compare two, score them all. |
| **Benchmark** | Replay results by task class; run the replay benchmark; capture fixtures. |
| **Jobs** | Every command started from the UI, its status, and its full output. |
| **Settings** | Your name, the control token, the configuration in force, Init and Export. |

**Jobs and their status.** Each action becomes a job:

- **RUNNING:** still going.
- **EXIT 0:** finished successfully.
- **EXIT 1 / EXIT 2:** finished with a failure (1) or a refusal (2); the output
  says why. An argument the CLI couldn't parse also shows as EXIT 2.
- **UNKNOWN:** started by an earlier UI session and not yet finished, so this UI
  can't tell. This happens on Windows only; on Linux the status is checked
  properly.

A job keeps running if you close the browser or restart the UI, and its output
stays on the Jobs page.

**Confirmations.** Anything that drives load against your service, changes it
or restarts it asks first, naming the target and environment: starting a
campaign, resuming, a ceiling probe, a live capture, and preflight with the
apply probe. Abort explains what it keeps and what it throws away before it
runs.

---

## 5. Your first campaign, step by step

### Step 1: put your service into the state you want to investigate

Crucible measures the service as it finds it. It never breaks it for you.
For the PerfLab demo that means `maximum-pool-size=2` deployed on the sandbox
branch. Check that this is what's running before you start.

### Step 2: Plan (changes nothing)

**New → 1 · Check first → Plan.**

The output lists:

- every property the agent may change, with its range;
- every path it may never write;
- the noise floor;
- where a deploy would go (remote and branch);
- whether deploys are automatic or manual.

Read it once. This is the authority you're about to hand over.

### Step 3: Preflight (touches the target, read-only by default)

**New → Preflight.** Checks that:

- the environment isn't production;
- the SLA and load profile are protected;
- metrics are readable;
- the service reports its running commit;
- the deploy configuration is valid.

Fix anything marked `FAIL` first.

Tick **apply probe** only for a first contact with a new environment. It makes a
harmless change, restarts the service and reverts it, so you know deploy and
revert work before a campaign relies on them.

### Step 4: Start the campaign

**New → 2 · Start a campaign.** The defaults are sensible:

| Field | Default | Meaning |
|---|---|---|
| profile | spring-boot | Which profile from `config/profiles/` |
| SLA file | config/slo.yaml | The goal |
| scenario | db-latency | The load pattern (named for the record) |
| workspace | `$CRUCIBLE_WORKSPACE` or `.` | Your service's checkout |
| run id | blank | Blank generates one; give it a name you'll recognise later |
| users / warmup / measure | 50 / 120 s / 300 s | Warm-up is discarded; only the measured window counts |
| max experiments | 5 | At most five changes are tried; a guard refusal doesn't use one up |
| model provider / model | gemini / (env) | Pinned for the whole campaign |
| Tracing | empty | Optional Jaeger. Without it, the report says traces were never checked. |
| stakeholder request | empty | Free text from whoever asked. Shown to the model as context only; the SLA stays the authority. |
| Approval | **Ask me for every change** | Keep this. "Do not ask" is recorded on the record as preapproved. |
| approval timeout | 3600 s | If nobody answers in time, the campaign **pauses**. Nothing is applied and nothing measured is lost. Answer the card, then Resume (see §6). |

Click **Start campaign**, read the confirmation (target, environment, the exact
command) and click **Run it**. The output appears below. Leave the page if you
like; the campaign carries on, and **Jobs** has its output.

What happens next, in order:

1. **Baseline** (~7 minutes): warm-up, then the measured window, with gauges
   sampled every second.
2. **If the goal is already met, it stops** and says so. Nothing to fix is a
   correct result.
3. **Diagnosis:** the model reads the converted snapshot and names a cause.
4. **Guard:** the proposal is checked against the profile. A property that isn't
   allowed, or a value out of range, is refused before you ever see it, and
   recorded.
5. **Your approval:** the campaign waits for you (next section).

---

## 6. While a campaign runs

Open **Campaign**. The page refreshes itself whenever something changes.

### Approving or rejecting a change

A card marked **AWAITING APPROVAL** shows:

- the run and experiment number;
- the **cause** the agent named;
- the **change**, as property, current value and new value (e.g.
  `maximum-pool-size -> 20`, shown with the current value 2);
- its **confidence** and **predicted p99**, labelled as claims, not results;
- the evidence it cited and its reasoning.

Click **Approve** or **Reject**. The dialog repeats exactly what you're
answering. There is no box for a different value. To try another value, reject,
and the agent proposes again. Your name is recorded with the answer.

After approval, Crucible:

1. edits the one config file, commits, and pushes to the sandbox branch;
2. waits until the service reports the new commit (and reads the changed
   setting back where it can). **It does not measure until this is proven;**
3. measures again under identical load (~7 minutes);
4. compares old and new p99 against the noise floor:

   | Verdict | Meaning | What happens |
   |---|---|---|
   | **IMPROVED** | Faster by more than the noise floor | Kept. Becomes the new starting point. |
   | **INCONCLUSIVE** | Moved less than the noise floor | Reverted |
   | **WORSE** | Slower by more than the noise floor | Reverted |
   | **NOT_MEASURED** | Couldn't be measured (e.g. deploy not proven) | Campaign stops |
   | **ABORTED** | The watchdog stopped the measurement | Campaign stops; target rolled back |

5. stops when the goal is met, or tries the next experiment, up to the maximum.

### If you missed an approval

If nobody answers within the approval timeout (an hour by default), the campaign
**pauses**. Nothing is applied, every experiment already measured is kept, and
the proposal stays parked. To carry on:

1. Approve or reject the card that is still under **Waiting on you**.
2. Click **Resume** under **Paused**.

Resume re-parks the *same* proposal rather than asking the model again, so your
answer applies to exactly what you were shown. If you resume without answering,
it waits for you again.

### "MANUAL STEP" cards

If a deploy or restart is set to manual, the campaign pauses with instructions.
Do the step, then click **I have done this**. This only reports that you did it;
it approves nothing, and Crucible checks the service again before measuring.
The record notes that a human stepped in.

### Pause, abort, resume

| Button | When | What it does |
|---|---|---|
| **Pause** | Something unrelated needs fixing mid-campaign | Stops at the next experiment boundary. Keeps everything measured; resumable. |
| **Abort** | Stop for good | Stops at the next boundary, discards the in-flight experiment, keeps verified ones, and **rolls the service back** to the last commit it confirmed running. |
| **Resume** (Paused section) | After a pause | Continues where it stopped. Leave the fields blank to reuse the original settings. |
| **Clear** (Requested section) | You changed your mind before the campaign reached the boundary | Removes the pause or abort request. |

Neither pause nor abort stops a change half-way through being applied. Stopping
in the middle could leave the service in a state nothing on record describes.

---

## 7. After a campaign

### Read the report

**History → Report**, choose the run, **Show report**. The report contains:

- **Headline:** what was tried, what was kept, p99 before and after, against the goal.
- **Experiments:** each change, its verdict, and how many noise floors it cleared.
- **Measurement:** p50/p95/p99, throughput and error rate, before and after.
- **Calibration:** what the agent predicted against what was measured. A
  prediction is never the result.
- **Limits of this result:** what was not checked (no tracing, one instance, one
  measurement per state...). This is generated from the record, not written by
  hand, so it stays honest.
- **Reproduction:** model, harness commit, collector version, environment,
  noise floor, approvals, deployed commits.

### Compare two campaigns

**History → Compare two campaigns**, choose A and B, **Diff**.

- If the **scenario** or the **load profile** differ, the comparison is
  **refused**: the numbers measured different things.
- Any other difference is **listed above the numbers** and the comparison goes
  ahead: environment, SLA, profile, collector version, noise floor, model. Read
  the numbers with those in mind.
- No "difference" figure is calculated. You see each campaign's own before and
  after.

### Score the journal

**History → Score.** Scores every campaign on disk by:

- outcome;
- diagnosis accuracy (needs a ground-truth file);
- integrity;
- efficiency;
- calibration;
- cost.

It never calls a model, so scoring is free and repeatable.

---

## 8. Going further

### Ceiling probe: "how much can it take?"

**New → 3 · Ceiling probe.** When your service already meets its goal, this
raises the load in steps until the goal is missed, and reports the **knee**: the
last level that passed and the first that failed. It changes the load profile,
which the agent may never do, so only you can start it, and you set the bounds
(min users, max users, step size). The maximum users field is required.

A ceiling result describes the service's capacity; it is never presented as a
verdict on a change. In the SPA demo UI it appears as the fourth mode card in
the Budget step of the New Test Run wizard.

CLI equivalent:
```bash
uv run crucible ceiling --min-users 50 --max-users 300 --step-size 50 \
    --run-id demo-ceiling
```

### Capture fixtures and run the replay benchmark

A **fixture** is one state of your service (say, pool=2), captured once and
saved with its true cause written down. The **replay benchmark** asks the model
to diagnose saved fixtures. That costs seconds and fractions of a cent, with no
live service, so you can test the diagnosis on many states cheaply.

**Benchmark → Capture a fixture:**

1. Put the service in the fixture's state yourself. Crucible never breaks the
   service for you.
2. Choose the fixture.
3. **Show capture plan** first; it changes nothing.
4. Then **Capture**.

"All configured providers" captures Actuator, Prometheus (give its URL) and
Datadog (keys come from the environment, never the form) from a single load, so
you can check the three backends agree.

**Benchmark → Run bench** replays the task set against the captured fixtures.
The page grades the latest run the same way `crucible score` does: an error is
"not graded", never a wrong answer.

---

## 9. Troubleshooting

| What you see | What to check |
|---|---|
| A write action (launch, approve, abort, pause) returns 401 | Token not saved in Settings → Control token. Paste `CRUCIBLE_CONTROL_TOKEN` value and click Save. |
| A write action returns 503 | `CRUCIBLE_CONTROL_TOKEN` not set in Box B `.env`. Add it and `sudo systemctl restart crucible-ui`. |
| `serve-ui` exits with "refusing to serve the UI on '0.0.0.0'" | Set `CRUCIBLE_CONTROL_TOKEN`, or bind to 127.0.0.1 and use the tunnel. |
| Active tab shows TR-004 but a campaign is running | Poll endpoint unreachable — check `curl 127.0.0.1:8113/v1/perf/campaign/active` on Box B. |
| Results tab shows demo data after a campaign finished | No manifest on disk yet — check `curl .../v1/perf/runs`; may need a full campaign to complete first. |
| A job shows **EXIT 2** right away | It was refused (`2` is the CLI's "refused" code; `1` is a failure). The output says why: production environment, missing SLA, an unsafe comparison... |
| The campaign fails at the baseline | The service isn't reachable from the load box, or not in the state you expect. Run Preflight. |
| `git push failed ... (fetch first)` in the output | Your workspace is behind the deploy remote. In the workspace: `git pull perftest perftest_sandbox`, then start again. |
| Stuck before measuring after a deploy | The service hasn't reported the new commit. Check its deploy log and `/api/version`. |
| The first diagnosis takes a minute | The hosted gateway sleeps when idle and is waking up. |
| A campaign was killed and a new one won't start | A lock is still held. Check Home → Active campaigns; if nothing is really running, remove `~/.crucible/locks/<branch>.lock`. |
| Datadog values are all empty | The service isn't pushing to Datadog. On the service box, check its log for Datadog errors and that the keys are exported to the process. |
| A job shows **UNKNOWN** | Started by an earlier UI session on Windows. It will show EXIT n once it finishes. |

---

## 10. What Crucible will never do

- Run against an environment declared `production`.
- Change the SLA, the load profile, the tests, or its own profile.
- Change a setting that isn't on the profile's allowed list, or set one outside its range.
- Apply a change you haven't approved, unless you started the campaign with
  "Do not ask", which is then written on the record.
- Push anywhere except the one sandbox branch, or force-push.
- Measure before it has proven your service is running the change.
- Call a result an improvement when it's inside the measured noise.
- Put a password or API key into a snapshot, a prompt or the journal.
