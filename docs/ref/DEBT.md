# Known debt

Pre-existing issues in the inherited S17Code base, not owned by current capstone
work. Fix only when touching the file for another reason.

- `OfficialA2AServicer.SubscribeToTask`: terminal frame missed when the task flips
  between yield and check. Fix: emit the terminal frame before break. See
  `test_official_subscription_resumes_waiting_graph_and_maps_cancel`.

- **Async timing races in the test suite.** `test_live_graph.py` (fast/slow sibling
  expansion, planner-cancels-sibling) and the a2a hardening test above use
  `asyncio.sleep`-based ordering assumptions. Under load a shifting 1-3 of them
  fail; each passes in isolation. Same on `main`. CI runs `pytest --reruns 2`,
  which **masks** this — it does not fix it. The real fix is to make the tests
  wait on a condition instead of a sleep. Do not read a green CI as "solved".

- **`proofs/p4_trace_export.py` cannot pass against any real metered run.** Its
  hierarchy check requires every `provider_call` span to have a `node` parent
  (`EXPECTED_PARENT = {"provider_call": "node"}`), but `crucible/telemetry/spans.py`
  deliberately nests the planner's own metered call under the `plan` span, and
  `planner.py` is a first-class metered caller by design. So p4 flags the planner
  calls as reparented, offline or live, on this branch or `main`. It also needs a
  planner-aware offline transport before a `node` level exists at all (the shared
  `OfflineTransport` returns a fixed blob that is not a graph patch). CI runs the
  p4 step with `continue-on-error: true` so the failure stays visible without
  blocking. Real fix (own PR): allow `provider_call` under `node` OR `plan` in
  both p4 and `test_p4_backend_verification.py`, and give p4 its own planner-aware
  offline transport passed into `run_task`.
