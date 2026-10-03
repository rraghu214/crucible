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
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Crucible HTTP surface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("CRUCIBLE_PORT", "8113")))

    serve_ui = sub.add_parser("serve-ui", help="run the NiceGUI campaign UI")
    serve_ui.add_argument("--host", default="127.0.0.1")
    serve_ui.add_argument("--port", type=int, default=int(os.getenv("CRUCIBLE_UI_PORT", "8765")))
    serve_ui.add_argument("--root", default=".", help="workspace root (default: cwd)")

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
    run.add_argument(
        "--stakeholder-request",
        default="",
        dest="stakeholder_request",
        help="free text from the person who raised the investigation; shown to the model "
             "as context only -- the SLA and measurements remain the authority",
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

    pause = sub.add_parser(
        "pause", help="pause a campaign at its next experiment boundary (resumable)"
    )
    pause.add_argument("run_id")
    pause.add_argument("--reason", default="")
    pause.add_argument("--clear", action="store_true", help="remove the pause state instead")
    pause.add_argument("--state-dir", default=None)

    resume = sub.add_parser("resume", help="continue a paused campaign from where it stopped")
    resume.add_argument("run_id")
    resume.add_argument("--state-dir", default=None)
    # Resume needs the same config flags as run so it can rebuild the campaign.
    resume.add_argument("--profile", default=None)
    resume.add_argument("--sla", dest="sla_path", default=None)
    resume.add_argument("--scenario", default=None)
    resume.add_argument("--workspace", default=None)

    ceiling = sub.add_parser(
        "ceiling",
        help="stepped load ramp to find the service's capacity knee (§20, operator-declared)",
    )
    ceiling.add_argument("--profile", default=None)
    ceiling.add_argument("--sla", dest="sla_path", default=None)
    ceiling.add_argument("--scenario", default=None)
    ceiling.add_argument("--users", type=int, default=50, help="users at the first step")
    ceiling.add_argument("--warmup", type=float, default=120.0, dest="warmup_s")
    ceiling.add_argument("--measure", type=float, default=300.0, dest="measure_s")
    ceiling.add_argument("--min-users", type=int, default=0)
    ceiling.add_argument("--max-users", type=int, default=500)
    ceiling.add_argument("--step", type=int, default=50, dest="step_size")
    ceiling.add_argument("--run-id", default="")
    ceiling.add_argument("--state-dir", default=None)

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
        "--tag", default="",
        help="locust tag for --revalidate (default: derived from the SLA endpoint). A "
             "fixture capture takes its tag from the fixture and ignores this",
    )
    capture.add_argument(
        "--revalidate", type=int, default=0, metavar="N",
        help="run N identical measurements first and refuse to capture if the p99 spread "
             "exceeds 20%%. Run this BEFORE the first capture, never after",
    )
    capture.add_argument(
        "--plan", action="store_true", dest="show_plan",
        help="show what would be captured and how long it would take; touches nothing",
    )
    capture.add_argument(
        "--all-providers", action="store_true", dest="all_providers",
        help="run ONE load and capture a snapshot for every configured provider "
             "(actuator always; promql if --promql-url; datadog if keys are set)",
    )
    capture.add_argument(
        "--promql-url", default="", dest="promql_url",
        help="Prometheus/PromQL base URL for --all-providers, e.g. http://10.0.0.79:9090",
    )
    capture.add_argument(
        "--promql-instance", default="", dest="promql_instance",
        help="instance label value to pin the PromQL query to one target",
    )
    capture.add_argument(
        "--datadog-url", default=os.getenv("DATADOG_API_BASE", ""), dest="datadog_base_url",
        help="Datadog API base URL (default: $DATADOG_API_BASE or https://api.datadoghq.com)",
    )
    capture.add_argument(
        "--datadog-api-key", default=os.getenv("DATADOG_API_KEY", ""), dest="datadog_api_key",
        help="Datadog API key (default: $DATADOG_API_KEY)",
    )
    capture.add_argument(
        "--datadog-app-key", default=os.getenv("DATADOG_APP_KEY", ""), dest="datadog_application_key",
        help="Datadog Application key (default: $DATADOG_APP_KEY)",
    )

    # `bench` is the replay half: diagnosis, refusal and confidence against saved
    # snapshots, with no live target (DESIGN.md 7).
    export = sub.add_parser("export", help="export collection config as shareable YAML (no credentials)")
    with_common(export)
    export.add_argument("--out", default="", help="output file (default: stdout)")

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
    bench.add_argument(
        "--skip-missing-fixtures",
        action="store_true",
        default=False,
        help="skip tasks whose required fixtures have not been captured yet, "
             "instead of aborting. Use for partial runs while JVM or other "
             "special-setup fixtures are not yet available.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("crucible.main:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.command == "serve-ui":
        os.environ.setdefault("CRUCIBLE_ROOT", str(Path(args.root).resolve()))
        from nicegui import ui  # noqa: PLC0415

        from .ui import nicegui_app as _nicegui_app  # noqa: PLC0415,F401
        ui.run(host=args.host, port=args.port, title="Crucible", favicon="🔥",
               reload=False, show=False)
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
    if args.command == "pause":
        if args.clear:
            return commands.cmd_clear_pause(args.run_id, state_dir=args.state_dir)
        return commands.cmd_pause(args.run_id, reason=args.reason, state_dir=args.state_dir)
    if args.command == "resume":
        return commands.cmd_resume(
            args.run_id,
            state_dir=args.state_dir,
            profile_name=getattr(args, "profile", None),
            sla_path=getattr(args, "sla_path", None),
            scenario_name=getattr(args, "scenario", None),
            workspace=getattr(args, "workspace", None),
        )
    if args.command == "ceiling":
        return commands.cmd_ceiling(
            profile_name=getattr(args, "profile", None),
            sla_path=getattr(args, "sla_path", None),
            scenario_name=getattr(args, "scenario", None),
            users=args.users,
            warmup_s=args.warmup_s,
            measure_s=args.measure_s,
            min_users=args.min_users,
            max_users=args.max_users,
            step_size=args.step_size,
            run_id=args.run_id,
            state_dir=args.state_dir,
        )
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
            stakeholder_request=args.stakeholder_request,
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
        import os as _os  # noqa: PLC0415
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
            tag=args.tag,
            revalidate=args.revalidate,
            show_plan=args.show_plan,
            all_providers=args.all_providers,
            promql_url=args.promql_url,
            promql_instance=args.promql_instance,
            # CLI args take precedence; fall back to env vars so the capture
            # sweep on Box B can use --all-providers without passing keys on
            # the command line (they live in ~/crucible/.env already).
            datadog_base_url=args.datadog_base_url or _os.environ.get("DATADOG_API_BASE", ""),
            datadog_api_key=args.datadog_api_key or _os.environ.get("DATADOG_API_KEY", ""),
            datadog_application_key=args.datadog_application_key or _os.environ.get("DATADOG_APP_KEY", ""),
        )
    if args.command == "export":
        return commands.cmd_export(sla_path=args.sla, profile_name=args.profile, out=args.out)
    if args.command == "bench":
        return commands.cmd_bench(
            tasks_path=args.tasks,
            fixture_dir=args.fixtures,
            profile_name=args.profile,
            sla_path=args.sla,
            out=args.out,
            provider=args.provider,
            model=args.model,
            skip_missing_fixtures=args.skip_missing_fixtures,
        )
    raise SystemExit(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
