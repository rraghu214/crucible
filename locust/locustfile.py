"""PerfLab load profile: one tagged task per cause family.

Run one family at a time, selected by tag, so a scenario measures one signal
rather than a blend of eight::

    locust -f locust/locustfile.py --headless --host http://localhost:8080 \\
           -u 50 -r 10 --run-time 300s --tags db --csv results/run-1

The tags match the cause families declared in
``config/profiles/spring-boot.yaml``. Adding a task here without adding its
family there produces a fixture the agent cannot name correctly.

**This file is not writable by the agent.** It is a protected path in the profile
and it is Policy memory, enforced twice (``DESIGN.md`` section 4.4). Fewer users
is not a fix, and changing the profile mid-campaign destroys comparability
between experiments as surely as moving the SLA would.

Note on encoding: this file uses ``->`` and never the arrow character. Windows
cp1252 cannot encode it, and that has broken things here before.
"""

import json
import os
import time

from locust import HttpUser, between, events, tag, task

RUN_ID = os.getenv("RUN_ID", f"run-{int(time.time())}")

#: Rows requested from /api/payload. Large enough that serialisation dominates,
#: small enough that the response still fits comfortably in memory.
PAYLOAD_ROWS = int(os.getenv("PERFLAB_PAYLOAD_ROWS", "5000"))

#: Orders requested from /api/orders. Each one costs an extra query while
#: default_batch_fetch_size is 1, so this number sets the size of the N+1.
ORDER_PAGE_SIZE = int(os.getenv("PERFLAB_ORDER_SIZE", "50"))


class PerfLabUser(HttpUser):
    """Every cause family, one tag each. Select exactly one per scenario."""

    wait_time = between(0.1, 0.3)  # think time between requests

    def _get(self, path, name=None):
        with self.client.get(path, name=name or path, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"status={r.status_code}")

    @tag("fast")
    @task
    def fast(self):
        """Baseline. No dependency, so a regression here is the app itself."""
        self._get("/api/fast")

    @tag("db")
    @task
    def db_read(self):
        """connection_pool_exhaustion."""
        self._get("/api/db")

    @tag("async")
    @task
    def async_work(self):
        """thread_pool_saturation."""
        self._get("/api/async")

    @tag("churn")
    @task
    def churn(self):
        """gc_pressure."""
        self._get("/api/churn")

    @tag("orders")
    @task
    def orders(self):
        """inefficient_query (N+1)."""
        self._get(f"/api/orders?size={ORDER_PAGE_SIZE}", name="/api/orders")

    @tag("catalog")
    @task
    def catalog(self):
        """cache_miss. Pages 0-4 so a working set exists to hit or miss."""
        page = int(time.time() * 10) % 5
        self._get(f"/api/catalog?page={page}", name="/api/catalog")

    @tag("downstream")
    @task
    def downstream(self):
        """downstream_latency."""
        self._get("/api/downstream")

    @tag("ledger")
    @task
    def ledger(self):
        """lock_contention. Diagnosable; deliberately not fixable by configuration."""
        self._get("/api/ledger")

    @tag("payload")
    @task
    def payload(self):
        """payload_serialization."""
        self._get(f"/api/payload?rows={PAYLOAD_ROWS}", name="/api/payload")


@events.quitting.add_listener
def save_summary(environment, **kwargs):
    """Write a JSON summary alongside locust's own CSV.

    The runner reads the CSV, not this file -- the CSV is what locust guarantees.
    This exists for a human reading a single run by hand, and for the spike
    results already committed under results/.
    """
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[perflab] Saved -> {path}")
    print(json.dumps(summary, indent=2))
