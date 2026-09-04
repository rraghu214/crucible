# Crucible — Flow Diagrams

Two diagrams. **Capstone** is what ships in four weeks. **Product** is the same
loop with every fixed component replaced by an adapter.

Show them in this order. The capstone diagram answers "is this four weeks or four
months"; the product diagram answers "where does this go." Reversing them makes
the scope question harder to answer.

Both render in GitHub, VS Code with the Mermaid extension, and mermaid.live for
PNG/SVG export.

---

## Diagram 1 — Capstone (four weeks)

```mermaid
flowchart TD
    START([Campaign start]) --> CFG["<b>1 · Resolve scenario</b><br/>campaign.yaml<br/>endpoint · load profile · SLA"]

    CFG --> LOAD["<b>2 · Run load experiment</b><br/>LoadRunner → Locust<br/>50 users · 90s · fixed profile"]

    LOAD --> COLLECT["<b>3 · Collect telemetry</b><br/>MetricsProvider → Actuator<br/>gauges sampled DURING load"]

    COLLECT --> SNAP["<b>4 · Build snapshot</b><br/>units converted · derived values<br/>+ available_evidence"]

    JOURNAL[("<b>Journal RAG</b><br/>past experiment manifests")]
    JOURNAL -.->|"hypotheses already<br/>disproven this campaign"| SNAP

    SNAP --> DIAG["<b>5 · Diagnose</b><br/>glc_v5 · model pinned · no failover"]

    DIAG --> PROP["<b>6 · Proposal</b><br/>cause · evidence · ruled_out<br/>change · predicted p99 · confidence"]

    PROP --> GUARD{"<b>7 · Within<br/>authority?</b>"}
    GUARD -->|"no"| REFUSE["<b>Refuse</b><br/>record integrity event"]
    REFUSE --> MANIFEST

    GUARD -->|"yes"| APPROVE{"<b>8 · Human<br/>approves?</b>"}
    APPROVE -->|"no"| DISCARD["<b>Discard</b><br/>record decision"]
    DISCARD --> MANIFEST

    APPROVE -->|"yes"| APPLY["<b>9 · Apply change</b><br/>guarded edit · restart · health check"]

    APPLY --> RETEST["<b>10 · Re-run load</b><br/>identical profile"]

    RETEST --> COMPARE["<b>11 · Compare</b><br/>before / after · p50 p95 p99"]

    COMPARE --> VERDICT{"<b>12 · Improved?</b>"}
    VERDICT -->|"yes"| KEEP["<b>KEEP</b>"]
    VERDICT -->|"no"| REVERT["<b>REVERT</b>"]

    KEEP --> MANIFEST["<b>13 · Write manifest</b><br/>journals/ · immutable"]
    REVERT --> MANIFEST

    MANIFEST --> JOURNAL

    MANIFEST --> SLACHK{"<b>14 · SLA met?</b>"}
    SLACHK -->|"no · budget remains"| LOAD
    SLACHK -->|"no · ceiling reached"| STOPF([Stop — honest failure])
    SLACHK -->|"yes"| DONE([Campaign complete])

    MANIFEST -.-> SCORER["<b>Scorer</b> · separate process<br/>reads manifests · calls no model<br/>outcome · diagnosis · integrity<br/>efficiency · calibration · cost"]

    EVENTS(["event stream"]) -.-> CHAT["<b>Observer chat</b><br/>AG-UI · live campaign state"]
    APPROVE -.-> ALERT["<b>Telegram alert</b><br/>approval needed"]

    classDef guard fill:#fff3cd,stroke:#b8860b,stroke-width:2px
    classDef store fill:#e8f4f8,stroke:#2c7a9c,stroke-width:2px
    classDef terminal fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    class GUARD,APPROVE,VERDICT,SLACHK guard
    class JOURNAL,MANIFEST,SCORER store
    class DONE,STOPF terminal
```

### What each guard is for

| Step | Purpose |
|---|---|
| 7 — Authority | Six allowed properties. The load profile and SLA definition are protected. An agent that can edit its own SLA always passes. |
| 8 — Human | Blocks. Nothing is applied without a human decision. |
| 12 — Verdict | Measured, not predicted. This is what makes an outcome *verified*. |
| 14 — SLA | Bounded by an experiment ceiling and a budget. Running out is an honest failure, not a crash. |

### The two arrows that matter most

**Journal RAG → step 4.** Prior manifests enter the snapshot, so the agent does
not re-propose a hypothesis it already disproved. Reasoning is cumulative across
experiments rather than starting fresh each round.

**Step 13 → step 14 → step 2.** The loop closes on measured evidence. Without
this, every proposal is an unverified pass.

---

## Diagram 2 — Product

Same loop. Every fixed component becomes an adapter.

```mermaid
flowchart TD
    subgraph TARGETS["<b>TargetProfile</b> — what is tunable, where it lives, how to restart"]
        direction LR
        SB["Spring Boot<br/><i>hikari · tomcat threads</i>"]
        FA["FastAPI / Django<br/><i>SQLAlchemy pool · uvicorn workers</i>"]
        EX["Express / NestJS<br/><i>pg pool · event loop</i>"]
        NET[".NET<br/><i>connection pool · thread pool</i>"]
    end

    subgraph LOADGEN["<b>LoadRunner</b>"]
        direction LR
        LOC["Locust"]
        JM["JMeter<br/><i>existing .jmx suites</i>"]
        K6["k6"]
        GAT["Gatling"]
    end

    subgraph METRICS["<b>MetricsProvider</b> — required capability"]
        direction LR
        PROM["<b>PromQL</b><br/><i>Prometheus · Mimir · Thanos<br/>AMP · Azure MP · GCP MP<br/>Chronosphere · VictoriaMetrics</i>"]
        DD["Datadog"]
        DT["Dynatrace"]
        NR["New Relic"]
        EL["Elastic"]
        ACT["<b>Actuator</b><br/><i>zero-config fallback</i>"]
    end

    subgraph TRACING["<b>TraceProvider</b> — optional capability"]
        direction LR
        JAEG["Jaeger"]
        TEMPO["Tempo"]
        DDAPM["Datadog APM"]
        DTAPM["Dynatrace"]
        NONE["<i>none — declared,<br/>not assumed</i>"]
    end

    LOADGEN --> RUN["<b>Run load experiment</b>"]
    TARGETS --> RUN
    RUN --> PULL["<b>Pull telemetry</b>"]
    METRICS --> PULL
    TRACING --> PULL

    PULL --> SNAP2["<b>Build snapshot</b><br/>units normalised · gauge peaks<br/><b>+ available_evidence</b><br/><i>metrics ✓ · traces ✗ · sampling rate</i>"]

    JRAG[("<b>Journal RAG</b><br/>run history")] -.->|"prior hypotheses"| SNAP2
    KRAG[("<b>Knowledge RAG</b><br/>runbooks · architecture<br/>incident history")] -.->|"domain context"| SNAP2

    SNAP2 --> DIAG2["<b>Diagnose</b><br/>cause families scoped to runtime<br/>confidence bounded by evidence"]

    DIAG2 --> PROP2["<b>Proposal</b>"]
    PROP2 --> GUARD2{"<b>Within authority?</b><br/><i>per TargetProfile</i>"}
    GUARD2 -->|"no"| REF2["Refuse · log integrity event"]
    GUARD2 -->|"yes"| HUMAN{"<b>Human approves?</b><br/><i>chat · Slack · Telegram</i>"}

    HUMAN -->|"no"| DISC2["Discard"]
    HUMAN -->|"yes"| EDIT["<b>Edit config only</b><br/>never a deploy on its own"]

    EDIT --> DIFF["<b>Human reviews diff<br/>and pushes</b>"]
    DIFF --> CICD["<b>CI/CD</b><br/>Jenkins · GitHub Actions · GitLab<br/><i>human-triggered, opt-in</i>"]

    CICD --> DEPLOY["<b>Deploy to pre-prod</b>"]
    DEPLOY --> RETEST2["<b>Re-run load · identical profile</b>"]
    RETEST2 --> CMP["<b>Compare</b><br/>IMPROVEMENT / REGRESSION / NO_CHANGE"]
    CMP --> KR{"<b>Keep or revert?</b>"}
    KR --> MAN2["<b>Manifest</b>"]
    REF2 --> MAN2
    DISC2 --> MAN2
    MAN2 --> JRAG
    MAN2 --> SCORE2["<b>Scorer</b> · reads manifests<br/>outcome · diagnosis · integrity<br/>efficiency · calibration · cost"]
    MAN2 --> LOOP{"<b>SLA met?</b>"}
    LOOP -->|"no · budget remains"| RUN
    LOOP -->|"yes"| FIN([Complete])

    MCP["<b>MCP interface</b><br/><i>run_campaign · get_result</i>"] -.->|"another agent<br/>commissions a campaign"| RUN

    classDef adapter fill:#f0f7ff,stroke:#4a90d9,stroke-width:2px
    classDef guard fill:#fff3cd,stroke:#b8860b,stroke-width:2px
    classDef store fill:#e8f4f8,stroke:#2c7a9c,stroke-width:2px
    class TARGETS,LOADGEN,METRICS,TRACING adapter
    class GUARD2,HUMAN,KR,LOOP guard
    class JRAG,KRAG,MAN2,SCORE2 store
```

### The design guarantee for approvers

The agent never has write access to a deployment pipeline and never triggers a
deploy on its own. Unattended, it can do exactly three things: run load, read
telemetry, and write a proposal. Every step after the human-approval gate requires
a person.

### Why PromQL is one adapter, not eight

Prometheus, Mimir, Thanos, VictoriaMetrics, Chronosphere, AWS Managed Prometheus,
Azure Managed Prometheus and Google Managed Prometheus all answer PromQL. One
implementation covers all of them.

Datadog, Dynatrace, New Relic and Elastic each have proprietary query languages
and need their own adapter. That is the market shape, not a design choice.

### Capabilities are declared, never assumed

`available_evidence` travels with every snapshot:

```json
{
  "metrics": true,
  "traces": false,
  "trace_reason": "ActuatorMetricsProvider has no trace store",
  "trace_sampling_rate": null,
  "endpoint_breakdown": true
}
```

Without this the agent cannot distinguish *"I looked and found no slow spans"*
from *"I never looked at spans"* — and will eliminate a live hypothesis on
evidence it never gathered. That is the same failure as the spike's K3 attempt 1:
`pending: 0` did not mean the pool was healthy, it meant the gauge was read after
the load drained.

Where traces exist, sampling rate matters too. Most production tracing runs at
1–10% head sampling, and a p99 outlier is by definition rare — so "no slow spans"
can be misleading even on a fully instrumented deployment.

---

## Rendering for slides

1. Paste a block into <https://mermaid.live>
2. Export SVG for slides, PNG for documents
3. For a five-minute pitch, Diagram 1 is the one on screen. Diagram 2 belongs on
   the roadmap slide, or in the appendix if questions go there.

Diagram 2 is dense on purpose — it is a document diagram, not a slide. If it must
go on a slide, split it: adapters on one, loop on another.
