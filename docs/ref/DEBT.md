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

- **The Tomcat thread meters are mapped but never sampled, so
  `thread_pool_saturation` cannot be confirmed from a snapshot.**
  `config/profiles/spring-boot.yaml` lists `tomcat.threads.busy` and
  `tomcat.threads.config.max` in `metric_map` -- which is what lets the agent say
  the family exists -- but in neither `gauges` nor `snapshot_metrics`, which are
  the blocks the collector actually reads. A snapshot therefore carries no thread
  fields at all: not null ones, absent ones. The agent cannot declare an evidence
  gap about a field that was never named to it.

  **Accepted deliberately by the operator on 26 September 2026.** The cost of
  closing it is small (two lines in `gauges:`, plus the PromQL and Datadog series
  names) but it lands mid-week-3 and would invalidate nothing already captured, so
  it is better done before a capture run than during one.

  Two consequences are live now rather than later:

  - `config/fixtures/perflab_thread_starved.yaml` is declared but **excluded from
    capture** until the meters exist. Capturing it first would bake the absence
    into every replay case built on it, and the agent would then be scored on a
    diagnosis the evidence could never support.
  - `spring-boot.SKILL.md` states the gap explicitly, so the model is told not to
    hunt for `tomcat.threads.busy` and not to infer the family from the *absence*
    of pool and GC signals -- an elimination performed on evidence nobody
    gathered is exactly the K3 attempt-1 failure (DESIGN.md 4.3).

  Closing it means: add `threads_busy` / `threads_config_max` to `gauges:`, add
  their `promql:` and `datadog:` series names, bump `COLLECTOR_VERSION` (the
  snapshot shape changes, and every existing fixture must then be recaptured --
  DESIGN.md 7), then capture the fixture.

- **The Datadog adapter cannot tell "not authorised" from "no such metric".**
  `DatadogMetricsProvider.query` catches `httpx.HTTPError` and returns an empty
  series, which the collector renders as `null` -- so a 403 from a wrong API key
  or the wrong Datadog site, a 429 rate limit, a transient 502, and a metric that
  genuinely has no data in the window all reach the agent as the same "never
  measured". Two of those four are fixable by a human in under a minute; the
  other two are not, and the agent has no way to say which it hit.

  This sits directly against principle 2 (the agent knows what it cannot see) and
  against DESIGN.md 4.8's rule that an unreadable metric is *declared*. The
  adapter already has the right machinery -- `unreadable` carries a reason per
  metric for the unit case -- so the fix is to record the status code there
  instead of discarding it, not to raise.

  Sharpened by the free tier: 1 host and **1-day retention**, so a query whose
  window falls outside retention returns empty and is indistinguishable from a
  metric that does not exist. `provider.unreadable` is where that distinction
  belongs.

  Also noted while reading it: credentials go in query PARAMETERS
  (`api_key`, `application_key`) rather than `DD-API-KEY` headers. Nothing
  currently logs the URL, so nothing leaks today -- but DESIGN.md 8 and 19.8 ask
  that credentials never be able to become a printable string, and a URL is one
  formatted exception away from being printed. The v1 query API accepts both
  forms; the header form costs nothing and removes the class of failure.

  Both open as of 26 September 2026, both in `tests/test_perf_datadog.py`'s
  group (GROUP 19), which is the one group still marked REVIEW NEEDED.

- **`perflab_code_latency` describes an endpoint the target does not have.** The
  fixture declares `/api/slow` with a 400 ms blocking call; Box A returns **404**
  for it (verified 26 September 2026). It was written from the assertion doc's
  description of the cause rather than from perf-lab's actual routes -- the same
  class of error as a skill naming collector fields that had been renamed, and
  caught the same way, by running the thing rather than reading it.

  Excluded from capture (`providers: []`) until perf-lab grows the endpoint. This
  is a TARGET gap rather than a Crucible one, and it is small: a handler with a
  `Thread.sleep(400)` and a `@tag("slow")` task in `locust/locustfile.py`.
  `/api/downstream` is the nearest existing endpoint and is deliberately NOT a
  substitute -- it is genuinely downstream latency through httpbin, so an agent
  diagnosing `application_code` from it would be marked correct for a wrong
  reason, which is precisely the LUCKY quadrant the benchmark exists to expose.

  Consequence for the benchmark: **T5 (class D, outside authority) has no fixture
  to run against**, so "can it say this is not mine to fix?" is currently
  untested. T4's "nothing is wrong" still has `perflab_healthy`.

- **Neither PromQL nor Datadog can be captured on Box A today.** Prometheus is not
  running on `10.0.0.79:9090` (connection refused, 26 September 2026) and no
  Datadog credentials exist on Box B. Both adapters are implemented and unit
  tested; neither has been exercised against a live backend.

  This is the gap that matters most for the product's central claim. "Whatever
  your stack" rests on provider independence, and the honest evidence for it is
  the same target state diagnosed identically through three backends -- which is
  exactly what `perflab_pool_starved` and `perflab_gc_pressure` are tagged for and
  cannot yet deliver. Until then the benchmark demonstrates ONE provider, and the
  claim must say so.

  Prometheus is the cheap half: it is already in the §16 target stack and needs a
  container on Box A plus the scrape config. Datadog needs an account and an
  agent, and its free tier is 1 host with 1-day retention.
