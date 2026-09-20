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
) -> Any:
    """Assemble a real campaign from configuration. Kept apart from ``cmd_run``
    so a test can build one without going through argparse."""
    from ..gateway import GatewayClient
    from .approval import FileApprovalGate, PreapprovedGate
    from .campaign import Campaign
    from .collector import AvailableEvidence, build_snapshot
    from .diagnosis import Diagnoser
    from .providers import ActuatorMetricsProvider
    from .runner import LocustRunner, Scenario, measurement_window

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

    provider_client = ActuatorMetricsProvider(sla.target_base_url)
    runner = LocustRunner(
        results_dir=Path("results"), metrics_provider=provider_client, profile=profile
    )

    def measure(scn: Scenario, rid: str) -> Any:
        """Run load, then build the snapshot the model is allowed to see."""
        load = runner.run(scn, rid)
        raw = {
            name: provider_client.fetch(name)
            for name in profile.snapshot_metrics.values()
        }
        breakdown = provider_client.endpoint_breakdown()
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
                traces=False,
                trace_reason="no trace provider configured for this campaign",
                endpoint_breakdown=bool(breakdown),
                gauge_sampling=bool(load.gauge_samples),
            ),
        )
        return load, snapshot

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

    return Campaign(
        profile=profile,
        sla=sla,
        scenario=scenario,
        applicator=Applicator(profile=profile, workspace=Path(workspace), restarter=restarter),
        diagnoser=Diagnoser(
            profile=profile, transport=GatewayClient(), provider=provider, model=model
        ),
        measure=measure,
        approval_gate=gate,
        deployer=deployer,
        max_experiments=max_experiments,
        run_id=run_id,
        state_dir=state_dir,
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

