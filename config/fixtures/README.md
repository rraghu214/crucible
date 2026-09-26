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

| Fixture | Cause | Severity | Providers |
|---|---|---|---|
| `perflab_pool_starved` | `connection_pool_exhaustion` | severe | actuator, promql, datadog |
| `perflab_pool_starved_mild` | `connection_pool_exhaustion` | mild | actuator |
| `perflab_gc_pressure` | `gc_pressure` | severe | actuator, promql, datadog |
| `perflab_thread_starved` | `thread_pool_saturation` | severe | *(none — excluded)* |
| `perflab_healthy` | none | none | actuator |
| `perflab_code_latency` | `application_code` | severe | actuator |

Nine snapshots from the five fixtures being captured, ~9 minutes each: about an
hour and a quarter, not the 7.5-hour overnight run EVALUATION.md prices at fifty
fixtures. That gap is
real and is not hidden: this is the *minimum* set the week-4 brief asked for, and
EVALUATION.md's full grid is 10 families × 3 severities plus 10 special cases plus
a 10-fixture Python slice. The claim made from these six must say six.

## `perflab_thread_starved` is declared but excluded

It needs `tomcat.threads.busy` and `tomcat.threads.config.max` in the profile's
`gauges` / `snapshot_metrics`. They are in its `metric_map` but nowhere the
collector reads, so the snapshot would carry no thread fields at all — not null
ones, absent ones — and the agent would correctly report that it could not check.
Honest, and useless. Capturing it first would bake that absence into every replay
built on it.

**Accepted and documented rather than fixed** (operator, 26 September 2026 —
`docs/ref/DEBT.md`). It declares `providers: []`, which is what the capture plan
reads, so nothing has to remember to skip it. It stays declared because deleting
it would lose the reasoning about *where* the wait is — connector versus acquire
— which is the sharpest discrimination pair in the set.

Closing the gap later means bumping `COLLECTOR_VERSION`, which invalidates every
fixture captured before it (DESIGN.md §7). That is the argument for doing it
before a capture run rather than after one.
