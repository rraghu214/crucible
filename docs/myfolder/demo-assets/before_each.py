"""
before_each.py — sample hook for demo purposes.

Crucible runs this with: python -I before_each.py
Environment available: CRUCIBLE_HOOK_STAGE, RUN_ID, EXPERIMENT
Nothing else: control token and Crucible's own variables are never passed.
A non-zero exit code blocks the experiment.
"""
import os
import sys

stage = os.environ.get("CRUCIBLE_HOOK_STAGE", "unknown")
run_id = os.environ.get("RUN_ID", "unknown")
experiment = os.environ.get("EXPERIMENT", "unknown")

print(f"[before_each] stage={stage} run={run_id} experiment={experiment}")

# In a real hook you would reset test data here, e.g.:
#   import subprocess
#   subprocess.run(["psql", "-h", "10.0.0.79", "-U", "perf", "-c",
#                   "TRUNCATE orders RESTART IDENTITY CASCADE;"], check=True)

print("[before_each] done")
sys.exit(0)
