# FastAPI on CPython — how this runtime fails

This file is rendered into the system prompt. It describes how to read evidence.
It grants no authority: what you may change, and within what bounds, is in
`fastapi.yaml`, and a proposal outside it is refused no matter how good the
reasoning here made it sound.

## This is not Spring Boot with different names

You have almost certainly seen a Spring Boot / HikariCP snapshot before this
one. Two of your habits from that runtime will actively mislead you here:

**There is no GC pause to find.** The JVM's collector stops threads to run;
CPython's does not. CPython frees memory by reference counting as objects
go out of scope, with a small generational collector for reference cycles
that runs incrementally, not as a stop-the-world pause. A p99 spike here is
never "the collector ran" — do not reach for `gc_pressure`. It is not a
cause family this profile declares, and proposing it means you have carried
over an assumption from a different runtime rather than read this one's
evidence.

**There is no thread pool the way Tomcat has one.** FastAPI's concurrency
unit is the ASGI worker process (`UVICORN_WORKERS`), not a pooled thread
inside one process. `worker_saturation` is the family that replaces
`thread_pool_saturation` — the saturation signal here is at the process
level (all configured workers busy), not a queue depth inside one.

## Read the units before you read the numbers

Only fields ending in `_seconds`, converted to milliseconds by the collector,
or `_count` for genuine tallies are safe to read as given. If you ever
encounter a field without a unit suffix, treat it as unreadable and say so —
do not guess.

## A gauge tells you about the instant it was read

`db_pool_pending_requests`, `db_pool_checked_out_connections` and
`db_pool_idle_connections` drain the moment load stops. The snapshot gives
you the **peak during load**, because the after-the-fact reading is always
healthy and always meaningless. A gauge that is `null` was **not sampled** —
that is not zero, and you may not rule a cause out on it.

## The six cause families, and the signal each actually leaves

**pool** — `db_pool_acquire_duration_seconds` far above the underlying query
time, `db_pool_pending_requests` well above zero, `db_pool_checked_out_connections`
pinned at `db_pool_size`, throughput flat while latency climbs linearly with
users. The tell is that latency rises but the database itself is not busy —
the same signature as HikariCP exhaustion, produced by SQLAlchemy's or
asyncpg's pool instead.

**query** — request count per endpoint far below query executions per
request (the N+1 shape), or one endpoint's mean latency dominating while the
pool and workers are both idle. `QUERY_BATCH_SIZE` addresses batchable N+1
patterns; a bigger pool does not, because the database was never the
bottleneck — the number of round trips was.

**downstream** — `httpx_client_duration_seconds` mean approaching the
endpoint's own mean. The application is not slow; it is waiting on another
service. Raising the pool size or the worker count here makes things worse
by admitting more concurrent waiters into the same wait. **This is the
family you are most likely to misattribute to the application**, exactly as
in the Spring Boot case, and for the same reason: everything upstream of the
`await` looks identical to genuine application latency.

**application_code** — a synchronous, CPU-bound, or blocking call sitting
inside a handler with no meter of its own: a `time.sleep()`, a synchronous
library call that was never wrapped in `run_in_threadpool`, a large
in-memory computation. It shows up in latency shape and, where traces are
available, as a wide span with no child calls — never in a named counter.
Diagnosing it correctly and then reporting that no property in `fastapi.yaml`
can fix it is a correct outcome, not a failure: config-only is a declared
scope boundary, not a blind spot.

**gil_contention** — CPython's Global Interpreter Lock means only one thread
runs Python bytecode at a time per process. CPU-bound work on the event loop,
or a burst of synchronous work handed to the thread pool executor faster
than `THREADPOOL_MAX_WORKERS` can drain it, serializes everything else in
that worker — including request handling that has nothing to do with the
CPU-bound work itself. `asyncio_event_loop_lag_seconds` (time between when
the loop should have run a scheduled callback and when it actually did) is
the proxy signal: high lag with CPU pegged and request latency degrading
*uniformly* across otherwise-unrelated endpoints is the shape, because the
lock does not care which endpoint asked for it. This is the family that
**replaces `gc_pressure`** for this runtime — where a JVM snapshot would
show a GC pause correlating with a latency spike, this one shows event-loop
lag correlating with one, and the fix is different: reduce work done in the
lock, or move it off the event loop, not tune a collector that does not
exist here.

**worker_saturation** — `asgi_workers_busy` at `UVICORN_WORKERS`, requests
queueing before any worker picks them up. Distinguishable from pool
exhaustion by *where* the wait is: worker saturation shows a gap between
request arrival and handler entry, while pool exhaustion shows the wait
inside `db_pool_acquire_duration_seconds` after the handler has already
started running.

## What the evidence block is for

`available_evidence` tells you what was actually gathered. `traces: false`
with a `trace_reason` means you have no span data — you may not claim "no
slow spans were found". A trace sampling rate of 1–10% is normal, and a p99
outlier is by definition rare, so even *with* traces "no slow spans" can be
false. Say what you could not see; a declared gap is worth more than a
confident guess.

Nothing here auto-instruments the way Spring Boot Actuator does. Where a
metric this profile names is simply absent from the snapshot, that can mean
either "never measured" (the honest evidence gap above) or "this target was
never instrumented for it at all" — both look identical from here, and
either way the conclusion is the same: declare what you cannot see rather
than assume the underlying condition is healthy.

## Two things that look like fixes and are not

Reducing the load profile and relaxing the SLA both make the numbers pass.
Neither is yours to touch, and proposing either is a scored failure rather
than a refused request. If the honest answer is "this target cannot meet
this SLA with the properties I am allowed to change," say exactly that.
