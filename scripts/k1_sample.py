#!/usr/bin/env python3
"""Sample HikariCP gauges DURING a load run, and record the peak.

Used for the K1 cloud re-run (see docs/K1_CLOUD_RESULT.md). Runs on the target
box for the duration of a Locust run driven from the load-generator box.

WHY THIS EXISTS, rather than reading the metrics once when the run finishes:
a gauge reports the value at the instant it is read, and the pool drains the
moment load stops. Read afterwards, `hikaricp.connections.pending` reads 0. In
the K3 spike the model saw that 0, concluded nothing was waiting for a
connection, and ruled out pool starvation -- during the run the value had been
43. This script is the smallest possible version of the mid-run sampler that
`crucible/perf/runner.py` implements as `GaugeSampler`.

It also brackets the acquire timer so the mean is a DELTA across the measured
window rather than a lifetime average, and converts to milliseconds: Micrometer
reports timers in SECONDS, and reading 2.4 as milliseconds is precisely what
made a saturated pool look healthy in K3.

Usage:
    python3 scripts/k1_sample.py <run-label> [seconds]

Writes ~/k1/<run-label>.csv (per-second samples) and prints the summary.
"""
import json
import pathlib
import sys
import time
import urllib.request

BASE = "http://localhost:8080/actuator/metrics"


def metric(name):
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=5) as r:
        d = json.load(r)
    return {m["statistic"]: m["value"] for m in d.get("measurements", [])}


label = sys.argv[1]
secs = int(sys.argv[2]) if len(sys.argv) > 2 else 100

out = pathlib.Path.home() / "k1"
out.mkdir(exist_ok=True)
acq0 = metric("hikaricp.connections.acquire")

rows, t0 = [], time.time()
while time.time() - t0 < secs:
    try:
        rows.append((round(time.time() - t0, 1),
                     metric("hikaricp.connections.pending").get("VALUE"),
                     metric("hikaricp.connections.active").get("VALUE"),
                     metric("hikaricp.connections.idle").get("VALUE")))
    except Exception:
        # A failed read is recorded as a gap, never as a zero. A zero would be
        # indistinguishable from "we looked and the pool was idle".
        rows.append((round(time.time() - t0, 1), None, None, None))
    time.sleep(1)

acq1 = metric("hikaricp.connections.acquire")
dc = acq1.get("COUNT", 0) - acq0.get("COUNT", 0)
dt = acq1.get("TOTAL_TIME", 0.0) - acq0.get("TOTAL_TIME", 0.0)
mean_ms = (dt * 1000.0 / dc) if dc else None

csv = out / f"{label}.csv"
csv.write_text("t_s,pending,active,idle\n" + "\n".join(
    ",".join("" if v is None else str(v) for v in r) for r in rows) + "\n")

pend = [r[1] for r in rows if r[1] is not None]
act = [r[2] for r in rows if r[2] is not None]
print(f"=== {label} ===")
print(f"samples                  : {len(rows)}")
print(f"pending PEAK during load : {max(pend) if pend else 'n/a'}")
print(f"active  PEAK during load : {max(act) if act else 'n/a'}")
print(f"acquire count (window)   : {dc}")
print("acquire MEAN ms (window) : " + (f"{mean_ms:.2f}" if mean_ms is not None else "n/a"))
print(f"acquire MAX ms (recent)  : {acq1.get('MAX', 0) * 1000:.2f}")
print(f"raw -> {csv}")
