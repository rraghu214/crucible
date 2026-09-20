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

- **The deployer's own report is not captured on the manifest.** A post-receive
  hook or a CI job knows which sha it checked out and built, and logs it
  (`scripts/box_a_post_receive.sh` writes `built perf-lab with commit=<sha>`).
  Crucible does not read it back, so when a deploy comes back unverified the
  manifest cannot distinguish "the build failed" from "the build succeeded but
  the restart did not take" from "it is running but will not say what it is" --
  three conditions an operator would respond to differently. Fix (own PR): carry
  `deployer_reported_commit` and the deployer's exit status on `DeployResult`.

  NOT a verification gap. The deployer's word never satisfies DESIGN.md 19.6 --
  it is evidence about the build, produced by the thing doing the building, and
  it cannot prove the process currently serving requests is that artifact. This
  is about diagnosing a failed deploy faster, not about trusting one.

- **A target on the bottom rung of 19.6's ladder yields unverified experiments.**
  Where nothing can observe a change -- no metric carrying the property, no
  config endpoint, no commit, no usable uptime -- the experiment is recorded as
  unverified and the report says so. That is the designed behaviour rather than a
  defect, but it is a real limitation worth stating: such a result rests on the
  deploy pipeline having done what it said. Accepted deliberately on
  21 September 2026, because refusing to run would exclude a large class of real
  applications; what Crucible refuses is to present the result as verified.

- **The model gateway is public and unauthenticated.** glc_v5 is hosted at
  https://glc-v5-rraghu214.onrender.com and `/v1/chat` accepts requests without a
  token; Crucible sends none (`crucible/gateway.py` attaches a bearer token only
  to channel calls). Anyone who learns the URL can spend the Gemini free-tier
  quota the whole campaign budget is calculated against (`config/quota.yaml`),
  and the OpenAPI spec is public, which lists `/v1/control/kill` among the
  routes.

  **Accepted deliberately by the operator on 21 September 2026**, on the grounds
  that the blast radius is free-tier quota rather than money and that adding auth
  is scope the capstone does not have room for. Recorded rather than argued: the
  decision is reasonable, and a reader six months from now should be able to see
  that it was a decision and not an oversight.

  Cheapest fix when it is wanted: a shared bearer token in Render's env, checked
  in glc_v5's request path, and one header added to `GatewayClient._payload`'s
  caller. Perhaps an hour, most of it in glc_v5 rather than here.
