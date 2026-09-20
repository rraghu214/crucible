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
        )
    raise SystemExit(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
