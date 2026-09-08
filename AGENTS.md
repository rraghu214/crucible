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

**Raghu writes the test assertions and their reasoning. Claude Code
implements against them.** A test written by Claude Code (or any AI) scores
zero in the course this project is submitted to — assertions and their
reasoning must be in Raghu's own hand.

Claude Code may scaffold test files, set up fixtures, and run suites. It must
never decide what "correct" means. `CRUCIBLE_TEST_ASSERTIONS.md` (34 draft
assertions across 8 groups) is a **starting point for Raghu to rewrite**, not
something to commit as-is.

## Session discipline

- Start every session with: "Read AGENTS.md and DESIGN.md before doing
  anything." (The root `CLAUDE.md` does this automatically via `@AGENTS.md` /
  `@DESIGN.md` imports, so this should already be loaded — confirm it, don't
  assume it.)
- Reference files by path. Never paste file contents into the prompt.
- `/clear` between phases (e.g. between "implement the collector" and
  "implement diagnosis") — don't let one phase's context bleed into the next.
- If you're about to propose something that isn't in AGENTS.md or DESIGN.md,
  stop and ask why before writing code.
- Run `uv run pytest -q` and `uv run ruff check .` before every commit. Both
  must pass clean.
- One bug, one PR. Adjacent findings get their own PR, not a bundle.
- `capstone/perf-agent` is the working branch. `main` stays the clean
  S17Code baseline — never commit capstone work to it.

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

- Windows primary. `JAVA_HOME=C:\Raghu\Installs\JAVA\jdk-21.0.3`. Use
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
