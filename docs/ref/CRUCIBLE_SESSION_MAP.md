# Crucible — course concept map

Which EAG V3 session each part of Crucible comes from.

**Corpus:** every session-notes PDF and transcript under `C:\Raghu\MyLearnings\EAG_V3\S*\`
— S1 through S19, extracted to text and searched. Complete coverage, with one caveat:
**S5 has only a single printed lesson page** (~750 chars) on disk, so S5's mapping leans
on the S6 notes, which describe S5's `agent5.py` monolithic loop in detail.

Two blocks below: what is **in the four-week build**, and what is **not in it but has
potential**. Both are on the slide.

---

## Block A — in the build

| # | Session | The topic | Where it lands in Crucible |
|---|---|---|---|
| **3** | Developer Foundations & First Agent | Agent skeleton; typed tool contracts | `crucible/perf/` package layout; the campaign entry point |
| **5** | Planning & Reasoning with LLMs | One reasoning call over structured evidence, not a chat loop; `uv` packaging | `DiagnosisEngine` — a single `glc_v5` call returns cause + evidence + `ruled_out` + confidence as typed JSON |
| **6** | Agentic Architecture | **Memory / Perception / Decision / Action** as four typed roles over Pydantic contracts | The whole loop: collector = Perception, `DiagnosisEngine` = Decision, `ChangeApplicator` = Action, `journals/` = Memory |
| **7** | Memory & Retrieval — Embeddings, FAISS | Vector retrieval when surface tokens don't match. **Plus the role-boundary drift**: tool-selection leaked from Decision into Perception, found by diagnostic discipline, fixed by a diff | Journal RAG (week 3) — past manifests retrieved into the next snapshot. And the reason diagnosis never touches the applicator |
| **8** | Multi-Agent DAG Orchestration | The **Critic was a rubber stamp** — an LLM judging an LLM. *"The wiring is mechanism; the verdict quality is policy; they are separate problems, addressed in separate places."* | The single most load-bearing lesson: Crucible's verdict is a **measurement**, not a model judging itself, and the scorer is a separate process that calls no model |
| **11** | Channels, Voice and Gateway | Channel adapters over one gateway; approval delivered over a channel | Telegram alert on the human-approval gate; `glc_v5` as the single model seam |
| **12** | Cybersecurity, Sandboxes, Adversary's Mindset | Threat-modelling by asking *"where has someone misplaced their trust?"*; per-adapter container walls; a rogue adapter that poisons the ledger and clears the audit log | `CRUCIBLE_SANDBOX_ROOT`; protected paths; **"an agent that can rewrite its own SLA always passes"** is that question asked of my own design. Eval task T10 — an injected instruction in a log line is data, not instruction |
| **13** | Graphs, Memory, Semantic Chunking, A2A | **A live graph that grows from outcomes** instead of planning the whole future upfront; typed memory with provenance | Next-experiment selection — experiment *n+1* is earned from the measured result of *n*, rather than pre-planning five |
| **14** | Generative UI, A2UI and AG-UI | Declarative component tree from a **trusted catalog**; an event channel streaming graph events to the browser; an injection attempt rejected by the catalog validator | **Observer chat over the live campaign event stream** — watch a campaign as it runs. The catalog rule holds too: the agent emits a typed proposal, never executable text |
| **15** | Model Routing, Agent Economics, Observability | Model pinned / failover off; **cost per resolved task, not cost per token**; spans and traces | Budget $0.05 per campaign; cost + wall-clock as a scoring dimension; `gemini-3.5-flash-lite` pinned with `auto_route` off so runs stay comparable |
| **16** | Event Driven Autonomous Agents | *"An autonomous agent is one that stops in the right state and continues for the right reason."* Relevance gate, rate and budget limits, idempotency, atomic checkpoints | Experiment ceiling of 5; budget abort; **honest failure as a legitimate end state**; one immutable manifest per experiment |
| **17** | Agentic Coding & Markdown-as-Code Skills | **The S17Code base**: protected-path guard, bounded command runner, validator, failure ceiling. *"Getting an agent to write code is the easy half. Knowing whether it worked is the hard one."* | Not "inspired by" — Crucible **is** S17Code extended with `crucible/perf/`. The guarded edit in `ChangeApplicator`; the verdict gate answers the hard half |
| **18** | Evaluating Agents, and Why Benchmarks Lie | Benchmark = **task + harness + policy + scorer + manifest**. Four outcomes: verified / unverified / honest failure / false success. *"A zero means something only when the event was possible."* | `EVALUATION.md` wholesale — the outcomes kept by name, the reachability contract, integrity scored separately, the scorer re-scoring from disk. **My addition:** diagnosis scored separately from outcome, which is what exposes `LUCKY` |
| **19** | Capstone scoping | Five asks; who does this today; four weeks, not four months | This deck's structure, and the four-week cut |

---

## Block B — not in the four weeks, but has potential

| # | Session | The topic | What it could unlock for Crucible |
|---|---|---|---|
| **1–2** | Transformer Architecture; LLM Internals & the 2026 Landscape | Attention, tokenisation, the model landscape | Background. Justifies why a **small pinned model** is enough: diagnosis is structured reasoning over ~40 numbers, not open-ended generation. Would matter if I ever swap models — that becomes a different claim |
| **4** | Model Context Protocol | MCP as a tool surface; `dotenv` secret separation | `.env` separation is already in the protected paths. **Roadmap:** an MCP interface (`run_campaign`, `get_result`) so another agent can commission a performance campaign — Crucible as a tool, not just a tool user |
| **9** | Browser Agents & Autonomous Web | A **four-layer cascade** — *"do the cheap thing first, escalate only when the cheap thing fails"* — priced free → free → cheap text → vision. And a new skill plugged in with *"only a yaml entry, a prompt file, and a small dispatch branch"* | Two real ones. **(a)** The adapter pattern `TargetProfile` / `MetricsProvider` / `LoadRunner` copies "plug in without runtime edits". **(b)** Cheapest-discriminating-experiment-first: under a 5-experiment ceiling, order hypotheses by what a cheap experiment can eliminate |
| **10** | Computer-Use Agent | AX tree vs screenshot; OS-dependence; *"we can map the traps, you still have to walk through them"* | Genuinely unused — every Crucible action is a config edit or an HTTP call, deliberately narrower because narrower is auditable. **Potential:** driving a vendor APM console that has no API. Low priority, high fragility |

---

## The four strongest lineages — worth saying out loud

1. **S8 → the verdict gate.** The Critic that rubber-stamped is why Crucible never lets a
   model grade its own work. "Mechanism vs policy, addressed in separate places" is
   literally the runner/scorer split.

2. **S18 → `EVALUATION.md`.** The four outcomes are the session's, kept by name.
   `UNREACHABLE` is *"a zero means something only when the event was possible"* turned
   into a field. Crucible adds diagnosis-vs-outcome scoring on top.

3. **S17 → the codebase.** Crucible *is* S17Code extended. The protected-path guard and
   failure ceiling are inherited, not rebuilt.

4. **S12 → the authority boundary.** "Where has someone misplaced their trust?" asked of
   my own agent gives exactly one answer: the SLA definition. So it is protected.

---

## Coverage note

**Fourteen of nineteen sessions are load-bearing in the four-week build.** Three more
(S4, S9, S10) contribute a roadmap item or a pattern already borrowed. Only S1–S2 are
pure background.

## Inconsistency to resolve in the deck

The observer chat is a four-week deliverable and appears in FLOW.md's capstone diagram,
but **slide 6 does not mention it** — neither the "In" list nor the week table names it.
Options: add it to week 4 alongside deploy + eval, or add "observer chat over the live
event stream" to the In list. Flagging rather than changing it unasked.
