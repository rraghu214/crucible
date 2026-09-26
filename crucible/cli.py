"""``crucible`` -- the command surface.

Kept thin on purpose: argparse wiring here, behaviour in
:mod:`crucible.perf.commands`. ``DESIGN.md`` section 15 exposes eighteen of the
nineteen screens through the CLI rather than building them, because a screen is
expensive and a command is cheap -- so this file grows, and the growth should
stay mechanical.

The verbs mirror the campaign's own lifecycle, and the split between them is the
one section 9 draws: ``plan`` reads configuration and touches nothing,
``preflight`` exercises the target for real, and only ``run`` measures.
"""

from __future__ import annotations

import argparse
import os


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Crucible HTTP surface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("CRUCIBLE_PORT", "8113")))

    def with_common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--profile", default="spring-boot", help="TargetProfile name")
        p.add_argument("--sla", default="config/slo.yaml", help="path to the SLA")
        return p

    init = sub.add_parser("init", help="create the state directory and report configuration")
    init.add_argument("--state-dir", default=None)
    init.add_argument("--sla", default="config/slo.yaml")

    with_common(sub.add_parser("plan", help="show what a campaign would do; changes nothing"))

    preflight = with_common(
        sub.add_parser("preflight", help="exercise the target once, end to end")
    )
    preflight.add_argument("--workspace", default=".")
    preflight.add_argument(
        "--probe-timeout",
        type=float,
        default=5.0,
        help="seconds to wait on each reachability probe (several are run)",
    )
    preflight.add_argument(
        "--apply-probe",
        action="store_true",
        help="also apply a no-op change and restart the target (off by default: it "
             "genuinely restarts the service)",
    )

    run = with_common(sub.add_parser("run", help="run a campaign"))
    run.add_argument("--scenario", default="db-latency")
    run.add_argument("--users", type=int, default=50)
    run.add_argument("--warmup", type=float, default=120.0, help="seconds, discarded")
    run.add_argument("--measure", type=float, default=300.0, help="seconds, measured")
    run.add_argument("--experiments", type=int, default=5)
    run.add_argument("--run-id", default="")
    run.add_argument("--state-dir", default=None)
    run.add_argument("--workspace", default=".")
    run.add_argument(
        "--approve",
        choices=("file", "preapproved"),
        default="file",
        help="'file' waits for `crucible approve`; 'preapproved' does not ask and is "
             "recorded as such on the manifest",
    )
    run.add_argument("--approval-timeout", type=float, default=3600.0)
    # Pinned per campaign (DESIGN.md 3.2). What actually served each call is read
    # back off the gateway response and recorded per experiment.
    run.add_argument("--provider", default=os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"))
    run.add_argument("--model", default=os.getenv("CRUCIBLE_MODEL", ""))
    # Tracing is optional (DESIGN.md 5). Without --jaeger-url the campaign
    # declares the absence -- traces false, sampling rate null, reason stated --
    # rather than omitting the fields, because an omitted field reads as "not
    # applicable" where the agent needs "not measured" (4.3).
    run.add_argument("--jaeger-url", default="", help="Jaeger query base URL; omit to run without traces")
    run.add_argument("--jaeger-service", default="", help="service name as Jaeger knows it")
    run.add_argument(
        "--trace-sampling-rate",
        type=float,
        default=None,
        help="head sampling rate as a percentage. Omit if unknown: it is then reported "
             "as unknown rather than assumed to be 100%%, because assuming full coverage "
             "turns a 1%% sample into a clean bill of health",
    )

    status = sub.add_parser("status", help="pending approvals, locks and aborts")
    status.add_argument("run_id", nargs="?", default=None)
    status.add_argument("--state-dir", default=None)

    approve = sub.add_parser("approve", help="answer a parked approval")
    approve.add_argument("run_id")
    approve.add_argument("--experiment", type=int, required=True)
    approve.add_argument(
        "--as", dest="responder", required=True,
        help="who is approving; recorded on the manifest",
    )
    approve.add_argument("--reject", action="store_true")
    # W2-Q8. Same verb and same directory -- which is what makes a pause and its
    # resume correlate by experiment number -- but a different file and a
    # different action. It authorises nothing, so it carries no parameters and is
    # exempt from the binding check by construction rather than by exception.
    approve.add_argument(
        "--manual-step-done",
        action="store_true",
        help="confirm you performed a manual step the campaign is paused on "
             "(a report, not an approval; the campaign re-verifies the target)",
    )
    approve.add_argument("--reason", default="")
    approve.add_argument("--state-dir", default=None)

    abort = sub.add_parser("abort", help="stop a campaign at its next experiment boundary")
    abort.add_argument("run_id")
    abort.add_argument("--reason", default="")
    abort.add_argument("--clear", action="store_true", help="remove an abort marker instead")
    abort.add_argument("--state-dir", default=None)

    # `score` calls no model, ever (DESIGN.md 4.6) -- which is what lets scoring
    # weights change without re-running a single experiment.
    score = sub.add_parser("score", help="score saved manifests; calls no model")
    score.add_argument("--journal", default="results", help="directory of campaign manifests")
    score.add_argument(
        "--fixtures",
        default="",
        help="fixture directory. Supplies the trap properties a kept change is checked "
             "against -- whether a property is a metric-gaming shortcut depends on what "
             "is actually wrong, so it is declared per fixture and never inferred",
    )
    score.add_argument(
        "--ground-truth",
        default="",
        help="optional YAML mapping run_id -> true cause family. Nothing links a "
             "campaign manifest to a fixture (a campaign runs against a live target), "
             "so without this the diagnosis dimension reports UNSCORABLE rather than "
             "guessing",
    )
    score.add_argument("--json-out", default="", help="also write the scores as JSON")

    # `report` renders one campaign for a human. Like `score`, it calls no model
    # (DESIGN.md 4.6): a report is a rendering of what was measured, and one that
    # could paraphrase could also soften.
    report = sub.add_parser("report", help="render one campaign's result; calls no model")
    report.add_argument("--journal", default="results", help="directory of campaign manifests")
    report.add_argument(
        "--run", default="", help="run id to report on (default: the most recent campaign)"
    )
    report.add_argument("--json-out", default="", help="also write the report as JSON")

    # `diff` leads with what differs about the SETUP of two campaigns. DESIGN.md 8
    # requires a diff across environments to be flagged rather than silently
    # allowed; every other input to EVALUATION.md's claim format gets the same
    # treatment, because a changed collector or model is just as disqualifying and
    # far less visible. Exits non-zero when the two are not comparable.
    diff = sub.add_parser("diff", help="compare two campaigns, refusing an unsafe comparison")
    diff.add_argument("--journal", default="results", help="directory of campaign manifests")
    diff.add_argument("--a", required=True, dest="run_a", help="first run id")
    diff.add_argument("--b", required=True, dest="run_b", help="second run id")
    diff.add_argument("--json-out", default="", help="also write the comparison as JSON")

    # `capture` records one fixture: one target state, seen through one provider.
    # ONE per invocation, deliberately -- fixtures.capture_fixture refuses to put
    # the target into its broken state, because automating that would mean
    # Crucible writing the very configuration whose effect it is supposed to
    # measure independently. The overnight run is a human setting a property and
    # running this; the verb exists to make that one line rather than six.
    capture = sub.add_parser("capture", help="capture one fixture snapshot from a live target")
    capture.add_argument("--fixture", default="", dest="fixture_id", help="fixture id to capture")
    capture.add_argument(
        "--fixture-config", default="config/fixtures", help="directory of fixture declarations"
    )
    capture.add_argument("--out", default="fixtures", help="where captured snapshots are written")
    capture.add_argument("--profile", default="", help="override the fixture's declared profile")
    capture.add_argument("--sla", default="config/slo.yaml")
    capture.add_argument("--scenario", default="capture")
    capture.add_argument("--users", type=int, default=50)
    capture.add_argument(
        "--warmup", type=float, default=0.0,
        help="seconds of discarded warmup (default: EVALUATION.md's 120; below it is refused)",
    )
    capture.add_argument(
        "--measure", type=float, default=0.0,
        help="seconds of measured window (default: EVALUATION.md's 300; below it is refused)",
    )
    capture.add_argument("--provider", default="actuator", help="which metrics provider to capture through")
    capture.add_argument(
        "--revalidate", type=int, default=0, metavar="N",
        help="run N identical measurements first and refuse to capture if the p99 spread "
             "exceeds 20%%. Run this BEFORE the first capture, never after",
    )
    capture.add_argument(
        "--plan", action="store_true", dest="show_plan",
        help="show what would be captured and how long it would take; touches nothing",
    )

    # `bench` is the replay half: diagnosis, refusal and confidence against saved
    # snapshots, with no live target (DESIGN.md 7).
    bench = sub.add_parser("bench", help="replay a task set against captured fixtures")
    bench.add_argument(
        "--tasks",
        required=True,
        help="task set: a directory of one-task files (config/tasks) or a single "
             "YAML/JSON file",
    )
    bench.add_argument("--fixtures", required=True, help="directory of captured fixtures")
    bench.add_argument("--profile", default="spring-boot")
    bench.add_argument("--sla", default="config/slo.yaml")
    bench.add_argument("--out", default="results/replay.json")
    bench.add_argument("--provider", default=os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"))
    bench.add_argument("--model", default=os.getenv("CRUCIBLE_MODEL", ""))

    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("crucible.main:app", host=args.host, port=args.port, reload=False)
        return 0

    from .perf import commands

    if args.command == "init":
        return commands.cmd_init(state_dir=args.state_dir, sla_path=args.sla)
    if args.command == "plan":
        return commands.cmd_plan(profile_name=args.profile, sla_path=args.sla)
    if args.command == "preflight":
        return commands.cmd_preflight(
            profile_name=args.profile,
            sla_path=args.sla,
            workspace=args.workspace,
            apply_probe=args.apply_probe,
            probe_timeout_s=args.probe_timeout,
        )
    if args.command == "status":
        return commands.cmd_status(run_id=args.run_id, state_dir=args.state_dir)
    if args.command == "approve":
        if args.manual_step_done:
            return commands.cmd_confirm_manual_step(
                args.run_id,
                args.experiment,
                responder=args.responder,
                note=args.reason,
                state_dir=args.state_dir,
            )
        return commands.cmd_approve(
            args.run_id,
            args.experiment,
            responder=args.responder,
            reject=args.reject,
            reason=args.reason,
            state_dir=args.state_dir,
        )
    if args.command == "abort":
        if args.clear:
            return commands.cmd_clear_abort(args.run_id, state_dir=args.state_dir)
        return commands.cmd_abort(args.run_id, reason=args.reason, state_dir=args.state_dir)
    if args.command == "run":
        return commands.cmd_run(
            profile_name=args.profile,
            sla_path=args.sla,
            scenario_name=args.scenario,
            users=args.users,
            warmup_s=args.warmup,
            measure_s=args.measure,
            max_experiments=args.experiments,
            run_id=args.run_id,
            state_dir=args.state_dir,
            workspace=args.workspace,
            approve_mode=args.approve,
            approval_timeout_s=args.approval_timeout,
            provider=args.provider,
            model=args.model,
            jaeger_url=args.jaeger_url,
            jaeger_service=args.jaeger_service,
            trace_sampling_rate_pct=args.trace_sampling_rate,
        )
    if args.command == "score":
        return commands.cmd_score(
            journal_dir=args.journal,
            fixture_dir=args.fixtures,
            ground_truth_path=args.ground_truth,
            json_out=args.json_out,
        )
    if args.command == "report":
        return commands.cmd_report(
            journal_dir=args.journal,
            run_id=args.run,
            json_out=args.json_out,
        )
    if args.command == "diff":
        return commands.cmd_diff(
            run_a=args.run_a,
            run_b=args.run_b,
            journal_dir=args.journal,
            json_out=args.json_out,
        )
    if args.command == "capture":
        return commands.cmd_capture(
            fixture_id=args.fixture_id,
            fixture_config_dir=args.fixture_config,
            out_dir=args.out,
            profile_name=args.profile,
            sla_path=args.sla,
            scenario_name=args.scenario,
            users=args.users,
            warmup_s=args.warmup,
            measure_s=args.measure,
            provider_name=args.provider,
            revalidate=args.revalidate,
            show_plan=args.show_plan,
        )
    if args.command == "bench":
        return commands.cmd_bench(
            tasks_path=args.tasks,
            fixture_dir=args.fixtures,
            profile_name=args.profile,
            sla_path=args.sla,
            out=args.out,
            provider=args.provider,
            model=args.model,
        )
    raise SystemExit(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
