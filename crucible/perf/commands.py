"""The CLI surface: init, plan, preflight, run, status, approve, abort.

``DESIGN.md`` section 15 puts nineteen screens in scope as *capability* and only
the campaign path in scope as UI. Everything else is exposed here, because a
screen is expensive and a CLI command is cheap -- and because the CLI is the
surface many engineers will only ever use (screen 19).

Two commands deserve their reasoning stated, because they look redundant:

**``plan`` reads configuration and touches nothing.** Terraform's plan/apply
pattern, no Terraform involved. For a tool that edits configuration on a running
service and restarts it, "show me first" is a command rather than a checkbox
(section 9). It answers what *would* happen: which profile, which SLA, which
properties are in bounds, where a deploy would go, and whether anything is
declared manual.

**``preflight`` exercises the target for real.** One tiny end-to-end experiment
including a restart and a revert, before any campaign depends on those working.
It is the difference between discovering a broken restart during preflight and
discovering it eight minutes into the first measured run.

Every function here returns an exit code and prints. None of them raise on an
expected failure: a refusal is information, and an operator should get the reason
rather than a traceback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .applicator import Applicator, Change, CommandRestarter, ManualRestarter, Proposal, guard_proposal
from .approval import (
    DEFAULT_STATE_DIR,
    confirm_manual_step,
    pending_approvals,
    pending_manual_steps,
    write_decision,
)
from .campaign import (
    CampaignRefused,
    Sla,
    abort_marker_path,
    check_environment,
    request_abort,
)
from .deploy import GitPushDeployer, ManualDeployer, read_running_commit
from .profile import TargetProfile

OK = 0
FAILED = 1
REFUSED = 2


def _state_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.getenv("CRUCIBLE_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()


def _rule(title: str) -> str:
    return f"\n{title}\n" + "-" * len(title)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(state_dir: str | None = None, sla_path: str = "config/slo.yaml") -> int:
    """Create the state directory and report what configuration already exists.

    Deliberately does not write an SLA. A generated default would be a threshold
    nobody chose, and the agent would then be measured against a goalpost that
    arrived by accident -- which is the same failure as letting the agent write
    one, reached by a friendlier route.
    """
    root = _state_dir(state_dir)
    for child in ("approvals", "locks", "aborts", "results"):
        (root / child).mkdir(parents=True, exist_ok=True)
    print(f"state directory ready: {root}")

    print(_rule("configuration"))
    sla_file = Path(sla_path)
    if sla_file.exists():
        try:
            sla = Sla.load(sla_file)
            print(f"  SLA          : {sla_file} ({sla.name})")
            print(f"  objective    : {sla.endpoint} p99 <= {sla.p99_ms:.0f} ms")
            print(f"  environment  : {sla.environment_name} [{sla.environment_kind}]")
            print(f"  noise floor  : {sla.noise_p99_spread_pct:.2f}% (measured {sla.noise_measured_on})")
        except CampaignRefused as refused:
            print(f"  SLA          : {sla_file} EXISTS BUT IS UNUSABLE -- {refused}")
            return REFUSED
    else:
        print(f"  SLA          : MISSING at {sla_file}")
        print("                 Write it by hand. It must declare objective.p99_ms,")
        print("                 environment.kind and a measured noise.p99_spread_pct.")
        print("                 Crucible will not generate one: a threshold nobody")
        print("                 chose is a goalpost that arrived by accident.")

    try:
        profile = TargetProfile.named("spring-boot")
        print(f"  profile      : {profile.source_path} ({len(profile.allowed_properties)} tunable properties)")
    except (FileNotFoundError, ValueError) as exc:
        print(f"  profile      : NOT LOADABLE -- {exc}")
        return FAILED
    return OK


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def cmd_plan(profile_name: str = "spring-boot", sla_path: str = "config/slo.yaml") -> int:
    """Show what a campaign would do. Reads config; changes nothing.

    The output is deliberately exhaustive about *authority*: which properties may
    move and between what bounds, which paths can never be written, and where a
    deploy would land. Those are the answers an operator needs before agreeing to
    let something edit a running service, and burying them behind a run is how
    you get an approval nobody understood.
    """
    try:
        profile = TargetProfile.named(profile_name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot load profile {profile_name!r}: {exc}")
        return FAILED
    try:
        sla = Sla.load(sla_path)
    except CampaignRefused as refused:
        print(f"cannot load SLA: {refused}")
        return REFUSED

    print(_rule("plan (nothing will be changed)"))
    print(f"profile       : {profile.name} ({profile.runtime}) from {profile.source_path}")
    print(f"SLA           : {sla.name} from {sla.source_path}")
    print(f"objective     : {sla.endpoint} p99 <= {sla.p99_ms:.0f} ms, errors <= {sla.error_rate_pct:.2f}%")
    print(f"environment   : {sla.environment_name} [{sla.environment_kind}] at {sla.target_base_url}")
    print(f"noise floor   : {sla.noise_p99_spread_pct:.2f}%  (a smaller move is INCONCLUSIVE, not a win)")
    print(f"steal ceiling : {sla.cpu_steal_abort_pct:.1f}%  (per-environment, not a constant)")

    try:
        check_environment(sla)
        print("environment   : accepted")
    except CampaignRefused as refused:
        print(f"environment   : REFUSED -- {refused}")
        return REFUSED

    print(_rule("cause families the agent may name"))
    for family in profile.cause_families:
        print(f"  {family}")

    print(_rule("properties the agent may change"))
    for prop in sorted(profile.allowed_properties):
        bounds = profile.allowed_properties[prop]
        span = []
        if bounds.minimum is not None:
            span.append(f"min {bounds.minimum}")
        if bounds.maximum is not None:
            span.append(f"max {bounds.maximum}")
        if bounds.allowed_values is not None:
            span.append(f"one of {bounds.allowed_values}")
        print(f"  {prop:<58} {bounds.kind:<5} {', '.join(span)}")

    print(_rule("paths the agent can never write"))
    for path in profile.protected_paths:
        print(f"  {path}")
    print("  (the SLA and the load profile are protected here AND as Policy memory;")
    print("   neither half is to be weakened without the other)")

    print(_rule("deploy"))
    deploy = profile.deploy
    print(f"  mode        : {deploy.mode}")
    print(f"  remote      : {deploy.remote or '(unset)'}")
    print(f"  branch      : {deploy.branch or '(unset)'}")
    print(f"  version url : {deploy.version_url or '(unset)'}")
    if not deploy.automated:
        print("  NOTE        : deploy is manual, so the campaign will block with")
        print("                instructions and record the manual step on the manifest.")

    print(_rule("restart"))
    restart = profile.restart
    print(f"  manual      : {restart.manual}")
    print(f"  command     : {' '.join(restart.command) if restart.command else '(none)'}")
    print(f"  health url  : {restart.health_url or '(unset)'}")
    return OK


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def cmd_preflight(
    profile_name: str = "spring-boot",
    sla_path: str = "config/slo.yaml",
    *,
    workspace: str = ".",
    apply_probe: bool = False,
    probe_timeout_s: float = 5.0,
) -> int:
    """Exercise the target end to end once, before a campaign depends on it.

    ``probe_timeout_s`` bounds each reachability probe. Preflight runs several,
    and an operator on a machine with no route to the target should get an answer
    in seconds rather than waiting out a full connect timeout per check.

    ``apply_probe`` is off by default. Preflight's reachability checks are
    read-only and safe to run anywhere; the apply/restart/revert rehearsal
    genuinely restarts the service, so it is opt-in rather than something an
    operator triggers by typing the obvious command.
    """
    checks: list[PreflightCheck] = []

    try:
        profile = TargetProfile.named(profile_name)
        checks.append(PreflightCheck("profile loads", True, str(profile.source_path)))
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot load profile: {exc}")
        return FAILED

    try:
        sla = Sla.load(sla_path)
        checks.append(PreflightCheck("SLA loads", True, f"{sla.name}, p99 <= {sla.p99_ms:.0f} ms"))
    except CampaignRefused as refused:
        print(f"cannot load SLA: {refused}")
        return REFUSED

    try:
        check_environment(sla)
        checks.append(
            PreflightCheck("environment is not production", True, f"{sla.environment_kind}")
        )
    except CampaignRefused as refused:
        checks.append(PreflightCheck("environment is not production", False, str(refused)))

    # The SLA and the load profile must be unwritable. Asserted here rather than
    # assumed, because this is the boundary that matters most and a profile that
    # had drifted would otherwise only be discovered by an agent exploiting it.
    from .applicator import path_is_protected

    for guarded in (sla_path, "locust/locustfile.py"):
        hit = path_is_protected(guarded, profile.protected_paths)
        checks.append(
            PreflightCheck(
                f"{guarded} is protected",
                bool(hit),
                f"matches {hit!r}" if hit else "NOT protected by this profile -- the agent could write it",
            )
        )

    # Metrics reachability. Non-blocking on its own: the operator may be running
    # preflight from a machine with no route to the target, and that is worth
    # reporting rather than treating as a broken configuration.
    checks.append(_check_metrics(profile, sla, probe_timeout_s))
    checks.append(_check_version_endpoint(profile, probe_timeout_s))
    checks.append(_check_deploy_config(profile))

    if apply_probe:
        checks.extend(_apply_revert_rehearsal(profile, workspace))
    else:
        checks.append(
            PreflightCheck(
                "apply/restart/revert rehearsal",
                True,
                "skipped (pass --apply-probe to actually restart the target)",
                blocking=False,
            )
        )

    print(_rule("preflight"))
    worst = OK
    for check in checks:
        mark = "PASS" if check.ok else ("FAIL" if check.blocking else "warn")
        print(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.blocking:
            worst = FAILED
    print()
    print("preflight passed" if worst == OK else "preflight FAILED -- fix the above before running a campaign")
    return worst


def _check_metrics(profile: TargetProfile, sla: Sla, timeout_s: float = 5.0) -> PreflightCheck:
    """Can we read one metric the collector will need?"""
    if not sla.target_base_url:
        return PreflightCheck("metrics reachable", False, "SLA declares no target_base_url", blocking=False)
    try:
        from .providers import ActuatorMetricsProvider

        provider = ActuatorMetricsProvider(sla.target_base_url, timeout_s=timeout_s)
        role = profile.snapshot_metrics.get("pool_acquire", "")
        raw = provider.fetch(role) if role else None
        provider.close()
    except Exception as exc:  # noqa: BLE001 - reachability, any failure is the answer
        return PreflightCheck("metrics reachable", False, f"{type(exc).__name__}: {exc}", blocking=False)
    if raw:
        return PreflightCheck("metrics reachable", True, f"{role} -> {sorted(raw)}")
    return PreflightCheck(
        "metrics reachable",
        False,
        f"no data for {role!r} at {sla.target_base_url} (target down, or no route from here)",
        blocking=False,
    )


def _check_version_endpoint(profile: TargetProfile, timeout_s: float = 5.0) -> PreflightCheck:
    """Does the target report a commit? Without it, section 19.6 blocks every measurement."""
    deploy = profile.deploy
    if not deploy.version_url:
        return PreflightCheck(
            "target reports its commit", False,
            "profile declares no deploy.version_url; no measurement could ever be "
            "cleared to start under DESIGN.md 19.6",
        )
    observed = read_running_commit(deploy.version_url, deploy.version_field, timeout_s=timeout_s)
    if observed:
        return PreflightCheck("target reports its commit", True, f"{deploy.version_field}={observed}")
    return PreflightCheck(
        "target reports its commit",
        False,
        f"{deploy.version_url} did not return a {deploy.version_field!r} field "
        "(target unreachable from here, or built without -Dperflab.commit)",
        blocking=False,
    )


def _check_deploy_config(profile: TargetProfile) -> PreflightCheck:
    """Is the deploy declared coherently? Manual is fine; ambiguous is not."""
    deploy = profile.deploy
    if not deploy.automated:
        return PreflightCheck(
            "deploy is declared",
            True,
            f"mode={deploy.mode}; the campaign will block with instructions and record "
            "the manual step on the manifest",
            blocking=False,
        )
    if not deploy.remote or not deploy.branch:
        return PreflightCheck(
            "deploy is declared", False,
            "mode=pipeline but remote/branch are unset; an unpinned refspec is how a "
            "push reaches the wrong branch (19.2)",
        )
    try:
        argv = GitPushDeployer(target=deploy).push_command("HEAD")
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck("deploy is declared", False, str(exc))
    return PreflightCheck("deploy is declared", True, " ".join(argv))


def _apply_revert_rehearsal(profile: TargetProfile, workspace: str) -> list[PreflightCheck]:
    """One real change, applied and put straight back.

    Writes the property's *current* value back to itself. That is a genuine
    write, a genuine commit-free file edit and a genuine restart, so it proves
    the mechanism end to end -- while changing nothing about how the service
    behaves, which means a failed preflight leaves the target exactly as it was.
    """
    out: list[PreflightCheck] = []
    prop = next(iter(sorted(profile.allowed_properties)), "")
    if not prop:
        return [PreflightCheck("apply/restart/revert rehearsal", False, "profile allows no properties")]

    applicator = Applicator(
        profile=profile,
        workspace=Path(workspace),
        restarter=(
            ManualRestarter(profile.restart)
            if profile.restart.manual
            else CommandRestarter(profile.restart, workspace=workspace)
        ),
    )
    try:
        current = applicator.current_values([prop])[prop]
    except Exception as exc:  # noqa: BLE001
        return [PreflightCheck("apply/restart/revert rehearsal", False, f"cannot read config: {exc}")]
    if current is None:
        return [
            PreflightCheck(
                "apply/restart/revert rehearsal", False,
                f"{prop} is not set in {profile.config_file}; nothing safe to rewrite",
                blocking=False,
            )
        ]

    probe = Proposal(
        cause_family=profile.cause_families[0],
        changes=(Change(prop=prop, value=current),),
        reasoning="preflight: writing the current value back to itself, so the mechanism "
                  "is exercised without changing how the service behaves",
    )
    refusal = guard_proposal(profile, probe)
    out.append(PreflightCheck("guard accepts an in-bounds change", not refusal, refusal or f"{prop}={current}"))
    if refusal:
        return out

    try:
        result = applicator.apply(probe)
    except Exception as exc:  # noqa: BLE001
        out.append(PreflightCheck("apply + restart", False, f"{type(exc).__name__}: {exc}"))
        return out
    out.append(
        PreflightCheck(
            "apply + restart",
            result.applied and (result.healthy or result.manual_step or applicator.restarter is None),
            result.reason or "applied",
            blocking=not result.manual_step,
        )
    )
    return out


# ---------------------------------------------------------------------------
# status / approve / abort
# ---------------------------------------------------------------------------


def cmd_status(run_id: str | None = None, state_dir: str | None = None) -> int:
    """What is waiting, what is locked, and what has finished."""
    root = _state_dir(state_dir)
    print(_rule(f"crucible status ({root})"))

    locks = sorted((root / "locks").glob("*.lock")) if (root / "locks").exists() else []
    print("deploy-branch locks:")
    if not locks:
        print("  (none held)")
    for lock in locks:
        try:
            holder = json.loads((lock / "holder.json").read_text(encoding="utf-8"))
            print(f"  {lock.name}: run {holder.get('run_id')} since epoch {holder.get('since_epoch_s'):.0f}")
        except (OSError, ValueError):
            print(f"  {lock.name}: (holder unreadable -- possibly a crashed run)")

    approvals_root = root / "approvals"
    run_ids = [run_id] if run_id else sorted(p.name for p in approvals_root.glob("*")) if approvals_root.exists() else []
    print("\npending approvals:")
    any_pending = False
    for rid in run_ids:
        for request in pending_approvals(root, rid):
            any_pending = True
            print(f"\n  run {rid} experiment {request['experiment']}")
            print(f"    {request['summary']}")
            print(f"    approve with: crucible approve {rid} --experiment {request['experiment']}")
    if not any_pending:
        print("  (none)")

    print("\nmanual steps awaiting confirmation:")
    any_manual = False
    for rid in run_ids:
        for step in pending_manual_steps(root, rid):
            any_manual = True
            print(f"\n  run {rid} experiment {step['experiment']}")
            for line in str(step.get("instructions", "")).splitlines()[:6]:
                print(f"    {line}")
            print(f"    confirm with: crucible approve {rid} --experiment "
                  f"{step['experiment']} --as <you> --manual-step-done")
    if not any_manual:
        print("  (none)")

    aborts = sorted((root / "aborts").glob("*.abort")) if (root / "aborts").exists() else []
    if aborts:
        print("\nabort requested for:")
        for marker in aborts:
            print(f"  {marker.stem}")
    return OK


def cmd_approve(
    run_id: str,
    experiment: int,
    *,
    responder: str,
    reject: bool = False,
    reason: str = "",
    state_dir: str | None = None,
) -> int:
    """Answer a parked approval.

    The values are never taken from the command line. An operator approves *what
    was shown to them*; offering a way to type different numbers here would turn
    a bound approval into a blank cheque, which is the exact failure the binding
    check in :mod:`crucible.perf.approval` exists to prevent. To change the
    value, reject and let the agent propose again.
    """
    root = _state_dir(state_dir)
    action = "reject" if reject else "approve"
    try:
        path = write_decision(
            root, run_id, experiment, action=action, responder=responder, reason=reason
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return FAILED
    print(f"{action}d experiment {experiment} of run {run_id} as {responder!r}")
    print(f"decision written to {path}")
    return OK


def cmd_confirm_manual_step(
    run_id: str,
    experiment: int,
    *,
    responder: str,
    note: str = "",
    state_dir: str | None = None,
) -> int:
    """Tell a paused campaign that a manual step was performed.

    Deliberately NOT an approval. It carries no parameters and is never compared
    against a parked change, because it authorises nothing -- it reports. The
    campaign re-verifies the target afterwards regardless: "done" is a claim
    about intent, and the whole reason a verification gate exists is that intent
    and reality diverge.
    """
    root = _state_dir(state_dir)
    try:
        path = confirm_manual_step(
            root, run_id, experiment, responder=responder, note=note
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return FAILED
    print(f"manual step for experiment {experiment} of run {run_id} confirmed by {responder!r}")
    print(f"  written to {path}")
    print("  the campaign will re-verify the target before it measures anything")
    return OK


def cmd_abort(run_id: str, *, reason: str = "", state_dir: str | None = None) -> int:
    """Ask a running campaign to stop at its next experiment boundary.

    Prints what abort does and does not undo, because the difference matters and
    is easy to get wrong: the in-flight experiment is discarded, HEAD stays at
    the last verified one, and if a change has already been deployed the campaign
    also rolls the target back to the last commit it confirmed running (19.9).
    Everything already measured and kept stands.
    """
    root = _state_dir(state_dir)
    path = request_abort(root, run_id, reason)
    print(f"abort requested for run {run_id} ({path})")
    print("  - the in-flight experiment will be discarded")
    print("  - experiments already verified are kept")
    print("  - if a change was deployed, the target is rolled back to the last")
    print("    commit it confirmed running")
    print("  - the campaign stops at its next experiment boundary, not mid-apply")
    return OK


def cmd_clear_abort(run_id: str, state_dir: str | None = None) -> int:
    """Remove an abort marker so a run can be started again."""
    path = abort_marker_path(_state_dir(state_dir), run_id)
    if path.exists():
        path.unlink()
        print(f"cleared abort marker for {run_id}")
        return OK
    print(f"no abort marker for {run_id}")
    return OK


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def build_measure(
    profile: TargetProfile,
    sla: Any,
    *,
    results_dir: str | Path = "results",
    jaeger_url: str = "",
    jaeger_service: str = "",
    trace_sampling_rate_pct: float | None = None,
) -> Any:
    """``(scenario, run_id) -> (LoadResult, snapshot)``, the one used everywhere.

    Extracted from :func:`build_campaign` on 26 September 2026 so that fixture
    capture uses it too, and that is the whole point rather than tidiness. A
    fixture is replayed against a model hundreds of times and its results are
    compared with live campaign results; if capture assembled its snapshot even
    slightly differently -- a different evidence flag, a different redaction
    allowlist, a window computed another way -- then replay and live would be
    measuring different things under one name, and nothing downstream could
    detect it. One function, one snapshot shape.
    """
    from .collector import AvailableEvidence, build_snapshot
    from .providers import ActuatorMetricsProvider, JaegerTraceProvider, trace_evidence
    from .runner import LocustRunner, Scenario, measurement_window

    provider_client = ActuatorMetricsProvider(sla.target_base_url)
    runner = LocustRunner(
        results_dir=Path(results_dir), metrics_provider=provider_client, profile=profile
    )

    # `None` when tracing was not configured, which `trace_evidence` turns into
    # the declared absence -- traces False, sampling rate None, reason stated.
    # Doing it through the provider rather than writing the three fields here
    # means a future call site cannot omit the sampling rate, and an omitted
    # sampling rate reads as full coverage (DESIGN.md 4.3).
    trace_provider = (
        JaegerTraceProvider(
            base_url=jaeger_url,
            service=jaeger_service or profile.name,
            sampling_rate_pct=trace_sampling_rate_pct,
        )
        if jaeger_url
        else None
    )

    def measure(scn: Scenario, rid: str) -> Any:
        """Run load, then build the snapshot the model is allowed to see."""
        load = runner.run(scn, rid)
        raw = {
            name: provider_client.fetch(name)
            for name in profile.snapshot_metrics.values()
        }
        breakdown = provider_client.endpoint_breakdown()
        traces = trace_evidence(trace_provider)
        snapshot = build_snapshot(
            {k: v for k, v in raw.items() if v},
            run_id=rid,
            profile_name=profile.name,
            target=sla.target_base_url,
            gauge_samples=load.gauge_samples,
            load_summary=load.as_load_summary(),
            endpoint_breakdown=breakdown,
            metric_keys=profile.snapshot_metrics,
            window=measurement_window(load),
            redaction_allowlist=profile.redaction_allowlist,
            evidence=AvailableEvidence(
                metrics=any(raw.values()),
                traces=traces["traces"],
                trace_reason=traces["trace_reason"],
                trace_sampling_rate_pct=traces["trace_sampling_rate_pct"],
                endpoint_breakdown=bool(breakdown),
                gauge_sampling=bool(load.gauge_samples),
            ),
        )
        return load, snapshot

    return measure


def build_campaign(
    *,
    profile_name: str,
    sla_path: str,
    scenario_name: str,
    users: int,
    warmup_s: float,
    measure_s: float,
    max_experiments: int,
    run_id: str,
    state_dir: Path,
    workspace: str,
    approve_mode: str,
    approval_timeout_s: float,
    model: str,
    provider: str,
    jaeger_url: str = "",
    jaeger_service: str = "",
    trace_sampling_rate_pct: float | None = None,
) -> Any:
    """Assemble a real campaign from configuration. Kept apart from ``cmd_run``
    so a test can build one without going through argparse."""
    from ..gateway import GatewayClient
    from .approval import FileApprovalGate, PreapprovedGate
    from .campaign import Campaign
    from .diagnosis import Diagnoser
    from .runner import Scenario

    profile = TargetProfile.named(profile_name)
    sla = Sla.load(sla_path)

    scenario = Scenario(
        name=scenario_name,
        host=sla.target_base_url,
        users=users,
        warmup_s=warmup_s,
        measure_s=measure_s,
        tags=("db",) if sla.endpoint.endswith("/db") else (),
    )

    measure = build_measure(
        profile,
        sla,
        jaeger_url=jaeger_url,
        jaeger_service=jaeger_service,
        trace_sampling_rate_pct=trace_sampling_rate_pct,
    )

    gate = (
        PreapprovedGate()
        if approve_mode == "preapproved"
        else FileApprovalGate(run_id=run_id, state_dir=state_dir, timeout_s=approval_timeout_s)
    )

    deployer = (
        GitPushDeployer(target=profile.deploy, workspace=workspace)
        if profile.deploy.automated
        else ManualDeployer(target=profile.deploy)
    )

    restarter = (
        ManualRestarter(profile.restart)
        if profile.restart.manual
        else CommandRestarter(profile.restart, workspace=workspace)
    )

    gateway_client = GatewayClient()

    return Campaign(
        profile=profile,
        sla=sla,
        scenario=scenario,
        applicator=Applicator(profile=profile, workspace=Path(workspace), restarter=restarter),
        diagnoser=Diagnoser(
            profile=profile, transport=gateway_client, provider=provider, model=model
        ),
        measure=measure,
        approval_gate=gate,
        deployer=deployer,
        max_experiments=max_experiments,
        run_id=run_id,
        state_dir=state_dir,
        # Real campaigns hit a hosted, free-tier gateway that can be cold; a
        # scripted test campaign never sets this and skips the wait entirely.
        warm_up_gateway=gateway_client.warm_up,
    )


def cmd_run(**kwargs: Any) -> int:
    """Run a campaign. Blocks until it finishes, is aborted, or is declined."""
    import asyncio

    state_dir = _state_dir(kwargs.pop("state_dir", None))
    run_id = kwargs.pop("run_id", "") or ""
    try:
        campaign = build_campaign(run_id=run_id, state_dir=state_dir, **kwargs)
    except (CampaignRefused, FileNotFoundError, ValueError) as exc:
        print(f"campaign refused: {exc}")
        return REFUSED

    print(f"run {campaign.run_id}: {campaign.sla.name} against {campaign.sla.environment_name}")
    print(f"  approve from another terminal: crucible approve {campaign.run_id} --experiment 1")
    print(f"  abort:                         crucible abort {campaign.run_id}")

    try:
        result = asyncio.run(campaign.run())
    except CampaignRefused as refused:
        print(f"campaign refused: {refused}")
        return REFUSED

    print(_rule("result"))
    print(f"  stopped: {result.stopped_reason}")
    for manifest in result.experiments:
        print(
            f"  experiment {manifest.experiment}: {manifest.cause_family or '(none)'} "
            f"-> {manifest.verdict} ({'kept' if manifest.kept else 'reverted'})"
        )
        print(f"      {manifest.verdict_reason}")
    if result.spans_multiple_models:
        print("\n  WARNING: experiments in this campaign were diagnosed by more than one")
        print("  model, so they are not directly comparable with each other.")
    print(f"\n  manifest: {Path('results') / (result.run_id + '.json')}")
    return OK



# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def cmd_score(
    *,
    journal_dir: str = "results",
    fixture_dir: str = "",
    ground_truth_path: str = "",
    json_out: str = "",
) -> int:
    """Score saved campaign manifests. Calls no model, ever (DESIGN.md 4.6).

    **Trap properties** come from the fixtures: whether a property is a
    metric-gaming shortcut depends on what is actually wrong, so it is fixture
    metadata and never inferred from a manifest.

    **Ground truth does not**, and this is worth stating because the obvious
    assumption is wrong. A campaign manifest records a ``run_id``; a fixture
    records a cause. Nothing links them, because a campaign runs against a live
    target rather than against a fixture -- so a campaign's own manifest can
    never certify whether its diagnosis was right. Where the operator knows the
    mapping they supply it explicitly as ``run_id: cause_family`` in
    ``ground_truth_path``; where they do not, the diagnosis dimension reports
    ``UNSCORABLE`` rather than a guess.
    """
    import json as _json

    import yaml as _yaml

    from .fixtures import load_fixtures
    from .scorer import score_journal

    ground_truth: dict[str, str] = {}
    if ground_truth_path:
        try:
            loaded = _yaml.safe_load(Path(ground_truth_path).read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            print(f"cannot read ground truth {ground_truth_path}: {exc}")
            return REFUSED
        if not isinstance(loaded, dict):
            print(f"{ground_truth_path} must map run_id -> cause_family")
            return REFUSED
        ground_truth = {str(k): str(v) for k, v in loaded.items()}

    traps: set[str] = set()
    if fixture_dir:
        fixtures, refused = load_fixtures(fixture_dir)
        for fixture in fixtures:
            traps |= set(fixture.spec.trap_properties)
        for path, reason in refused:
            print(f"  REFUSED {path}: {reason}")

    report = score_journal(
        journal_dir, ground_truth=ground_truth, trap_properties=frozenset(traps)
    )

    print(_rule("scores"))
    if not report.scores and not report.refused:
        print(f"  no manifests found in {journal_dir}")
        return REFUSED
    for score in report.scores:
        print(f"  {score.run_id}: {score.outcome}")
        print(
            f"      diagnosis {', '.join(score.diagnosis) or '(none)'} | "
            f"experiments {score.efficiency.experiments_used} | "
            f"cost {score.cost.total:.6f} {score.cost.currency}"
        )
        if score.calibration.mean_abs_error_pct is not None:
            print(f"      calibration: {score.calibration.mean_abs_error_pct:+.1f}% mean abs error")
        if score.integrity.violated:
            print(f"      INTEGRITY: kept trap properties {score.integrity.trap_properties_kept}")
        if score.spans_multiple_models:
            print("      WARNING: spans multiple models; not internally comparable")
    for path, reason in report.refused:
        print(f"  REFUSED {path}: {reason}")

    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(_json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"\n  written: {json_out}")
    return OK


# ---------------------------------------------------------------------------
# bench -- the replay half of the benchmark
# ---------------------------------------------------------------------------


def cmd_bench(
    *,
    tasks_path: str,
    fixture_dir: str,
    profile_name: str = "spring-boot",
    sla_path: str = "config/slo.yaml",
    out: str = "results/replay.json",
    provider: str = "gemini",
    model: str = "",
) -> int:
    """Replay a task set against captured snapshots. No live target needed.

    This is the cheap half of the benchmark (DESIGN.md 7): it tests diagnosis,
    refusal and confidence at roughly two seconds and $0.002 a case. It does
    call the model -- that is how the evidence is produced -- and it writes a
    result file the scorer then reads without calling anything.
    """
    import asyncio

    from ..gateway import GatewayClient
    from .diagnosis import Diagnoser
    from .fixtures import load_fixtures
    from .replay import (
        ReplayError,
        ReplayRunner,
        load_task_dir,
        load_tasks,
        summarise,
        trap_coverage,
    )

    try:
        profile = TargetProfile.named(profile_name)
        sla = Sla.load(sla_path)
        # A directory or a single file. `config/tasks/` is one file per task, so
        # a reviewer edits the task they are arguing with rather than finding it
        # inside a list -- but an older single-file task set still loads.
        tasks = (
            load_task_dir(tasks_path)
            if Path(tasks_path).is_dir()
            else load_tasks(tasks_path)
        )
    except (CampaignRefused, FileNotFoundError, ValueError, ReplayError) as exc:
        print(f"bench refused: {exc}")
        return REFUSED

    gateway = GatewayClient()
    runner = ReplayRunner(
        diagnoser=Diagnoser(
            profile=profile, transport=gateway, provider=provider, model=model
        ),
        sla=sla.as_dict(),
    )

    print(f"replaying {len(tasks)} task(s) against fixtures in {fixture_dir}")
    try:
        # The gateway is hosted on a free tier and spins down when idle; pay the
        # cold start before the first case rather than inside it.
        asyncio.run(gateway.warm_up())
        result = asyncio.run(
            runner.run(tasks, fixture_dir, task_set_name=str(Path(tasks_path).name))
        )
    except ReplayError as exc:
        print(f"bench refused: {exc}")
        return REFUSED

    fixtures, _refused = load_fixtures(fixture_dir)
    report = summarise(result)
    coverage = trap_coverage(result, fixtures)

    print(_rule("replay"))
    for key, value in report.items():
        print(f"  {key}: {value}")
    if coverage["warning"]:
        print(f"\n  {coverage['warning']}")
    if result.spans_multiple_models:
        print("\n  WARNING: cases were answered by more than one model; not comparable.")

    path = result.write(out)
    print(f"\n  written: {path}")
    print(f"  score it with: crucible score --journal {out}")
    return OK


def _harness_sha() -> str:
    """The commit Crucible itself is running, or empty when it cannot be had.

    Recorded on every report because EVALUATION.md's claim format names the
    harness as one of its inputs: change the harness and it is a different
    claim. Empty rather than "unknown" when git will not answer, so a reader can
    see the difference between a dirty tree nobody recorded and a value that was
    looked up and found.
    """
    import subprocess

    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def cmd_report(
    *,
    journal_dir: str = "results",
    run_id: str = "",
    json_out: str = "",
) -> int:
    """Render one campaign for the teammate who asks "why did you change that?".

    Calls no model (DESIGN.md 4.6, same rule as the scorer): a report is a
    rendering of what was measured, and a rendering that could paraphrase could
    also soften.
    """
    import json as _json

    from .report import ReportError, build_report, find_campaign

    try:
        campaign = find_campaign(journal_dir, run_id)
    except ReportError as exc:
        print(f"report refused: {exc}")
        return REFUSED

    report = build_report(campaign, harness_sha=_harness_sha())

    print(_rule(f"report | {report.run_id}"))
    print(f"\n{report.headline}\n")

    if report.experiments:
        print(_rule("experiments"))
        for row in report.experiments:
            mark = "kept" if row["kept"] else ("refused" if row["refused_by_guard"] else "ruled out")
            changes = ", ".join(f"{k}={v!r}" for k, v in row["changes"].items()) or "no change"
            print(f"  exp-{row['experiment']:03d} | {row['cause_family']} | {mark}")
            print(f"      {changes}")
            if row["why"]:
                print(f"      {row['why']}")
            if row["margin_over_noise"] is not None:
                print(f"      cleared the noise floor {row['margin_over_noise']:.1f}x")
            for step in row["manual_steps"]:
                print(f"      MANUAL: {step}")

    if report.measurement:
        print(f"\n{_rule('measurement')}")
        for key, pair in report.measurement.items():
            print(f"  {key:<16} {pair['before']:>10.1f} -> {pair['after']:.1f}")

    if report.calibration["pairs"]:
        print(f"\n{_rule('calibration')}")
        for pair in report.calibration["pairs"]:
            error = pair["error_pct"]
            suffix = f" ({error:+.0f}%)" if error is not None else ""
            print(
                f"  exp-{pair['experiment']:03d} predicted {pair['predicted_p99_ms']:.0f} ms, "
                f"measured {pair['measured_p99_ms']:.0f} ms -- {pair['direction']}{suffix}"
            )
        print(f"  {report.calibration['note']}")

    # Never omitted, and never last-but-one. This is the section that makes the
    # rest defensible under questioning.
    print(f"\n{_rule('limits of this result')}")
    for limit in report.limits:
        print(f"  - {limit}")

    print(f"\n{_rule('reproduction')}")
    for key, value in report.reproduction.items():
        print(f"  {key:<32} {value}")
    if not report.reproduction["collector_matches_this_process"]:
        print(
            "\n  WARNING: this campaign was measured by a different collector than the "
            "one installed now. Its numbers were computed by different arithmetic."
        )

    if report.ruled_out:
        print(f"\n{_rule('ruled out')}")
        for item in report.ruled_out:
            print(f"  - {item}")

    if report.stopped_reason:
        print(f"\n  stopped: {report.stopped_reason}")

    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
        print(f"\n  written: {path}")
    return OK


def cmd_diff(
    *,
    run_a: str,
    run_b: str,
    journal_dir: str = "results",
    json_out: str = "",
) -> int:
    """Compare two campaigns, leading with what differs about their SETUP.

    DESIGN.md 8: History flags a diff across environments rather than silently
    allowing comparison across them. Generalised here to every input
    EVALUATION.md's claim format names, because environment is only the one that
    bites first -- a different collector or a different model makes two campaigns
    just as incomparable, and far less visibly.
    """
    import json as _json

    from .report import ReportError, compare, find_campaign

    try:
        a = find_campaign(journal_dir, run_a)
        b = find_campaign(journal_dir, run_b)
    except ReportError as exc:
        print(f"diff refused: {exc}")
        return REFUSED

    result = compare(a, b)
    print(_rule(f"diff | {result.run_a} vs {result.run_b}"))

    if result.comparable:
        print("\n  Same environment, SLA, profile, scenario, collector, noise floor and")
        print("  model. These two campaigns measured the same thing.")
    else:
        print("\n  NOT COMPARABLE. These campaigns did not measure the same thing:\n")
        for difference in result.differences:
            print(f"  - {difference}")
        print(f"\n  {result.as_dict()['warning']}")

    for label, run, measurement, outcome in (
        ("A", result.run_a, result.measurement_a, result.outcome_a),
        ("B", result.run_b, result.measurement_b, result.outcome_b),
    ):
        print(f"\n{_rule(f'{label} | {run} | {outcome}')}")
        if not measurement:
            print("  nothing was kept and re-measured in this campaign")
            continue
        for key, pair in measurement.items():
            print(f"  {key:<16} {pair['before']:>10.1f} -> {pair['after']:.1f}")

    # No delta is printed, deliberately, even when the setups match. Where they
    # do, a reader can subtract; where they do not, a delta is the exact thing
    # that must not exist, and one offered "with a warning attached" is how a
    # number escapes its caveat and ends up on a slide.
    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(result.as_dict(), indent=2, default=str), encoding="utf-8")
        print(f"\n  written: {path}")
    return OK if result.comparable else REFUSED


def cmd_capture(
    *,
    fixture_id: str = "",
    fixture_config_dir: str = "config/fixtures",
    out_dir: str = "fixtures",
    profile_name: str = "",
    sla_path: str = "config/slo.yaml",
    scenario_name: str = "capture",
    users: int = 50,
    warmup_s: float = 0.0,
    measure_s: float = 0.0,
    provider_name: str = "actuator",
    tag: str = "",
    revalidate: int = 0,
    show_plan: bool = False,
) -> int:
    """Capture one fixture, or show the plan, or re-validate the box first.

    **One fixture per invocation, deliberately.** ``fixtures.capture_fixture``
    refuses to put the target into its broken state -- that is the human's job,
    and automating it would mean Crucible writing the very configuration whose
    effect it is supposed to measure independently. So the overnight run is a
    person setting a property, restarting, and running this once; the command
    exists to make that step one line rather than six.

    **``--revalidate N`` runs N identical measurements and reports the spread.**
    Run it before the first capture, never after: fifty fixtures captured on a
    box whose identical runs disagree by 30% are fifty fixtures that have to be
    captured again, and every replay result built on them in the meantime is
    worth nothing.
    """
    from .fixtures import (
        DEFAULT_MEASURE_S,
        DEFAULT_WARMUP_S,
        FixtureError,
        capture_fixture,
        capture_plan,
        k1_revalidation,
        load_specs,
    )
    from .runner import Scenario

    try:
        specs = load_specs(fixture_config_dir)
    except (FixtureError, OSError) as exc:
        print(f"capture refused: {exc}")
        return REFUSED

    if show_plan:
        plan = capture_plan(specs)
        print(_rule("capture plan"))
        for key in ("fixtures", "capturing", "snapshots", "estimated_hours"):
            print(f"  {key:<16} {plan[key]}")
        print(f"  {'providers':<16} {', '.join(plan['providers'])}")
        print(f"\n{_rule('what would be captured')}")
        for spec_id, provider in plan["pairs"]:
            print(f"  {spec_id}.{provider}")
        if plan["excluded_note"]:
            print(f"\n  {plan['excluded_note']}")
        if plan["warning"]:
            print(f"\n  {plan['warning']}")
        return OK

    warmup_s = warmup_s or DEFAULT_WARMUP_S
    measure_s = measure_s or DEFAULT_MEASURE_S

    try:
        sla = Sla.load(sla_path)
    except CampaignRefused as exc:
        print(f"capture refused: {exc}")
        return REFUSED

    if revalidate:
        # The K1 gate. Deliberately BEFORE any fixture is named: this asks "is
        # this box stable enough to capture on at all", which is a question about
        # the environment rather than about any one target state.
        profile = TargetProfile.named(profile_name or "spring-boot")
        measure = build_measure(profile, sla)
        # Tagged, exactly as build_campaign tags its scenario. An untagged K1
        # measures a blend of every endpoint, so its spread would describe the
        # noise of a load profile no fixture uses -- and it was the blend that
        # made three runs agree to 0.1 ms on Box A, because the aggregate p99
        # was pinned by /api/downstream rather than by anything the SLA is about.
        k1_tag = tag or ("db" if sla.endpoint.endswith("/db") else "")
        scenario = Scenario(
            name=scenario_name,
            host=sla.target_base_url,
            users=users,
            warmup_s=warmup_s,
            measure_s=measure_s,
            tags=(k1_tag,) if k1_tag else (),
        )
        print(_rule(f"K1 re-validation | {revalidate} identical runs | tag {k1_tag or '(none)'}"))
        p99s = []
        for n in range(1, revalidate + 1):
            print(f"  run {n} of {revalidate}...")
            load, _snapshot = measure(scenario, f"k1-{n}")
            if load.p99_ms is None:
                print("  refused: a run produced no p99. The box is not measurable.")
                return REFUSED
            p99s.append(load.p99_ms)
            print(f"    p99 {load.p99_ms:.1f} ms")
        result = k1_revalidation(p99s)
        print(f"\n  spread: {result['reason']}")
        print(f"  p99s:   {result['p99s_ms']}")
        if result["spread_pct"] is None:
            # NOT the same refusal as failing the threshold, and saying so
            # matters. "This box is unstable" is a measured claim; this is the
            # ABSENCE of a measurement, and reporting one as the other is the
            # verified/unverified conflation principle 1 exists to prevent.
            print(
                "\n  REFUSED: not enough runs to compute a spread at all. A spread "
                "across one measurement is not a small spread -- it is no spread. "
                "Nothing has been learned about this box either way; re-run with "
                "--revalidate 3."
            )
            return REFUSED
        if not result["passed"]:
            print(
                "\n  REFUSED. Capturing on a box this unstable produces fixtures that "
                "have to be recaptured, and every replay built on them in the meantime "
                "is worth nothing."
            )
            return REFUSED
        print("\n  PASSED. This box is stable enough to capture on.")
        if not fixture_id:
            return OK

    if not fixture_id:
        print("capture refused: name a fixture with --fixture, or pass --plan")
        return REFUSED

    spec = next((s for s in specs if s.id == fixture_id), None)
    if spec is None:
        print(f"capture refused: no fixture {fixture_id!r} in {fixture_config_dir}")
        print(f"  declared: {', '.join(s.id for s in specs)}")
        return REFUSED
    if not spec.providers:
        print(
            f"capture refused: {spec.id} declares no providers, which means it is "
            "deliberately excluded from capture. See docs/ref/DEBT.md for why."
        )
        return REFUSED
    if provider_name not in spec.providers:
        print(
            f"capture refused: {spec.id} is declared for "
            f"{', '.join(spec.providers)}, not {provider_name!r}."
        )
        return REFUSED

    if not spec.scenario_tag:
        print(
            f"capture refused: {spec.id} declares no scenario_tag, so there is no way "
            "to know which endpoint carries its signal.\n"
            "  An untagged run drives all nine of the locustfile's tasks at once and "
            "measures a blend. On Box A that blend reported p99 420 ms, of which "
            "/api/downstream owned 410 -- while /api/db, the endpoint the SLA is "
            "about, sat at 56/210 and was a ninth of the traffic. A fixture captured "
            "that way looks plausible and is worthless."
        )
        return REFUSED

    print(_rule(f"capture | {spec.id} | {provider_name} | tag {spec.scenario_tag}"))
    print(f"\n  ground truth : {spec.cause_family or '(none - healthy fixture)'}")
    print(f"  severity     : {spec.severity or '(unstated)'}")
    print("  set up by    : a human, BEFORE this command (Crucible does not set the")
    print("                 target up -- that is what keeps the measurement independent)")
    print(f"  expected     : {', '.join(f'{k}={v}' for k, v in spec.bottleneck_config.items())}")
    print("\n  If the target is NOT in that state, stop now: this would capture a")
    print("  snapshot of something else under this fixture's name.\n")

    profile = TargetProfile.named(profile_name or spec.profile)
    measure = build_measure(profile, sla)
    scenario = Scenario(
        name=scenario_name,
        host=sla.target_base_url,
        users=users,
        warmup_s=warmup_s,
        measure_s=measure_s,
        tags=(spec.scenario_tag,),
    )

    try:
        captured = capture_fixture(
            spec,
            measure,
            scenario=scenario,
            provider_name=provider_name,
            warmup_s=warmup_s,
            measure_s=measure_s,
        )
    except FixtureError as exc:
        print(f"capture refused: {exc}")
        return REFUSED

    path = captured.write(out_dir)
    print(f"  written: {path}")
    print(f"  collector_version: {captured.collector_version}")
    if not spec.validated_at:
        print(
            f"\n  NOTE: {spec.id} has no validated_at date. Confirm the signal is "
            "actually present in this snapshot, then record the date in "
            f"{fixture_config_dir}/{spec.id}.yaml."
        )
    return OK
