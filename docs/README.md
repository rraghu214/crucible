# Crucible documentation

Start with the [project README](../README.md) for what Crucible is, then
[Getting started](GETTING_STARTED.md) to run it. Everything else is grouped below.
The two governing files sit at the repository root: [`DESIGN.md`](../DESIGN.md)
(every decision and why) and [`EVALUATION.md`](../EVALUATION.md) (how the benchmark works).

## Use it

| Document | What it covers |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Install, run locally, run on a server, run a campaign |
| [HOOKS.md](HOOKS.md) | Python hooks around each experiment: reset and reseed, token refresh, logs |
| [KNOWLEDGE_STORE.md](KNOWLEDGE_STORE.md) | How uploaded documents are indexed and who reads them |
| [DEPLOY_SETUP.md](DEPLOY_SETUP.md) | The push-to-deploy path: deploy key, bare repo, post-receive hook |
| [CLOUD_PROVISIONING.md](CLOUD_PROVISIONING.md) | How the two Oracle boxes were set up |

## Results

| Document | What it covers |
|---|---|
| [BENCHMARK_REPLAY_RESULTS.md](BENCHMARK_REPLAY_RESULTS.md) | Replay: diagnosing frozen snapshots. Run 2 (1 Nov 2026) is the run of record |
| [BENCHMARK_LIVE_RESULTS.md](BENCHMARK_LIVE_RESULTS.md) | Live campaigns, one per fault, with the diagnosis quadrant |
| [bench/](bench/) | Raw replay files; the Benchmarks page reads the top-level `replay-*.json` (the `2026-09-26/` set is test data) |
| [../proofs/](../proofs/) | The latest run of each type, and the live benchmark's manifests, as recorded on Box B |

## Security

| Document | What it covers |
|---|---|
| [SECURITY.md](SECURITY.md) | How Crucible is protected as deployed, what was fixed, what is left |
| [THREAT_MODEL.md](THREAT_MODEL.md) | The working analysis behind it, finding by finding |

## Evidence the design rests on

`DESIGN.md` cites these when it explains a rule. Their raw run files were moved out of
the repository on 3 November 2026; the figures in each document are the record.

| Document | What it showed |
|---|---|
| [SPIKE_VERDICT.md](SPIKE_VERDICT.md) | The feasibility spike: both gates passed |
| [K1_RESULT.md](K1_RESULT.md) · [K1_CLOUD_RESULT.md](K1_CLOUD_RESULT.md) | Measurements are stable enough to judge a change; the 2.08% noise floor |
| [K3_RESULT.md](K3_RESULT.md) | A model handed raw Micrometer units misreads seconds as milliseconds: why the collector converts first |
| [W2_E2E_RESULT.md](W2_E2E_RESULT.md) | The first full campaign end to end on the cloud boxes |

## Engineering record

| Document | What it covers |
|---|---|
| [CRUCIBLE_TEST_ASSERTIONS.md](CRUCIBLE_TEST_ASSERTIONS.md) | Every test assertion with its reasoning, reviewed by the operator |
| [ref/DEBT.md](ref/DEBT.md) | Known, deliberately unfixed debt, and what is planned |
| [COMPLETION_CHECKLIST.md](COMPLETION_CHECKLIST.md) | Capstone steps to submission |
| [crucible-screens-v2.html](crucible-screens-v2.html) | The original 19-screen design reference (DESIGN.md §15) |

## Demo kit

[`myfolder/`](myfolder/): the recording script, the backstage guide, the trainer pre-read,
the trial-run record, and sample files for the demo.
