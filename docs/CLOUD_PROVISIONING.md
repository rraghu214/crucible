# Cloud provisioning — week 1, items 2 and 3

**Status: not started. This is the critical path.** Everything else in week 1 is
done and committed; weeks 2–4 all assume a box exists. Week 2's "one full
campaign verified end to end" has nowhere to run without it.

This document is written to be executed in its own chat. The prompt to start
that chat is at the bottom.

---

## 1 · Why two boxes, not one

This is a measurement-integrity requirement, not a convenience.

If Locust runs on the same box as the target, the load generator competes with
the JVM for CPU. The p99 you measure is then partly about the load generator,
and every experiment-to-experiment comparison inherits that noise. `DESIGN.md`
§6 makes this explicit: the watchdog carries a **host contention tripwire** that
aborts a run when CPU steal exceeds 5%, because *"better to declare the
measurement invalid than report a p99 that was about someone else's workload"*.
Co-locating the load generator guarantees that condition instead of detecting
it.

So: **target on box A, Crucible + Locust on box B.**

| | Box A — target stack | Box B — Crucible + Locust |
|---|---|---|
| Purpose | the service under test | the agent and the load generator |
| Containers | Spring Boot ~800 MB, Postgres ~256 MB, Redis ~64 MB, Prometheus ~256 MB, Jaeger ~256 MB, httpbin ~64 MB | — |
| RAM needed | ~1.7 GB + headroom → **4 GB comfortable** | ~1 GB → **2 GB comfortable** |
| Notes | week 1 only needs Postgres, Redis, httpbin (see `perf-lab/docker-compose.yml`); Prometheus and Jaeger arrive weeks 2–3 | needs outbound HTTPS to the model gateway |

`DESIGN.md` §16 sizes this as Oracle's free allowance split across two VMs, or a
single Hetzner CX32.

---

## 2 · Path A — Oracle Cloud Always Free (try first)

`DESIGN.md` §16 says Oracle first, *"Hetzner immediately on capacity failure"*.
That instruction exists because Oracle's free ARM capacity is very often
exhausted in popular regions — the failure is `Out of host capacity`, and it can
persist for days. **Do not spend more than one evening fighting it.**

### Steps

1. **Create the account** at `cloud.oracle.com`. A card is required for identity
   verification; Always Free resources are not charged. Pick your home region
   carefully — **it cannot be changed later**, and free ARM capacity varies a lot
   between regions.
2. **Check the current Always Free allowance** on Oracle's own page before
   sizing anything. Oracle has changed these terms more than once, so treat any
   number in this document as needing confirmation. What we need is a total of
   roughly 6 GB RAM and 3 cores across two instances.
3. **Create instance A (target).** Ampere A1 (ARM) shape if available — it is the
   generous one. Ubuntu 22.04 or 24.04 LTS. Allocate ~4 GB RAM / 2 OCPU.
4. **Create instance B (Crucible + Locust).** ~2 GB RAM / 1 OCPU. Same region,
   same VCN, so A↔B traffic stays on the internal network.
5. **If you hit `Out of host capacity`** — try one or two other availability
   domains in your region, then **stop and switch to Path B.** That is the
   documented decision, not a fallback to improvise.

### The Oracle firewall gotcha

Oracle blocks traffic in **two** independent places, and opening one is the
single most common reason a new instance looks broken:

1. **Security List / Network Security Group** in the VCN console — add ingress
   rules for the ports you need.
2. **The instance's own iptables** — Oracle's Ubuntu images ship with rules that
   drop almost everything. Ubuntu images also persist them via
   `netfilter-persistent`.

You must open both. Symptom of opening only one: `curl` works from the box
itself but times out from anywhere else.

---

## 3 · Path B — Hetzner CX32 (the reliable fallback)

Roughly ₹1,000 for the capstone period (`DESIGN.md` §18), and it provisions in
about a minute with no capacity lottery. A CX32 (4 vCPU / 8 GB) can host **both**
roles if you keep them apart with CPU pinning — but two smaller boxes (CX22)
are truer to the measurement requirement in §1 and cost about the same.

**Recommendation: two CX22 instances.** Cheaper to reason about, and it removes
the co-location question entirely.

### Steps

1. Create a project at `console.hetzner.cloud`.
2. Add your **SSH public key** to the project before creating servers — it is
   far easier than fixing access afterwards.
3. Create server A: CX22, Ubuntu 24.04, location close to you (Nuremberg /
   Helsinki / Ashburn / Singapore).
4. Create server B: same, same location — same-location traffic is free and
   low-latency.
5. Note both public IPs and the private network addresses if you attach one.

---

## 4 · Common setup, both boxes

Run on **each** box after it is reachable.

```bash
# 1. Update, and create a non-root user with sudo
sudo apt update && sudo apt -y upgrade
sudo adduser --disabled-password --gecos "" crucible
sudo usermod -aG sudo crucible

# 2. SSH keys only — no password auth
#    /etc/ssh/sshd_config:  PasswordAuthentication no
#                           PermitRootLogin no
sudo systemctl restart ssh

# 3. Swap. Small boxes OOM-kill the JVM without it, and an OOM mid-run
#    looks exactly like a performance cliff in the results.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 4. Docker (box A needs it for the target stack)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker crucible
```

### Box A extras — the target

```bash
sudo apt -y install openjdk-21-jdk git
# perf-lab/docker-compose.yml brings up Postgres, Redis and httpbin
```

### Box B extras — Crucible and Locust

```bash
sudo apt -y install git python3-pip
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Ports

Open only what is needed, and only to where it is needed:

| Port | On | Reachable from | Why |
|---|---|---|---|
| 22 | both | your IP only | SSH |
| 8080 | A | **box B only** | the target service and its Actuator endpoint |
| 5432, 6379, 8081 | A | localhost only | Postgres, Redis, httpbin — never exposed |

Exposing 8080 to the whole internet would let anyone drive load at the target
mid-experiment and silently corrupt a measurement.

---

## 5 · Acceptance checks — do not skip

The box is not "done" until all of these pass. Each one has burned someone.

```bash
# From box B, not from box A — this proves the network path the load
# generator will actually use.
curl -s http://<BOX_A_PRIVATE_IP>:8080/actuator/health          # {"status":"UP"}
curl -s http://<BOX_A_PRIVATE_IP>:8080/api/version              # app + poolSize

# On box A: confirm the metrics the collector depends on exist
curl -s localhost:8080/actuator/metrics/hikaricp.connections.pending
curl -s localhost:8080/actuator/metrics/hikaricp.connections.acquire

# On box A: CPU steal must be near zero at idle. If it is not, the
# measurements are about a neighbouring tenant (DESIGN.md section 6).
vmstat 1 5     # the `st` column

# Core count, both boxes — needed to size the Locust user count sensibly
nproc
```

---

## 6 · Then: re-run K1 (week 1, item 3)

K1 is the measurement-stability kill test. It was passed on a MacBook with a
**14.3% p99 spread**, and `DESIGN.md` §16 requires re-running it on the cloud
box because *"the 14.3% spread is a MacBook number"* — shared vCPU is noisier,
and every later "is this change real or is it noise?" judgement is calibrated
against this number.

Procedure (from `docs/K1_RESULT.md`):

- 3 baseline runs at `maximum-pool-size=10`, 3 bottleneck runs at `=2`
- 50 users, 90 s, against `/api/db`
- Locust runs on **box B**, target on **box A**
- Record p50/p95/p99/rps, and `hikaricp.connections.pending` **sampled during
  the run**, not after — a drained gauge reads 0 and means nothing

**Pass criteria:** baseline p99 spread under 20%, and bottleneck p99 clearly
separated from baseline (it was 8.7x on the MacBook).

Write the result to `docs/K1_CLOUD_RESULT.md`. The new spread becomes the noise
threshold every later verdict is judged against — one per endpoint eventually,
since `/api/churn` (GC) will be noisier than `/api/fast`.

---

## 7 · Prompt for the separate chat

Paste this to start:

```
Read AGENTS.md and DESIGN.md before doing anything.

I am provisioning the cloud boxes for Crucible — week 1 items 2 and 3 of
DESIGN.md section 16. Follow docs/CLOUD_PROVISIONING.md.

Context: weeks 1's code deliverables (collector, runner, TargetProfile, the
expanded PerfLab) are done and committed on capstone/perf-agent. The two
outstanding week 1 items are cloud provisioning and re-running K1 on the
cloud box, and they are the critical path for week 2.

Start by asking me which path I took — Oracle Always Free or Hetzner — and
whether both boxes are reachable over SSH. Then walk me through section 4
(common setup) and section 5 (acceptance checks) one step at a time,
waiting for my output before moving on. Do not assume a step succeeded.

When the acceptance checks pass, help me re-run K1 per section 6 and write
the result to docs/K1_CLOUD_RESULT.md. The new p99 spread replaces the
MacBook's 14.3% as our noise threshold, so it must be measured, not
estimated.

Two standing rules for this work:
- Never restart glc_v5 (port 8111) from inside Claude Code.
- I review any test assertions before they are committed.
```

---

## 8 · Open decisions for Raghu

1. **Oracle or straight to Hetzner?** Oracle is free but the ARM capacity
   lottery can cost days. Hetzner is ~₹1,000 and provisions in a minute. Given
   two days remain in week 1, the schedule argues for Hetzner.
2. **Two small boxes or one CX32?** Two is truer to §1's separation requirement.
3. **Region**, which fixes network latency to both you and the model gateway.
