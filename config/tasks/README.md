# `config/tasks/` — what the benchmark asks

One file per task. A **task** is a behaviour the harness must exhibit ("can it
separate two causes that look alike?"); a **fixture** is one target application
in one known broken state. `EVALUATION.md` keeps them in separate files on
purpose: `pool=2` is a property of the target, not of the benchmark, so the same
task set runs against a second application by swapping fixtures rather than by
rewriting tasks.

Read by `crucible.perf.replay.load_task_dir`. One task per file rather than one
list of eight, so a reviewer edits the task they are arguing with instead of
finding it inside a list.

## What a task file may and may not declare

| Key | Meaning |
|---|---|
| `id` | names the result row. Duplicates are refused. |
| `task_class` | one of EVALUATION.md's five: A, B, C, D, E. |
| `name` | a short label for a results table. |
| `fixtures` | which fixture ids this task is asked of. Empty means all of them. |
| `prompt` | extra instruction for the model, if the task needs one. |
| `expected_behaviour` | prose: what passing looks like. For the reviewer, and for the report. |
| `asserts_fixture` | **a cross-check, never a source of truth** — see below. |

**`asserts_fixture` restates what the fixture says, and must agree with it.**
The ground truth (`cause_family`) and the integrity traps (`trap_properties`)
live on the fixture, in `config/fixtures/`, written by the human who put the
target into that state *before* any run. A task file may restate them so it reads
on its own; `check_task_fixture_agreement` then refuses a mismatch by name at load
time, before any model is called.

That refusal is deliberate and it does not pick a winner. Preferring the fixture
would make the restatement decorative and let it rot unread. Preferring the task
would put ground truth in a file the benchmark author edits, which is the agent
grading itself one step removed. Refusing means a human who knows which side is
wrong fixes it — the only way to fix it correctly. It is also the failure
`AGENTS.md` already records for two copies of the assertions doc: two files saying
the same thing drift apart silently.

Omit `asserts_fixture` entirely and nothing is checked. State it and it must be
right.

## Class coverage

`EVALUATION.md`: *"A task set with no class C tasks cannot tell you whether the
guard works. Zero violations is an untested zero if the agent never had a real
opportunity to violate."* T3 is that opportunity, and
`crucible.perf.replay.trap_coverage` reports any trap fixture no case ever
proposed — an untempted trap is a weak fixture, not a clean pass.

| Task | Class | Question |
|---|---|---|
| T1 | A | diagnose and repair a connection pool |
| T2 | A | diagnose and repair GC pressure |
| T3 | C | does it refuse the change that games the metric? |
| T4 | D | can it say "nothing is wrong"? |
| T5 | D | can it say "this one is not mine to fix"? |

Class B (discriminate) and class E (ambiguity) have no task file yet. They are
not absent because they do not matter — B is arguably the sharpest test in the
set — but because each needs a fixture pair that does not exist yet: B needs two
fixtures sharing a surface signature, and E needs one whose evidence genuinely
does not settle the question. Recorded here rather than left as a silent gap.
