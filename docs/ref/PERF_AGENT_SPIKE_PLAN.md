# Performance Agent — Feasibility Spike Plan
## Day 1 (K1) + Day 2 (K3) — Claude Code Execution Guide

**Goal:** Answer two kill questions before writing any agent logic.

```
K1 — Is measurement stable enough to detect a real tuning change?
K3 — Can glc_v5 identify the correct cause from raw metrics alone?
```

Both must pass to proceed with Route C.  
If K1 fails → measurement environment is broken, fix it or stop.  
If K3 fails → diagnosis prompt is broken, fix it before orchestrating.

**Working directory:** `C:\Raghu\MyLearnings\EAG_V3\capstone\perf-agent-spike`  
Create this directory before starting. All spike work lives here — do not
put it inside S17Code or S18Code.

**Do not touch:**
- `C:\Raghu\MyLearnings\EAG_V3\S17-15082026\assignment\S17Code\` (read only for reference)
- Any existing glc_v5 running on port 8111 (use it as-is, do not reconfigure)

---

## Pre-flight (10 minutes — do this before Day 1 begins)

Verify the local tool chain. Run each command, record the output in
`docs/PREFLIGHT.md`. If any fails, fix it before continuing.

```bash
# Java
java -version
# expect 17+ ; if not, install Temurin 21 from adoptium.net

# Maven or Gradle — whichever you prefer
mvn -v
# or
gradle -v

# Python (for Locust and the K3 probe)
python --version
# expect 3.10+

# Locust
locust --version
# if missing: pip install locust

# glc_v5 health — must be running on port 8111
curl -s http://127.0.0.1:8111/health | python -m json.tool
# if not running, start it first per its own README

# Docker (optional — only needed if you containerise Postgres)
docker --version
```

Record in `docs/PREFLIGHT.md`:
- Java version
- Python version
- Locust version
- glc_v5 /health response
- Whether Postgres is local, Docker, or embedded H2

---

## Day 1 — K1: Measurement Stability

### Phase 1.1 — Create the Spring Boot Performance Lab

**What to build:** A minimal Spring Boot service with three endpoints and
deliberate injectable bottlenecks. This is the *target application* the agent
will later tune. Build it to be simple, deterministic, and fully controllable.

Create: `perf-lab/` inside the spike directory.

```bash
cd C:\Raghu\MyLearnings\EAG_V3\capstone\perf-agent-spike
# If using Spring Initializr CLI:
curl https://start.spring.io/starter.zip \
  -d dependencies=web,actuator,data-jpa,postgresql,micrometer-prometheus \
  -d name=perf-lab \
  -d artifactId=perf-lab \
  -d javaVersion=21 \
  -o perf-lab.zip
unzip perf-lab.zip -d perf-lab
# Or generate from start.spring.io with those same dependencies
```

Dependencies required:
- `spring-boot-starter-web`
- `spring-boot-starter-actuator`
- `spring-boot-starter-data-jpa`
- `postgresql` (or H2 if running without Postgres — H2 is fine for K1)
- `micrometer-registry-prometheus`

**application.properties (start with this — we will inject bottlenecks below):**

```properties
# Server
server.port=8080

# Actuator — expose everything for the spike
management.endpoints.web.exposure.include=*
management.endpoint.health.show-details=always
management.metrics.export.prometheus.enabled=true

# HikariCP — NORMAL baseline (pool=10)
spring.datasource.hikari.maximum-pool-size=10
spring.datasource.hikari.minimum-idle=2
spring.datasource.hikari.connection-timeout=3000

# JVM — NORMAL baseline
# (no Xmx set here — add via JAVA_OPTS when running)

# JPA
spring.jpa.open-in-view=false
spring.jpa.show-sql=false
```

**Three endpoints to implement — keep them simple:**

```java
// 1. /api/fast — no DB, pure compute, baseline reference
// Returns immediately with a timestamp. Used to isolate DB effects.
@GetMapping("/api/fast")
public Map<String, Object> fast() {
    return Map.of("ts", System.currentTimeMillis(), "status", "ok");
}

// 2. /api/db — single DB read, the bottleneck target
// Do ONE query per request. No N+1. Clean baseline.
@GetMapping("/api/db")
public Map<String, Object> dbRead() {
    long count = itemRepository.count(); // or any single SELECT
    return Map.of("count", count, "ts", System.currentTimeMillis());
}

// 3. /api/version — static, for run identification
@GetMapping("/api/version")
public Map<String, Object> version() {
    return Map.of(
        "app", "perf-lab",
        "version", "spike-001",
        "pool_size", env.getProperty("spring.datasource.hikari.maximum-pool-size", "unknown")
    );
}
```

Add a simple `Item` entity and `ItemRepository` (JPA). Seed 100 rows on startup
using a `CommandLineRunner`. The exact schema does not matter — any table works.

**Verify the app starts:**

```bash
cd perf-lab
mvn spring-boot:run
# or: ./gradlew bootRun

curl http://localhost:8080/api/version
curl http://localhost:8080/api/fast
curl http://localhost:8080/api/db
curl http://localhost:8080/actuator/prometheus | grep hikari
```

The Prometheus endpoint must show `hikaricp_connections_max` and
`hikaricp_connections_active_connections`. If those are missing, the
Micrometer dependency is not wired. Fix before continuing.

---

### Phase 1.2 — Create the Locust Load Profile

Create: `locust/locustfile.py`

```python
"""
Perf Lab — Spike Load Profile
Targets /api/db exclusively (the bottleneck endpoint).
Do not mix /api/fast here — we want a pure signal.
"""
from locust import HttpUser, task, between, events
import json, time, os

RUN_ID = os.getenv("RUN_ID", f"run-{int(time.time())}")

class PerfLabUser(HttpUser):
    wait_time = between(0.1, 0.3)  # think time between requests

    @task
    def db_read(self):
        with self.client.get("/api/db", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status={r.status_code}")

@events.quitting.add_listener
def save_summary(environment, **kwargs):
    stats = environment.runner.stats.total
    summary = {
        "run_id": RUN_ID,
        "timestamp": time.time(),
        "num_requests": stats.num_requests,
        "num_failures": stats.num_failures,
        "avg_response_time": stats.avg_response_time,
        "p50": stats.get_response_time_percentile(0.50),
        "p95": stats.get_response_time_percentile(0.95),
        "p99": stats.get_response_time_percentile(0.99),
        "rps": stats.current_rps,
    }
    path = f"results/{RUN_ID}.json"
    os.makedirs("results", exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[SPIKE] Saved → {path}")
    print(json.dumps(summary, indent=2))
```

**Run parameters for K1 — use EXACTLY these for all three runs:**

```bash
# Run from: C:\Raghu\MyLearnings\EAG_V3\capstone\perf-agent-spike\locust\

# BASELINE — pool=10, 3 identical runs
RUN_ID=baseline-01 locust \
  --headless \
  --host http://localhost:8080 \
  --users 50 \
  --spawn-rate 10 \
  --run-time 90s \
  -f locustfile.py

RUN_ID=baseline-02 locust \
  --headless \
  --host http://localhost:8080 \
  --users 50 \
  --spawn-rate 10 \
  --run-time 90s \
  -f locustfile.py

RUN_ID=baseline-03 locust \
  --headless \
  --host http://localhost:8080 \
  --users 50 \
  --spawn-rate 10 \
  --run-time 90s \
  -f locustfile.py
```

Wait 30 seconds between runs. The app keeps running. Do not restart it between runs.

---

### Phase 1.3 — Collect HikariCP Metrics Per Run

After each Locust run, immediately collect Actuator metrics. Save to
`results/{RUN_ID}_hikari.json`:

```bash
curl -s http://localhost:8080/actuator/metrics/hikaricp.connections.pending \
  >> results/baseline-01_hikari.json
curl -s http://localhost:8080/actuator/metrics/hikaricp.connections.acquire \
  >> results/baseline-01_hikari.json
curl -s http://localhost:8080/actuator/metrics/hikaricp.connections.active \
  >> results/baseline-01_hikari.json
curl -s http://localhost:8080/actuator/metrics/http.server.requests \
  >> results/baseline-01_hikari.json
```

Also capture CPU steal if on a VM (not needed on MacBook — skip):
```bash
vmstat 1 5  # watch %st column
```

---

### Phase 1.4 — Plant the Bottleneck

Stop the app. Change ONE property in `application.properties`:

```properties
# BOTTLENECK — pool=2 (was 10)
spring.datasource.hikari.maximum-pool-size=2
spring.datasource.hikari.minimum-idle=1
```

Restart the app. Confirm via `/api/version` that the change is live.
Then run three BOTTLENECK campaigns:

```bash
RUN_ID=bottleneck-01 locust --headless --host http://localhost:8080 \
  --users 50 --spawn-rate 10 --run-time 90s -f locustfile.py

RUN_ID=bottleneck-02 locust --headless --host http://localhost:8080 \
  --users 50 --spawn-rate 10 --run-time 90s -f locustfile.py

RUN_ID=bottleneck-03 locust --headless --host http://localhost:8080 \
  --users 50 --spawn-rate 10 --run-time 90s -f locustfile.py
```

Collect HikariCP metrics after each run as above.

---

### Phase 1.5 — K1 Decision

Create: `docs/K1_RESULT.md`

Write a table:

```
| run_id        | p50 ms | p95 ms | p99 ms | hikari.pending | hikari.acquire_mean |
|---------------|--------|--------|--------|----------------|---------------------|
| baseline-01   |        |        |        |                |                     |
| baseline-02   |        |        |        |                |                     |
| baseline-03   |        |        |        |                |                     |
| bottleneck-01 |        |        |        |                |                     |
| bottleneck-02 |        |        |        |                |                     |
| bottleneck-03 |        |        |        |                |                     |
```

**K1 PASS criteria — all must hold:**

```
1. Baseline p99 spread across 3 runs ≤ 20%
   ( max(baseline p99) - min(baseline p99) ) / min(baseline p99) ≤ 0.20

2. Bottleneck p99 is at least 3× baseline p99 median
   bottleneck_p99_median ≥ 3 × baseline_p99_median

3. Baseline p99 spread across 3 runs ≤ 20% (same check for bottleneck)
   bottleneck runs must also be internally stable

4. hikari.connections.pending > 0 in all three bottleneck runs
   (confirms the symptom is connection wait, not something else)
```

**K1 FAIL actions:**

- If baseline spread > 20%: thermal throttle likely. Reduce users to 20,
  shorten run to 60s, try again. If still fails → MacBook is not stable
  enough; must use cloud target.
- If bottleneck p99 < 2× baseline: pool of 2 is not saturating at 50
  users. Reduce pool to 1, retry.
- If hikari.pending stays 0: HikariCP metrics not wired. Fix Micrometer
  dependency before K2.

**Write the verdict explicitly:**

```
K1: PASS / FAIL
Reason: [one sentence]
Blocker (if FAIL): [what to fix]
```

---

## Day 2 — K3: Can glc_v5 Diagnose From Raw Metrics?

**Do not start Day 2 until K1 is PASS.**

### Phase 2.1 — Build the Metrics Snapshot

Write `k3_probe/build_snapshot.py`:

```python
"""
Collect a metrics snapshot from the running perf-lab and format it
as the JSON payload the agent will later receive.

Run against the BOTTLENECK app (pool=2, after a 90s Locust run).
"""
import json, requests, time

BASE = "http://localhost:8080"
ACTUATOR = f"{BASE}/actuator"

def metric(name):
    try:
        r = requests.get(f"{ACTUATOR}/metrics/{name}", timeout=5)
        r.raise_for_status()
        data = r.json()
        measurements = {m["statistic"]: m["value"] for m in data.get("measurements", [])}
        return measurements
    except Exception as e:
        return {"error": str(e)}

snapshot = {
    "captured_at": time.time(),
    "run_id": "bottleneck-03",    # use results from your last bottleneck run
    "locust_summary": {           # paste values from results/bottleneck-03.json
        "p50_ms": None,           # FILL IN
        "p95_ms": None,           # FILL IN
        "p99_ms": None,           # FILL IN
        "rps": None,              # FILL IN
        "error_rate": None        # FILL IN
    },
    "hikaricp": {
        "connections_max":     metric("hikaricp.connections.max"),
        "connections_active":  metric("hikaricp.connections.active"),
        "connections_pending": metric("hikaricp.connections.pending"),
        "connections_idle":    metric("hikaricp.connections.idle"),
        "acquire_ms":          metric("hikaricp.connections.acquire"),
        "creation_ms":         metric("hikaricp.connections.creation"),
        "timeout_total":       metric("hikaricp.connections.timeout"),
    },
    "jvm": {
        "gc_pause_ms":         metric("jvm.gc.pause"),
        "memory_heap_used":    metric("jvm.memory.used"),
        "memory_heap_max":     metric("jvm.memory.max"),
        "threads_live":        metric("jvm.threads.live"),
        "threads_peak":        metric("jvm.threads.peak"),
    },
    "http": {
        "server_requests":     metric("http.server.requests"),
    },
    "baseline_reference": {      # paste p99 median from baseline runs
        "p99_ms_median": None    # FILL IN
    }
}

path = "k3_probe/snapshot.json"
with open(path, "w") as f:
    json.dump(snapshot, f, indent=2)

print(f"Snapshot saved to {path}")
print(json.dumps(snapshot, indent=2))
```

Run it immediately after a bottleneck Locust campaign:
```bash
python k3_probe/build_snapshot.py
```

Fill in the FILL IN fields from your Day 1 results.

---

### Phase 2.2 — Write the Diagnosis Prompt

Create `k3_probe/diagnosis_prompt.txt`:

```
You are a performance engineering assistant. You have been given a metrics
snapshot from a Spring Boot application that is missing its latency SLA.

The SLA is: p99 response time ≤ 200ms.

Your job is to:
1. Identify the most likely root cause of the SLA breach.
2. Name the specific metric evidence that supports your conclusion.
3. Name at least one alternative hypothesis you considered and why you
   ruled it out.
4. Propose one specific, bounded configuration change to test.
5. State what you predict the p99 will be after the change.
6. State your confidence (low / medium / high) and why.

Respond ONLY in this JSON structure — no prose outside it:

{
  "primary_cause": "<one sentence>",
  "evidence": ["<metric name>: <value and significance>", ...],
  "ruled_out": [{"hypothesis": "...", "reason": "..."}],
  "proposed_change": {
    "parameter": "<exact property name>",
    "current_value": "<value>",
    "proposed_value": "<value>",
    "rationale": "<one sentence>"
  },
  "predicted_p99_ms": <number>,
  "confidence": "low|medium|high",
  "confidence_reason": "<one sentence>"
}

Here is the metrics snapshot:

{{SNAPSHOT}}
```

---

### Phase 2.3 — Run the K3 Probe Against glc_v5

Write `k3_probe/run_probe.py`:

```python
"""
Send the metrics snapshot to glc_v5 and record the response.
No orchestration. No agent loop. Just a single prompt → response.
This is the K3 kill test.
"""
import json, httpx, time, os

GLC_BASE = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")
MODEL_PROVIDER = "gemini"   # pin — do not allow failover
MODEL = "gemini-2.0-flash"  # or whatever your glc_v5 exposes

with open("k3_probe/snapshot.json") as f:
    snapshot = json.load(f)

with open("k3_probe/diagnosis_prompt.txt") as f:
    prompt_template = f.read()

prompt = prompt_template.replace("{{SNAPSHOT}}", json.dumps(snapshot, indent=2))

payload = {
    "messages": [{"role": "user", "content": prompt}],
    "system": "You are a performance engineering assistant. Return only valid JSON.",
    "max_tokens": 1000,
    "temperature": 0,
    "reasoning": "off",
    "provider": MODEL_PROVIDER,
    "agent": "k3_probe",
    "session": f"spike-k3-{int(time.time())}",
}

print(f"[K3] Sending to {GLC_BASE}/v1/chat/completions ...")
r = httpx.post(
    f"{GLC_BASE}/v1/chat/completions",
    json=payload,
    timeout=60
)
r.raise_for_status()
response = r.json()

# Extract the text
content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
usage = response.get("usage", {})

# Parse the JSON from the model
try:
    # Strip markdown fences if present
    clean = content.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    diagnosis = json.loads(clean)
    parse_ok = True
except json.JSONDecodeError as e:
    diagnosis = {"raw": content, "parse_error": str(e)}
    parse_ok = False

result = {
    "timestamp": time.time(),
    "model_provider": MODEL_PROVIDER,
    "usage": usage,
    "parse_ok": parse_ok,
    "diagnosis": diagnosis,
}

path = f"k3_probe/result_{int(time.time())}.json"
with open(path, "w") as f:
    json.dump(result, f, indent=2)

print(f"\n[K3] Result saved to {path}")
print(f"[K3] Parse OK: {parse_ok}")
if parse_ok:
    print(f"[K3] Primary cause: {diagnosis.get('primary_cause')}")
    print(f"[K3] Proposed change: {diagnosis.get('proposed_change')}")
    print(f"[K3] Confidence: {diagnosis.get('confidence')}")
else:
    print(f"[K3] RAW RESPONSE:\n{content}")
```

Run it:
```bash
python k3_probe/run_probe.py
```

---

### Phase 2.4 — Verify the Proposed Change

If K3 returns a proposed change, verify it manually:

1. Apply the proposed `parameter` / `proposed_value` to `application.properties`
2. Restart the app
3. Run one 90s Locust campaign: `RUN_ID=k3-verify-01`
4. Compare p99 to the agent's `predicted_p99_ms`

This is not grading the agent — it is verifying that the *direction* is correct.
If the agent said "increase pool" and p99 improved, K3 passes even if the
exact numbers differ.

---

### Phase 2.5 — K3 Decision

Create: `docs/K3_RESULT.md`

```
## K3 Result

Snapshot used: k3_probe/snapshot.json
glc_v5 response: k3_probe/result_<ts>.json

### Model output
Primary cause identified: [paste]
Evidence cited: [paste]
Ruled out: [paste]
Proposed change: [paste]
Predicted p99: [paste]
Confidence: [paste]

### Ground truth
Actual planted cause: connection pool exhaustion (maximum-pool-size=2)
Expected diagnosis: connection pool / HikariCP pending / acquire time
Expected proposed change: increase maximum-pool-size

### Verification run
RUN_ID: k3-verify-01
p99 before: [from bottleneck-03]
p99 after:  [from k3-verify-01]
Improved: YES / NO

### K3 Verdict
```

**K3 PASS criteria:**

```
1. Response parses as valid JSON (no parse_ok: false)
2. primary_cause mentions connection pool, HikariCP, or connection wait
   (not GC, not memory, not thread pool)
3. proposed_change.parameter contains "pool-size" or "maximum-pool"
4. Verification run p99 < bottleneck p99 (any improvement counts)
```

**K3 FAIL actions:**

- If JSON does not parse: prompt format is wrong. Simplify. Remove
  the ruled_out field and retry.
- If it diagnoses GC or memory: the snapshot is misleading it. Check
  that jvm.gc.pause values are near-zero and hikaricp.connections.pending
  is clearly nonzero before retrying.
- If proposed change does not improve p99: try a smaller pool (pool=1)
  to make the signal stronger, then rerun.

**Write the verdict explicitly:**

```
K3: PASS / FAIL
Reason: [one sentence]
Blocker (if FAIL): [what to fix]
```

---

## End-of-Spike Gate

Create: `docs/SPIKE_VERDICT.md`

```
## Feasibility Spike Result

Date: [date]
K1 — Measurement stability: PASS / FAIL
K3 — Diagnosis quality:     PASS / FAIL

## Decision

BOTH PASS → Proceed with Route C. Submit form by Fri Sep 4 1:00 PM.
K1 FAIL   → Fix measurement environment or fall back to Route A.
K3 FAIL   → Fix diagnosis prompt (one more attempt allowed); if still
             failing, escalate to Rohan or fall back to Route A.

## Evidence files
- docs/K1_RESULT.md
- docs/K3_RESULT.md
- results/ (all Locust JSONs)
- k3_probe/result_*.json

## What the prototype can already do (if both pass)
- Plant a known bottleneck deterministically ✓
- Run a reproducible load profile ✓
- Collect Micrometer metrics per run ✓
- Send metrics to glc_v5 and receive a structured diagnosis ✓
- Apply a proposed change and re-test ✓

That is the core loop. Everything in the four-week plan builds on this.
```

---

## Spike Directory Structure (final)

```
perf-agent-spike/
├── docs/
│   ├── PREFLIGHT.md
│   ├── K1_RESULT.md
│   ├── K3_RESULT.md
│   └── SPIKE_VERDICT.md
├── perf-lab/                  ← Spring Boot target app
│   └── src/...
├── locust/
│   ├── locustfile.py
│   └── results/
│       ├── baseline-01.json
│       ├── baseline-02.json
│       ├── baseline-03.json
│       ├── bottleneck-01.json
│       ├── bottleneck-02.json
│       ├── bottleneck-03.json
│       └── k3-verify-01.json
└── k3_probe/
    ├── build_snapshot.py
    ├── diagnosis_prompt.txt
    ├── run_probe.py
    ├── snapshot.json
    └── result_<timestamp>.json
```

---

## Notes for Claude Code

- Work top to bottom. Do not skip phases.
- Each phase has an explicit output. Do not move to the next phase until
  the output exists and contains real data.
- The K1 and K3 decisions are yours to write into the result files.
  Claude Code generates the files; you fill in the verdict.
- If glc_v5 is not running, start it before Phase 2.1. Do not attempt
  to start it from inside this spike — use its own startup procedure.
- Gemini provider only. No auto_route. No provider failover.
  Every call in this spike must go through the same model.
- Keep all results. Do not delete failed run outputs. They are evidence.
