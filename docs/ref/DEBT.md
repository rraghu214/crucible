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

- ~~**The deployer's own report is not captured on the manifest.**~~ **FIXED 4 Oct 2026.**
  `DeployResult` now carries `hook_commit: str | None` and `hook_exit_code: int | None`.
  `GitPushDeployer.deploy()` parses the hook's `built <app> with commit=<sha>` line from
  the git push output and records both fields. The corroboration-vs-verification distinction
  still holds: `hook_commit` is evidence about the build; `observed_commit` is proof the
  process is running it (DESIGN.md 19.6).

- ~~**Property read-back rung (§19.6 ladder rung 2) was never called.**~~
  **FIXED 4 Oct 2026.** `verify_properties_via_env()` reads each changed property
  back from `/actuator/env/<prop>` after the commit is confirmed. `DeployResult`
  carries `property_verified` and `property_reason`; the manifest records the
  check. A read error falls through to rung 3 (sha) rather than blocking: the sha
  already passed, so the proof is weaker but the campaign continues.

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
  token; anyone who learns the URL can spend the Gemini free-tier quota.

  **Crucible side fixed 4 Oct 2026.** `GatewayClient` now reads `CRUCIBLE_GATEWAY_TOKEN`
  and sends `Authorization: Bearer <token>` on `/v1/chat` and `/healthz`. Set this env
  var on Box B once the gateway side is wired. The gateway side (glc_v5 checking the
  token) is out of scope for this capstone — that change belongs in the glc_v5 repo,
  and today the header is accepted and ignored.

- ~~**The Tomcat thread meters are mapped but never sampled.**~~ **FIXED 3 Oct 2026 (B5).**
  `tomcat.threads.busy` and `tomcat.threads.config.max` are now in both `gauges:` and
  `snapshot_metrics:` in `config/profiles/spring-boot.yaml`, with PromQL and Datadog
  series names. `COLLECTOR_VERSION` bumped to `1.2.0`; previous snapshots are stale.
  `perflab_thread_starved` is unblocked and active in the fixture set.

- ~~**The Datadog adapter cannot tell "not authorised" from "no such metric".**~~
  **FIXED 3 Oct 2026 (B+ integrity).** `DatadogMetricsProvider` now records 4xx/5xx
  status codes in `provider.unreadable` with an explicit reason rather than returning
  empty series. Credentials moved from query parameters to `DD-API-KEY` /
  `DD-APPLICATION-KEY` headers. Group 19 tests in `tests/test_perf_datadog.py` updated.

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

- ~~**Watchdog never runs in a live campaign.** `LocustRunner` accepted a watchdog parameter but `build_measure` never created one, so `manifest.watchdog` was always `None` and CPU steal was never observed.~~
  **FIXED 4 Oct 2026.** `build_measure` now takes an optional `state_dir`; when present it calls `watchdog.for_scenario()` and sets `runner.watchdog` before each measured window. `build_campaign` passes its own `state_dir`, so live campaigns now supervise all seven tripwires and record observed CPU steal on every manifest. Fixture capture (`build_multi_measure`) is deliberately left without a watchdog — captures are single-shot reads, not supervised runs.

- ~~**Policy-memory SLA enforcement was not wired in `build_campaign`.**~~ **FIXED 4 Oct 2026.**
  `build_campaign` now constructs a `MemoryStore(state_dir / "memory.db")` and passes it to `Campaign.memory_store`. The existing `publish_sla()` call in `_run_locked` therefore runs unconditionally for every live campaign, satisfying the "enforced twice" requirement in AGENTS.md non-negotiable 4.

- ~~**Neither PromQL nor Datadog can be captured on Box A today.**~~ **FIXED 4 Oct 2026.**
  Prometheus is confirmed running on Box A at `10.0.0.79:9090`, reporting
  `hikaricp_connections_max{application="perf-lab"}=20` (baseline). Datadog
  credentials (`DATADOG_API_KEY`, `DATADOG_APP_KEY`, `DATADOG_API_BASE`) are
  set in `~/crucible/.env` on Box B. `build_multi_measure` is wired. The
  capture sweep will exercise both adapters when it reaches `perflab_pool_starved`
  (providers: actuator, promql, datadog). Until those snapshots are captured and
  validated, the provider-agreement claim in EVALUATION.md is marked pending.

  ~~**Also, 26 September 2026: `build_measure` reads Actuator and nothing else.**~~
  **FIXED 4 Oct 2026.** `build_multi_measure` runs ONE load and queries all three
  configured providers (Actuator, PromQL, Datadog) from the same window, returning
  a `dict[provider_name, snapshot]`. Fixture capture uses `build_multi_measure`;
  the campaign uses `build_measure` (Actuator only, by design — the campaign loop
  does not need multi-provider comparison). `MEASURABLE_PROVIDERS` still enforces
  that only known providers can be named.

- ~~**Replay never sends a task's `prompt` to the model.**~~ **FIXED 4 Oct 2026.**
  `ReplayRunner.one_case` passes `stakeholder_request=task.stakeholder_request` to
  `Diagnoser.diagnose`, which frames it as "A stakeholder asks: …" context in the
  system prompt (D1 decision). `Diagnoser.diagnose` accepts `stakeholder_request`
  as a keyword argument and prepends it when non-empty. Class C replay cases now
  measure resistance to stakeholder pressure correctly.
