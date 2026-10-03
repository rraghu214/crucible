# `config/fixtures/` — the target states, and the truth about them

One file per fixture. A **fixture** is one target application in one known broken
state, **with the true cause recorded before any run**. That last clause is the
whole reason this directory is separate from `config/tasks/`: an agent's own
manifest can never certify whether its diagnosis was right, so the ground truth
has to be written down by the human who broke the target, in advance.

Read by `crucible.perf.fixtures.load_specs`. These are the capture *plan* —
states somebody intends to capture. `load_fixtures` reads the other thing:
snapshots already on disk, each with a `collector_version` that must match.

## Fields

| Key | Meaning |
|---|---|
| `id` | names the snapshot files, `<id>.<provider>.json`. Duplicates are refused. |
| `target_profile` | which runtime profile this state belongs to. |
| `ground_truth_cause_family` | the true cause, in that profile's vocabulary. `none` for a healthy fixture. |
| `severity` | `mild` / `moderate` / `severe` / `none`. |
| `bottleneck_config` | the exact configuration, machine-readable. |
| `setup` | the same thing in prose, for a human reproducing it. |
| `providers` | which metrics backends to capture this state through. |
| `trap_properties` | changes that would improve a headline number without fixing the cause **on this fixture**. |
| `task_classes` | which of EVALUATION.md's five classes this fixture can answer. |
| `validated_at` | the date a K1 re-validation confirmed the signal reproduces. **Empty means not confirmed.** |

## `validated_at` is empty on every fixture here, deliberately

A fixture whose bottleneck does not actually reproduce on the box captures a
snapshot of nothing in particular, and every replay case built on it then scores
the model against an answer that was never in the data — which is indistinguishable,
in the results table, from a model that got it wrong.

`capture_plan` names the unvalidated fixtures rather than refusing them, because
refusing would block the run that validates them. The date goes in once three
identical campaigns have confirmed both that the box is stable (p99 spread under
20%, `k1_revalidation`) and that this fixture's signal is present in the snapshot.

## Why two fixtures are captured through three providers

Provider independence is the actual claim — "whatever your stack" is the first
line of the vision — and the honest way to support it is to show the same target
state diagnosed identically through three different backends. Doing it on
fixtures with a strong signal makes any disagreement unambiguous: if Actuator and
PromQL diagnose the same state differently *there*, the adapter is wrong rather
than the evidence thin.

**One fixture would not have been enough, and the reason is specific.** The
profile declares a separate name, unit and aggregation per metric *family*, so an
adapter can be right about HikariCP and wrong about the JVM. `perflab_pool_starved`
covers the pool; `perflab_gc_pressure` covers the JVM, including `jvm.memory.used`
— the only entry in the profile needing both a label matcher (`area: heap`) and an
aggregation, because Micrometer publishes one series per memory pool. An adapter
summing non-heap along with heap reports a number that is not the thing its name
claims, and nothing but a cross-provider disagreement would show it.

Both also exercise the unit conventions that cost the most to get wrong: the same
Micrometer timer is **seconds** under Prometheus and **nanoseconds** under Datadog
(see the `promql:` and `datadog:` blocks in `config/profiles/spring-boot.yaml`,
and §4.1 for what reading one as the other did to the K3 spike).

`perflab_thread_starved` is the intended third — pool, JVM and Tomcat being three
families — and is blocked behind the same missing gauges that stop it being
captured at all.

Not every fixture needs every provider (`EVALUATION.md`, "Open: fixtures vs
snapshots"). Everything else here is Actuator only.

## Current set

Twenty fixtures across nine cause families, all through Actuator; two through all
three providers for cross-adapter agreement. Twenty-four snapshots total. The
claim made from these twenty must say twenty.

| Fixture | Cause | Severity | Providers |
|---|---|---|---|
| `perflab_pool_starved` | `connection_pool_exhaustion` | severe | actuator, promql, datadog |
| `perflab_pool_starved_mild` | `connection_pool_exhaustion` | mild | actuator |
| `perflab_pool_starved_moderate` | `connection_pool_exhaustion` | moderate | actuator |
| `perflab_gc_pressure` | `gc_pressure` | severe | actuator, promql, datadog |
| `perflab_gc_pressure_moderate` | `gc_pressure` | moderate | actuator |
| `perflab_gc_and_pool` | `gc_pressure` | severe | actuator |
| `perflab_thread_starved` | `thread_pool_saturation` | severe | actuator |
| `perflab_thread_starved_moderate` | `thread_pool_saturation` | moderate | actuator |
| `perflab_inefficient_query_severe` | `inefficient_query` | severe | actuator |
| `perflab_inefficient_query_moderate` | `inefficient_query` | moderate | actuator |
| `perflab_cache_miss_severe` | `cache_miss` | severe | actuator |
| `perflab_cache_miss_moderate` | `cache_miss` | moderate | actuator |
| `perflab_downstream_latency_severe` | `downstream_latency` | severe | actuator |
| `perflab_downstream_latency_moderate` | `downstream_latency` | moderate | actuator |
| `perflab_lock_contention_severe` | `lock_contention` | severe | actuator |
| `perflab_payload_severe` | `payload_serialization` | severe | actuator |
| `perflab_code_latency` | `application_code` | severe | actuator |
| `perflab_code_latency_moderate` | `application_code` | moderate | actuator |
| `perflab_near_sla` | none | none | actuator |
| `perflab_healthy` | none | none | actuator |

`perflab_thread_starved` was unblocked on 3 October 2026 when collector 1.2.0
added `tomcat.threads.busy` and `tomcat.threads.config.max` gauges. Before that
it declared `providers: []` and was captured as excluded; bumping the collector
version invalidated earlier snapshots of it (DESIGN.md §7), so it was recaptured.
