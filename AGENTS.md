# AGENTS.md — Crucible

Read this and `DESIGN.md` before doing anything. If you're about to do
something not covered by either, stop and ask why before proceeding.

## What Crucible is

An autonomous performance engineer: it runs controlled load experiments
against a target service, diagnoses SLA misses from its own telemetry,
proposes bounded configuration changes under human approval, re-tests, and
keeps or reverts each change on measured evidence. Full vision, mission and
the five governing principles are in `DESIGN.md` §1 — every rule below exists
to protect one of them.

## The rule that overrides everything

**Claude Code drafts test assertions and their reasoning. The operator reviews
before anything is committed.** For each assertion, flag what needs
checking — not a diff to skim, a specific question: does the reasoning
match the actual failure mode it's testing for, is it testing behaviour or
implementation detail, is there an edge case it's missing. The operator decides
what's correct; Claude Code doesn't self-approve its own tests.
`docs/CRUCIBLE_TEST_ASSERTIONS.md` is the existing draft — start from there
rather than rewriting from scratch. That full path is the only copy; an
identical second copy under `docs/ref/` existed until week 1 and was deleted,
because two files with the same name drift apart silently.

`docs/CRUCIBLE_TEST_ASSERTIONS.md` (34 draft assertions across 8 groups) is the
starting point — extend it rather than starting fresh, and keep it current
as new test areas come up.

## Session discipline

- Start every session with: "Read AGENTS.md and DESIGN.md before doing
  anything." (The root `CLAUDE.md` does this automatically via `@AGENTS.md` /
  `@DESIGN.md` imports, so this should already be loaded — confirm it, don't
  assume it.)
- Reference files by path. Never paste file contents into the prompt.
- Verify against the file before stating what it contains. A partial `grep`
  is not a reading — it truncates lists, misses continuation lines, and
  hides the entry that changes the answer. When a claim rests on what a
  file says, read the relevant span or parse it, then state it. Three
  wrong answers were given in one session about `REASONING_MODEL_HINTS`
  because a truncated grep was retyped from memory; the fourth attempt
  parsed the source and got it right first time.
- Start a **new chat window** between phases (e.g. between "implement the
  collector" and "implement diagnosis") rather than `/clear` — don't let one
  phase's context bleed into the next. (`/clear` itself doesn't delete
  anything either — Claude Code keeps the old session resumable via
  `/resume` — but a new window keeps each phase as its own visible,
  browsable transcript instead of relying on picking the right one out of a
  resume list later.)
- If you're about to propose something that isn't in AGENTS.md or DESIGN.md,
  stop and ask why before writing code.
- Run `uv run pytest -q` and `uv run ruff check .` before every commit. Both
  must pass clean.
- One bug, one PR. Adjacent findings get their own PR, not a bundle.
- `capstone/perf-agent` is the working branch. `main` stays the clean
  S17Code baseline — never commit capstone work to it.
- **The target is a separate repository**: https://github.com/rraghu214/perf-lab.
  It lived at `perf-lab/` inside this repo until 21 September 2026. Crucible's
  workspace is a checkout of the *target's* repo, never of this one, and
  `profile.yaml`'s `config_file` is relative to that workspace. Changes to the
  target go in that repo, not here. See `DESIGN.md` §19.1b.
- Crucible writes to **one** branch on the target, `perftest_sandbox`, created
  from the target's own base branch and never onto it. Enforced in
  `DeployTarget`'s constructor, so every deployer inherits it (`DESIGN.md`
  §19.1a).

## Non-negotiables for any code you write

1. The collector converts units and pre-computes derived values before the
   model sees anything. Never hand the model a raw Micrometer tuple.
2. Gauges are sampled during load; an unsampled gauge is `null`, never `0`.
3. The model is pinned per campaign. `CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS`
   stays empty — no cross-provider failover, ever, under any error condition.
4. The SLA and the load profile are never writable by the agent. This is
   enforced twice — as a protected path in the guard, and as a `Policy`
   memory kind the agent has no write permission for. Don't weaken either
   independently of the other.
5. Every snapshot carries `collector_version`.
6. The scorer calls no model. It reads manifests from disk only.
7. Credential redaction is allowlist, never blocklist, and runs before the
   journal write.
8. `TargetProfile` is read from a file, never hardcoded — even while there's
   only one profile in existence.

Each of these exists because of a specific failure during the feasibility
spike (K1/K3) or a specific design lesson — see `DESIGN.md` §4 for which, and
why weakening it silently reintroduces that failure.

## Environment

- Windows primary. `JAVA_HOME=<jdk-21-home>`. Use
  `./mvnw`, never a bare `mvn`.
- MacBook 16 GB is the local dev lab for anything needing Unix tooling.
- `glc_v5` runs on port 8111 — **do not restart it from inside Claude Code.**
- Gemini free tier, multiple keys in rotation. Add 2–3 s between LLM calls in
  any loop to stay under RPD/RPM limits.
- `locustfile.py` uses `->`, never `→` — Windows cp1252 cannot encode the
  arrow; this has broken things before.

## Before starting new work

Don't infer current scope from memory. Read `docs/ref/DEBT.md` for known,
deliberately-unfixed debt, and `DESIGN.md` §16 (Roadmap) for the current
week's target and what's explicitly out of scope. If a scope cut becomes
necessary, the order is fixed: Datadog adapter → k6 runner → Jaeger → FastAPI
profile. Actuator, PromQL and Spring Boot are the irreducible core — never
propose cutting those to save time; ask instead.
