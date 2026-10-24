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

import csv
import itertools
import json
import os
import re
import time
import urllib.parse

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

    @tag("slow")
    @task
    def slow(self):
        """application_code. No DB, no pool -- pure handler latency."""
        self._get("/api/slow")


def _apply_traffic_mix():
    """CRUCIBLE_MIX="db=60,orders=40": run exactly these tagged tasks at these weights.

    Set by Crucible for a traffic-mix scenario (the SLA's ``scenarios[].mix``).
    Unset, every task keeps its weight of 1 and ``--tags`` picks one endpoint,
    as before. A tag in the mix that no task carries is an error, never a
    silently smaller mix.
    """
    raw = os.environ.get("CRUCIBLE_MIX", "").strip()
    if not raw:
        return
    weights = {}
    for part in raw.split(","):
        name, _, weight = part.partition("=")
        weights[name.strip()] = int(weight)
    tasks = {}
    for attribute in dir(PerfLabUser):
        function = getattr(PerfLabUser, attribute)
        for name in getattr(function, "locust_tag_set", set()):
            if name in weights:
                tasks[function] = weights[name]
    found = {name for function in tasks for name in function.locust_tag_set}
    missing = set(weights) - found
    if missing:
        raise RuntimeError(f"CRUCIBLE_MIX names tags no task carries: {sorted(missing)}")
    PerfLabUser.tasks = tasks


_apply_traffic_mix()


_TEMPLATE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_rows(path):
    with open(path, encoding="utf-8", newline="") as handle:
        if path.lower().endswith(".csv"):
            return list(csv.DictReader(handle))
        return json.load(handle)


def _apply_templated_requests():
    """CRUCIBLE_REQUESTS=<spec.json>: run the scenario's templated requests instead.

    Each request is a path on the target, a method, a query and/or body with
    ${var} placeholders, and a weight. Each ${var} takes the next row of its
    data column, cycling (query values URL-encoded, body values as written).
    Reported under the endpoint path, so per-endpoint statistics still work;
    any non-2xx response is a failure.
    """
    spec_path = os.environ.get("CRUCIBLE_REQUESTS", "").strip()
    if not spec_path:
        return
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    columns = {}
    for var, mapping in (spec.get("data") or {}).items():
        columns[var] = [str(row.get(mapping["column"], "")) for row in _load_rows(mapping["file"])]
    counters = {var: itertools.count() for var in columns}

    def next_value(var):
        values = columns[var]
        return values[next(counters[var]) % len(values)]

    def make_task(request):
        def templated(user):
            used = set(_TEMPLATE_VAR.findall(request["query"] + request["body"]))
            values = {var: next_value(var) for var in used}
            query = _TEMPLATE_VAR.sub(lambda m: urllib.parse.quote(values[m.group(1)], safe=""), request["query"])
            body = _TEMPLATE_VAR.sub(lambda m: values[m.group(1)], request["body"])
            path = request["endpoint"] + ("?" + query if query else "")
            headers = {"Content-Type": "application/json"} if body.lstrip()[:1] in ("{", "[") else None
            with user.client.request(request["method"], path, data=body or None, headers=headers,
                                     name=request["endpoint"], catch_response=True) as response:
                if not 200 <= response.status_code < 300:
                    response.failure(f"status={response.status_code}")

        templated.__name__ = "templated_" + re.sub(r"\W+", "_", request["endpoint"])
        return templated

    PerfLabUser.tasks = {make_task(request): int(request["weight"]) for request in spec["requests"]}


_apply_templated_requests()


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
