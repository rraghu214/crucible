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
    print(f"\n[SPIKE] Saved -> {path}")
    print(json.dumps(summary, indent=2))
