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
  https://<your-gateway>.onrender.com and `/v1/chat` accepts requests without a
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
  `build_campaign` now constructs a `MemoryStore(state_dir / "memory.db")` and passes it to `Campaign.memory_store` (since 24 Oct 2026: the agent's `memory.sqlite`, opened with the agent's embedder; see assertions group 42). The existing `publish_sla()` call in `_run_locked` therefore runs unconditionally for every live campaign, satisfying the "enforced twice" requirement in AGENTS.md non-negotiable 4.

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

## Deferred by the operator, 25 October 2026 (Part B)

- **Templated request bodies insert values as written** (assertion 40.4). A
  data value containing a quote breaks a JSON body. Escaping would be safer, but
  a numeric column would then arrive as a string. Deferred until a real target
  needs it.
- **Data rows are shared by all simulated users, cycling in order** (40.4). There
  is no per-user sequencing (e.g. each simulated user owning one account).
  Deferred.
- **Hooks are not run as a separate low-privilege OS user** (41.2). The
  environment allowlist is enforced in code, so the control token and API keys
  never reach a hook. Isolation from Crucible's *files* is a documented setup
  step (`docs/HOOKS.md`) and is not yet wired into Crucible (e.g. `sudo -u
  crucible-hooks`).

## Operator direction, 1 November 2026 (Group 46 review)

- **The applicator edits one file, in `.properties` format** (46.4). Operator:
  "even if it is in application.properties or groovy file, it should be able to
  edit it if it has the permissions to edit." Today `profile.yaml` names one
  `config_file` and every allowed property is written into it as `key=value`. A
  property that lives elsewhere (a `jvm.options` file, a `build.gradle` /
  Groovy file, YAML) cannot be a lever yet. The heap lever sits in
  `application.properties`, read by Box A's deploy hook, as a workaround that keeps
  one file per experiment commit. The fix: each allowed property names its file
  and format, the guard checks the file against `protected_paths`, and the
  applicator stages and reverts every file it touched. Each experiment still
  commits only what it changed (§19.1b). Not built before submission.

## Found during the run-2 re-measure, 1 November 2026 (not fixed before submission)

- **Property read-back compares a boolean as Python text.** `verify_properties_via_env`
  compares `str(change.value)` (`'True'`) with what `/actuator/env` returns (`'true'`), so
  run2-04's manifest records "property mismatch after deploy" for a change that was in force.
  Fix: compare with `_format_value`, the way the applicator writes the file. The commit check
  and the measurement were unaffected.
- **The PromQL adapter returns no GC figures.** Prometheus has `jvm_gc_pause_seconds_*`, split
  across several series (action, cause, gc); the profile maps it as one series without
  `aggregate: sum`, and the adapter returns nothing and does not declare it unreadable. Seen in
  `fixtures/perflab_gc_pressure.promql.json`; costs replay tasks T2 and T12 on that view.
- **The Datadog adapter reads the latest interval, not the measured window.** On
  `perflab_gc_pressure` it counts 57 GC pauses where Actuator and Prometheus count 2,338, and its
  longest request is half theirs. Its maxima agree; its counts and totals do not.
- **`crucible run --state-dir` does not create the folder**, and the campaign fails at
  `sqlite3.connect` with "unable to open database file". The run-2 script now creates it.
- **The Actuator/Prometheus audit lives outside the repo** (`audit_metrics.py`). Candidate for a
  `crucible preflight` check (Group 48 review question).

## Operator direction, 2 November 2026 (trial step 9)

- **The workspace is box configuration, not part of the target.** A dashboard launch finds
  the target's checkout through `CRUCIBLE_WORKSPACE` in Box B's `.env`
  (`/home/ubuntu/perf-lab`), added on 2 November when a launch with no workspace fell back
  to `~/crucible` and was refused as dirty. Operator: one app's path in box-wide
  configuration "limits my ability to expand this product as a generic harness for testing
  any apps". Interim, agreed: `~/perf-lab` stays a clean checkout of the target's GitHub
  repo and the line stays until the fix lands. The fix: the target profile declares its
  repo (and the deploy remote's URL, no credentials, §19.8), and Crucible keeps its own
  checkout per service, cloned on first use and fetched before each campaign, on the
  sandbox branch created from `base_branch`. The clean-workspace check stays. DESIGN.md
  §19.1b changes with it. Then remove the `.env` line. Low priority.
- **Kept changes never reach the base branch.** A completed campaign leaves its kept
  changes on `perftest_sandbox` only. Operator: after every completed campaign, Crucible
  opens a PR from the sandbox branch to the base branch, so the owner decides whether the
  change becomes the application's own. The PR is the human gate (principle 4); Crucible
  never merges it, and never writes the base branch (§19.1a). Low priority.
- **No browser tests.** Group 53's contract tests take each request body from the page's
  source and send it to the real route, which catches a page and server that disagree. They
  do not run the page: a click handler that never fires, or a value read from the wrong
  element, still passes. Operator, 2 November 2026: add browser tests (e.g. Playwright) for
  the campaign path (launch, approve, pause, resume, abort) after submission. Low priority.

## Operator direction, 2 November 2026 (trial, steps 9 to 15)

- **The metrics provider cannot be chosen for a live run.** `crucible run` builds the
  Actuator provider directly; only `crucible capture` takes `--provider` (actuator, promql,
  datadog). Operator: "our tool should be able to choose the provider in settings." It is not
  a settings change today: the Datadog adapter reads the latest 10 s interval, not the
  measured window (57 GC pauses where Actuator and Prometheus count 2,338), so a live run on
  it would feed the agent wrong counts and totals. Fix: a `metrics.provider` setting in the
  profile or SLA, shown on the wizard's Target step, with the window aggregation done first.
  Phase 1, the Settings page as a browser-session preview, is built (Group 56, 2 November 2026). Phase 2: a server-side connection store (credentials by environment-variable name, DESIGN.md §8), a `metrics.connection` reference in the profile or SLA and a selector in the wizard, a working Test button, runs that honour the connection, and the Datadog and PromQL window fixes. About 4 to 6 hours plus live validation. Until then the demo says Crucible reads Actuator (DEMO_SCRIPT Step 12).
- **No "run record" in the dashboard.** Results shows the headline, the measured before and
  after, spend and limits; per experiment only the label, p99 and verdict. The approver, the
  deployed commit and the commit the target reports, the model, the earlier attempts the
  agent was reminded of and the audit trail are in the manifest and `audit.jsonl` only (the
  demo shows them on Box B, Step 15). Operator: back-log it. Fix: a Run record card on
  Results (those facts per experiment, the run's audit lines, a link to download the
  manifest JSON). About 1.5 hours with tests.
- **Fact and Playbook memory are designed, not built.** The campaign recalls what earlier
  runs measured (the journal reads past manifests, and experiment 1 of run-1793596989174 was
  handed 2 of them), but it writes no Fact or Playbook, and the Playbooks screen says
  promotion is not built. Five of the seven memory kinds in §13 are live. Next step when it
  is taken up: a human-reviewed promotion from a repeated, measured finding to a
  project-scoped Playbook (§13).
- **Pause and Abort act only between experiments; DESIGN.md §7 says otherwise.** §7:
  "pausing stops the load generator and freezes the scenario's clock"; "abort discards the
  in-flight experiment". The code reads both markers once, before an experiment starts
  (`campaign.py`, top of the experiment loop), so the one in progress finishes first. The
  dashboard's dialogs now say what the code does (Group 55). Decide later whether to build the
  mid-window behaviour or to change §7; until then §7 and the code disagree.
- ~~**`memory.sqlite` held 16 stale document chunks**~~ **FIXED 2 November 2026.** Retired
  (status `superseded`, the store's own re-index statement; the store has no delete, by
  design) and backed up to `~/backups/` on Box B. One audit record that the same statement
  also retired was restored: the audit trail stays as it was.
