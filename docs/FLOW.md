# Crucible — Flow Diagram

The flow diagram lives in one place: the **Architecture** section of the
[README](../README.md#architecture). Solid boxes are what the code does today;
dashed boxes are designed or planned, not built.

It replaced the two diagrams that were here until 3 November 2026 (a week-1
"capstone" loop and a "product" adapter view). Both had drifted from the code:
they showed `campaign.yaml` (it is `config/slo.yaml`), a Telegram approval alert
and an MCP interface (neither exists), the Knowledge RAG feeding diagnosis (it
feeds the chat and the AI suggestions), a human pushing every diff (Crucible
pushes to the sandbox branch itself, after approval), and k6, JMeter and Gatling
as built runners (Locust is the only one). Two copies of one diagram drift apart
silently, which is why there is now one.

To export it as an image: paste the README's Mermaid block into
<https://mermaid.live>, then export SVG for slides or PNG for documents.
