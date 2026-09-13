# Scope fallback — Crucible

**Status: NOT ACTIVE.** The plan is full scope per `DESIGN.md` §16. This
document exists so that if scope has to come down, the decision is made against
a ladder written in advance rather than under time pressure at 11pm on 2
October — which is when the wrong things get cut.

Nothing here is a recommendation to cut. Read it only when a trigger below
fires.

---

## 1 · Triggers — when to open this document

Each trigger is a date plus a condition, both checkable without judgement. If a
trigger fires, invoke the corresponding tier. If it does not, close this file.

| Date | Condition | Action |
|---|---|---|
| **21 Sep** (end of week 2) | One full campaign has **not** run end to end — diagnose, propose, approve, apply, restart, re-measure, verdict | Tier 1 |
| **27 Sep** (end of week 3) | `scorer.py` does not produce all six dimensions, **or** no fixtures captured | Tier 2 |
| **30 Sep** | The benchmark has not started | Tier 3 |
| Any time | A deadline extension past 4 Oct is **confirmed in writing** | Close this file, restore full scope |

The week-2 trigger is the important one. The end-to-end campaign is the single
highest-risk item in the project: it is the first time diagnosis, apply,
restart, re-measure and verdict run together, and integration failures there
have no fallback. Everything in weeks 3 and 4 assumes it works.

---

## 2 · The cut ladder

Cuts are taken **in order**. Never skip ahead to a deeper tier while a
shallower one is untaken — the shallow cuts cost the least and are already
sanctioned by `DESIGN.md` §16.

### Tier 1 — the sanctioned order (≈25 h)

This is `DESIGN.md` §16's existing scope-cut list, unchanged. It was decided
before the project started, deliberately, so that a tired future self would not
have to rank these under pressure.

| # | Cut | Saves | What it costs |
|---|---|---|---|
| 1 | Datadog adapter | ~8 h | The "works outside one enterprise's stack" claim weakens from three metrics providers to two. PromQL still covers roughly half the observability market in one adapter (§5), so the argument survives |
| 2 | k6 runner | ~4 h | `LoadRunner` has one implementation instead of two, so the interface is asserted rather than demonstrated |
| 3 | Jaeger trace provider | ~6 h | `available_evidence` still declares `traces: false` honestly — the agent knowing it cannot see traces is the point (§4.3), and that behaviour is testable without ever having a trace provider |
| 4 | FastAPI target profile | ~7 h | **The most expensive cut on this tier.** It is the only evidence that `TargetProfile` genuinely generalises across runtimes rather than being a Spring Boot config file with extra steps. Take this one last |

**Last safe moment:** any time before week 4. These are additive, so dropping
them removes work rather than unpicking it.

### Tier 2 — deeper cuts (≈30 h)

| # | Cut | Saves | What it costs |
|---|---|---|---|
| 5 | **Fixtures 50 → 20** — Java only, 10 cause families × 2 severities (drop the mid severity), drop the Python slice | ~15 h | Overnight capture drops from ~7.5 h to ~3 h, and the replay eval shrinks with it. The benchmark is smaller but still credible: 10 families × 2 severities covers discrimination (class B), and the special cases can be kept by trimming a core family instead |
| 6 | **Campaign UI → CLI only** | ~15 h | `DESIGN.md` §16 already says "capability in, UI polish out", so this is consistent rather than a retreat. The 19 screens in `crucible-screens-v2.html` become design artefacts shown in the pitch rather than built screens. The CLI already exposes everything |

**Last safe moment for #5: before the overnight capture is scheduled.** Once
fixtures are captured, re-capturing at a different count costs the capture time
again. Decide the number first, capture once.

**Last safe moment for #6: before week 4 begins.** Starting the UI and
abandoning it half-built is the worst outcome — it costs the hours and delivers
nothing.

### Tier 3 — last resort (≈20 h)

Only if the 30 Sep trigger fires. Each of these damages the submission, so they
are ranked by how much.

| # | Cut | Saves | What it costs |
|---|---|---|---|
| 7 | Journal RAG | ~8 h | The agent can re-propose a hypothesis it already disproved. Note this explicitly in the claim rather than hiding it |
| 8 | Replay eval across multiple providers — run the benchmark against Actuator only | ~6 h | Answers the fixtures-vs-snapshots question by force: 20 fixtures, 20 snapshots, one provider |
| 9 | Calibration and cost dimensions of the scorer | ~6 h | Scoring drops from six dimensions to four. **Keep the diagnosis 2×2 including `LUCKY`** — see §3 |

---

## 3 · Irreducible — never cut, at any tier

If the work below cannot be finished, the honest response is to submit less
work that is sound, not more work that is unsound.

- **The loop itself.** Diagnose → propose → approve → apply → restart →
  re-measure → verdict. Without it this is a load-testing script with an LLM
  attached, which is precisely what §17 says already exists commercially.
- **Actuator, PromQL, Spring Boot.** `AGENTS.md` names these as the
  irreducible core. Never propose cutting them to save time; ask instead.
- **The measurement-integrity rules (§4 in full).** Unit conversion in the
  collector, mid-run gauge sampling, `available_evidence`, the SLA as
  operator-only Policy memory, verdicts from measured values, the scorer
  calling no model. Every one of these exists because of a specific observed
  failure. Cutting any of them does not save time, it invalidates results.
- **The diagnosis 2×2, including the `LUCKY` quadrant.** An agent that is right
  by accident and an agent that is right by reasoning are different products,
  and outcome-only scoring cannot tell them apart.
- **The HITL approval gate.** Principle 4. A human holds every irreversible
  action.
- **The claim format (`EVALUATION.md`).** See §4.

---

## 4 · What every cut must preserve

A reduced benchmark is honest. A reduced benchmark reported as though it were
the full one is not — and that distinction is the entire product argument.

Whatever is cut, the reported result still states, per `EVALUATION.md`:

> Under task set v1 — N tasks, N fixtures, N repeats — with harness `<sha>`,
> `<model>` pinned, budget $X, ceiling N experiments, profile `<profile>`, on
> `<host>`: N verified fixes, N unverified, N honest failures, N false
> successes, N unreachable. Diagnosis correct on N of M. Zero protected-path
> writes, N refusals.

Plus, explicitly, **what was not built and therefore not tested**. "Trace
provider not implemented; all fixtures declare `traces: false`" is a stronger
sentence than silence, because it tells the reader exactly what the number
covers.

The pitch line for a reduced scope is not an apology. It is: *these are the
adapters that were built and proven; the seams for the rest exist and are
asserted by tests; here is what was measured and here is what was not.*

---

## 5 · If an extension is confirmed

Restore in reverse order of cutting — Tier 3 first, since those cuts damage the
result most. One exception: **the fixture count (#5) does not restore
cheaply.** Going from 20 back to 50 means capturing 30 more fixtures at ~9
minutes each, plus the collector must still be at the same `collector_version`
or the original 20 need recapturing too. Decide the fixture count once, and
prefer to decide it late rather than to revise it.
