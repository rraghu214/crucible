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
