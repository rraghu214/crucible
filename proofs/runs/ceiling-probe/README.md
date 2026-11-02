# Ceiling probe

No ceiling probe has been run on the current code yet; this folder is where its evidence goes.

A ceiling probe answers a different question from a campaign: not "what is wrong?" but "how
much load can this service take?". It raises the load in steps, holds each step long enough
to measure, and stops at the first step that misses the goal. Its result is a pair, the last
level that met the goal and the first that did not, and it changes no setting.

To run one: the wizard's **Ceiling Probe** mode, or

```bash
uv run crucible ceiling --users 50 --min-users 50 --max-users 300 --step 50
```

Then copy `results/<run-id>.ceiling.json` and the job log here, the same way as the other
run folders.
