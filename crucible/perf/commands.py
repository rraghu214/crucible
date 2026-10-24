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
import time
from dataclasses import dataclass, replace
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
    mix_from,
    pause_marker_path,
    paused_state_path,
    request_abort,
    request_pause,
)
from .deploy import GitPushDeployer, ManualDeployer, read_running_commit
from .hooks import hooks_from_mapping
from .loadprofile import tag_for_endpoint
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


def cmd_plan(
    profile_name: str = "spring-boot",
    sla_path: str = "config/slo.yaml",
    workspace: str | None = None,
) -> int:
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

    print(_rule("workspace"))
    if workspace is None:
        print("  (not supplied — pass --workspace <path> to verify config_file exists)")
    else:
        config_path = Path(workspace) / profile.config_file
        if config_path.exists():
            print(f"  workspace   : {workspace}")
            print(f"  config_file : {profile.config_file}  ✓ found")
        else:
            print(f"  workspace   : {workspace}")
            print(f"  config_file : {profile.config_file}  MISSING — run will fail on first apply")

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

    checks.extend(
        preflight_checks(
            profile, sla, sla_path,
            workspace=workspace, apply_probe=apply_probe, probe_timeout_s=probe_timeout_s,
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


def preflight_checks(
    profile: TargetProfile,
    sla: Sla,
    sla_path: str,
    *,
    workspace: str = ".",
    apply_probe: bool = False,
    probe_timeout_s: float = 5.0,
) -> list[PreflightCheck]:
    """Every check after the profile and SLA have loaded, as data.

    Split out of :func:`cmd_preflight` so the Preflight screen shows the same
    checks the CLI prints rather than a second list that could drift from it.
    """
    checks: list[PreflightCheck] = []
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

    # Blocking: a campaign on an endpoint the load profile cannot drive would
    # measure something else, so preflight fails and the run refuses to start.
    try:
        checks.append(PreflightCheck("load profile drives the SLA endpoint", True,
                                     f"{sla.endpoint} -> tag {tag_for_endpoint(sla.endpoint)}"))
    except ValueError as exc:
        checks.append(PreflightCheck("load profile drives the SLA endpoint", False, str(exc)))

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
    return checks


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
            None
            if profile.deploy.automated
            else ManualRestarter(profile.restart)
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
    if paused_state_path(root, run_id).exists():
        return _abort_paused(root, run_id, reason)
    series_path = _series_path(root, run_id)
    if series_path.exists():
        plan = json.loads(series_path.read_text(encoding="utf-8"))
        if str(plan.get("status", "")).startswith("paused"):
            plan["status"] = f"aborted while paused: {reason or 'operator abort'}"
            _save_series(root, plan)
            print(f"series {run_id}: {plan['status']}")
            return OK
    path = request_abort(root, run_id, reason)
    print(f"abort requested for run {run_id} ({path})")
    print("  - the in-flight experiment will be discarded")
    print("  - experiments already verified are kept")
    print("  - if a change was deployed, the target is rolled back to the last")
    print("    commit it confirmed running")
    print("  - the campaign stops at its next experiment boundary, not mid-apply")
    return OK


def _abort_paused(root: Path, run_id: str, reason: str, results_dir: str | Path = "results") -> int:
    """Abort a PAUSED campaign: finish its manifest so the final report can be made.

    A paused campaign is not running, so an abort marker would never be read and
    the run would stay "paused" forever. Nothing to roll back: a pause happens
    at an experiment boundary, with the target on the last verified change.
    """
    reason = reason or "operator abort while paused"
    manifest_path = Path(results_dir) / f"{run_id}.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        why = manifest.get("pause_reason") or "paused"
        manifest.update(paused=False, aborted=True, abort_reason=reason, finished_at_epoch_s=time.time(),
                        stopped_reason=f"aborted while paused ({why}): {reason}")
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    paused_state_path(root, run_id).unlink(missing_ok=True)
    pause_marker_path(root, run_id).unlink(missing_ok=True)
    plan = _series_for_run(root, run_id)
    if plan is not None:
        scenario = next(n for n, r in plan["runs"].items() if r == run_id)
        plan["status"] = f"aborted in {scenario}: {reason}"
        _save_series(root, plan)
    print(f"run {run_id} was paused; it is now finished as aborted ({reason}).")
    print(f"  final report: crucible report --run {run_id}")
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


def cmd_pause(run_id: str, *, reason: str = "", state_dir: str | None = None) -> int:
    """Ask a running campaign to pause at its next experiment boundary.

    Unlike abort, pause preserves all verified experiments and leaves the
    campaign resumable. Only the measurement window in-flight at the moment of
    pause is discarded; the campaign continues from the next experiment boundary
    when ``crucible resume`` is called (DESIGN.md section 7).
    """
    root = _state_dir(state_dir)
    path = request_pause(root, run_id, reason)
    print(f"pause requested for run {run_id} ({path})")
    print("  - the in-flight experiment will be discarded and re-run on resume")
    print("  - all experiments already verified are kept")
    print("  - the campaign stops at its next experiment boundary, not mid-apply")
    print(f"  - resume with: crucible resume {run_id}")
    return OK


def cmd_clear_pause(run_id: str, state_dir: str | None = None) -> int:
    """Remove a pause marker without resuming the campaign."""
    root = _state_dir(state_dir)
    pm = pause_marker_path(root, run_id)
    ps = paused_state_path(root, run_id)
    removed = []
    for p in (pm, ps):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    if removed:
        print(f"cleared pause state for {run_id}: {', '.join(removed)}")
        return OK
    print(f"no pause state for {run_id}")
    return OK


def cmd_resume(run_id: str, state_dir: str | None = None, **kwargs: Any) -> int:
    """Resume a paused campaign from where it stopped (DESIGN.md section 7)."""
    import asyncio

    root = _state_dir(state_dir)
    ps = paused_state_path(root, run_id)
    new_caps = {k: kwargs[k] for k in ("cost_cap_usd", "wall_clock_cap_h") if kwargs.get(k) is not None}
    for key in ("cost_cap_usd", "wall_clock_cap_h"):
        kwargs.pop(key, None)
    series_path = _series_path(root, run_id)
    if not ps.exists() and series_path.exists():
        # A series paused BETWEEN scenarios (a cap ran out): resume it by its id.
        plan = json.loads(series_path.read_text(encoding="utf-8"))
        if not str(plan.get("status", "")).startswith("paused"):
            print(f"series {run_id!r} is {plan.get('status')!r}; nothing to resume")
            return REFUSED
        plan["caps"] = {**(plan.get("caps") or {}), **new_caps}
        print(f"resuming series {run_id} with caps {plan['caps']}")
        return _continue_series(root, plan)
    if not ps.exists():
        print(f"no paused state for {run_id!r} — expected {ps}")
        return REFUSED

    plan = _series_for_run(root, run_id)
    try:
        if plan is not None:
            # One scenario of a series: rebuild it on its declared load, not the
            # resume command's defaults.
            name = next(n for n, r in plan["runs"].items() if r == run_id)
            names = list(plan["runs"])
            if new_caps:
                plan["caps"] = {**(plan.get("caps") or {}), **new_caps}
            campaign = _scenario_campaign(root, plan, name, names.index(name) + 1, len(names))
        else:
            campaign = build_campaign(run_id=run_id, state_dir=str(root), **kwargs, **new_caps)
    except (CampaignRefused, FileNotFoundError, ValueError, _SeriesCapReached) as exc:
        print(f"campaign refused: {exc}")
        return REFUSED

    print(f"resuming run {campaign.run_id}: {campaign.sla.name}")
    print(f"  abort:  crucible abort {campaign.run_id}")
    print(f"  pause:  crucible pause {campaign.run_id}")

    try:
        result = asyncio.run(campaign.resume())
    except CampaignRefused as refused:
        print(f"campaign refused: {refused}")
        return REFUSED

    print(f"\nresume finished: {result.stopped_reason or 'complete'}")
    if plan is not None and not result.paused and not result.aborted:
        _record_scenario(plan, campaign.declared_scenario, result)
        plan["done"].append(campaign.declared_scenario)
        if result.cap_hit:
            plan["status"] = f"stopped: {result.stopped_reason}"
            _save_series(root, plan)
            return OK
        print(f"continuing series {plan['id']}")
        return _continue_series(root, plan)
    return OK


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


#: The metrics providers :func:`build_measure` / :func:`build_multi_measure`
#: can actually read through. Lives beside the functions so whoever adds a
#: provider sees the list they must extend.
MEASURABLE_PROVIDERS: tuple[str, ...] = ("actuator", "promql", "datadog")


def build_measure(
    profile: TargetProfile,
    sla: Any,
    *,
    results_dir: str | Path = "results",
    jaeger_url: str = "",
    jaeger_service: str = "",
    trace_sampling_rate_pct: float | None = None,
    state_dir: Path | None = None,
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

    ``state_dir`` wires the watchdog: a watchdog is created per measured window
    (§6) and supervises the seven tripwires during the run. Without it the run
    still works -- the watchdog is simply absent, ``manifest.watchdog`` stays
    ``None``, and CPU steal is never recorded (a real limitation for campaigns
    running on shared free-tier infrastructure).
    """
    from .collector import AvailableEvidence, build_snapshot
    from .providers import ActuatorMetricsProvider, JaegerTraceProvider, trace_evidence
    from .runner import LocustRunner, Scenario, measurement_window
    from .watchdog import for_scenario

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
        # Create a fresh watchdog per window so run_id and scenario_name are
        # correct (§6: seven tripwires, including CPU steal on every manifest).
        if state_dir is not None:
            runner.watchdog = for_scenario(scn, sla, rid, state_dir=state_dir)
        load = runner.run(scn, rid)
        raw = {
            name: provider_client.fetch(name)
            for name in profile.snapshot_metrics.values()
        }
        breakdown = provider_client.endpoint_breakdown()
        runtime_config = provider_client.fetch_all_env()
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
            runtime_config=runtime_config,
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


def build_multi_measure(
    profile: TargetProfile,
    sla: Any,
    *,
    results_dir: str | Path = "results",
    jaeger_url: str = "",
    jaeger_service: str = "",
    trace_sampling_rate_pct: float | None = None,
    promql_url: str = "",
    promql_selector: dict[str, str] | None = None,
    datadog_base_url: str = "",
    datadog_api_key: str = "",
    datadog_application_key: str = "",
    datadog_scope: dict[str, str] | None = None,
) -> Any:
    """``(scenario, run_id) -> (LoadResult, dict[provider_name, snapshot])``.

    Runs ONE load and queries every configured provider from the same window,
    so the provider-agreement comparison in the benchmark uses identical load
    and timing rather than three independent runs. The Actuator provider also
    drives gauge sampling during the measured window; PromQL and Datadog read
    after the run from their stored series.

    Only providers whose URL (and credentials for Datadog) are supplied are
    included in the returned dict. "actuator" is always present. "promql" and
    "datadog" appear when their URLs are non-empty. A provider that errors at
    construction is reported in the notes of the Actuator snapshot and omitted
    from the dict rather than aborting the run -- the Actuator snapshot is the
    primary, and a missing secondary is declared, not a campaign stopper.

    The campaign always calls the simpler :func:`build_measure`; this function
    exists for fixture capture, where one run per fixture state is the affordability
    constraint (DESIGN.md section 7 / plan B1).
    """
    from .collector import AvailableEvidence, build_snapshot
    from .providers import ActuatorMetricsProvider, JaegerTraceProvider, trace_evidence
    from .runner import LocustRunner, Scenario, measurement_window

    actuator = ActuatorMetricsProvider(sla.target_base_url)
    runner = LocustRunner(
        results_dir=Path(results_dir), metrics_provider=actuator, profile=profile
    )
    trace_provider = (
        JaegerTraceProvider(
            base_url=jaeger_url,
            service=jaeger_service or profile.name,
            sampling_rate_pct=trace_sampling_rate_pct,
        )
        if jaeger_url
        else None
    )

    # Build optional secondary providers. Construction errors are caught so a
    # misconfigured Prometheus URL does not abort a capture that is primarily
    # about Actuator numbers.
    secondary: dict[str, Any] = {}
    secondary_notes: list[str] = []
    if promql_url:
        try:
            from .providers.promql import PromQLMetricsProvider  # noqa: PLC0415

            secondary["promql"] = PromQLMetricsProvider.from_profile(
                promql_url, profile, selector=promql_selector
            )
        except Exception as exc:  # noqa: BLE001
            secondary_notes.append(f"promql provider skipped: {exc}")
    if datadog_base_url and datadog_api_key and datadog_application_key:
        try:
            from .providers.datadog import DatadogMetricsProvider  # noqa: PLC0415

            secondary["datadog"] = DatadogMetricsProvider.from_profile(
                datadog_base_url,
                profile,
                api_key=datadog_api_key,
                application_key=datadog_application_key,
                scope=datadog_scope,
            )
        except Exception as exc:  # noqa: BLE001
            secondary_notes.append(f"datadog provider skipped: {exc}")

    def _snapshot_from(
        provider: Any,
        provider_name: str,
        load: Any,
        rid: str,
        traces: dict[str, Any],
        runtime_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build one snapshot from ``provider`` using a completed load's window."""
        raw = {name: provider.fetch(name) for name in profile.snapshot_metrics.values()}
        breakdown = (
            provider.endpoint_breakdown() if hasattr(provider, "endpoint_breakdown") else None
        )
        notes = list(secondary_notes)
        auth_err = getattr(provider, "auth_error", "")
        if auth_err:
            notes.append(auth_err)
        unreadable = getattr(provider, "unreadable", {})
        if unreadable:
            notes.append(
                f"provider {provider_name!r} has unreadable metrics: "
                + ", ".join(f"{k} ({v.get('reason', '?')})" for k, v in unreadable.items())
            )
        ev = AvailableEvidence(
            metrics=any(raw.values()),
            traces=traces["traces"],
            trace_reason=traces["trace_reason"],
            trace_sampling_rate_pct=traces["trace_sampling_rate_pct"],
            endpoint_breakdown=bool(breakdown),
            # Gauge sampling is Actuator-only during the run. Secondary providers
            # read post-run from stored series; they have no per-sample list.
            gauge_sampling=(provider_name == "actuator" and bool(load.gauge_samples)),
            notes=notes,
        )
        if provider_name != "actuator":
            ev.notes.append(
                f"gauge peaks for {provider_name!r} are post-run point reads, "
                "not mid-run samples; peaks may understate pressure during the window"
            )
        return build_snapshot(
            {k: v for k, v in raw.items() if v},
            run_id=rid,
            profile_name=profile.name,
            target=sla.target_base_url,
            gauge_samples=load.gauge_samples if provider_name == "actuator" else {},
            load_summary=load.as_load_summary(),
            endpoint_breakdown=breakdown,
            metric_keys=profile.snapshot_metrics,
            window=measurement_window(load),
            runtime_config=runtime_config,
            redaction_allowlist=profile.redaction_allowlist,
            evidence=ev,
        )

    def multi_measure(scn: Scenario, rid: str) -> Any:
        """Run load once, then build one snapshot per configured provider."""
        load = runner.run(scn, rid)
        traces = trace_evidence(trace_provider)
        runtime_cfg = actuator.fetch_all_env()
        snapshots: dict[str, Any] = {}
        snapshots["actuator"] = _snapshot_from(
            actuator, "actuator", load, rid, traces, runtime_config=runtime_cfg
        )
        for pname, prov in secondary.items():
            snapshots[pname] = _snapshot_from(prov, pname, load, rid, traces)
        return load, snapshots

    return multi_measure


def _check_workspace_sync(workspace: Path, profile: "TargetProfile") -> None:
    """Verify workspace is in a clean, synced state before starting a campaign.

    Raises CampaignRefused for uncommitted changes (hard blocker).
    Prints a warning for sandbox drift vs. remote or vs. base_branch so the
    operator can decide whether to reset before proceeding.
    """
    import subprocess

    deploy = profile.deploy

    # --- 1. Uncommitted changes in the workspace → hard refuse ---
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode == 0 and r.stdout.strip():
        raise CampaignRefused(
            f"workspace has uncommitted changes — commit or stash before starting:\n"
            f"{r.stdout.rstrip()}"
        )

    # --- 2. Fetch remote deploy branch to get its current state ---
    subprocess.run(
        ["git", "fetch", deploy.remote],
        cwd=workspace,
        capture_output=True,
        timeout=30,
    )

    # --- 3. Warn if local HEAD differs from remote sandbox branch ---
    r = subprocess.run(
        ["git", "rev-parse", "HEAD", f"{deploy.remote}/{deploy.branch}"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode == 0:
        shas = r.stdout.strip().splitlines()
        if len(shas) == 2 and shas[0] != shas[1]:
            local_sha, remote_sha = shas[0], shas[1]
            print(
                f"\n[workspace] WARNING: local branch ({local_sha[:8]}) is out of sync "
                f"with remote {deploy.remote}/{deploy.branch} ({remote_sha[:8]}).\n"
                f"  Sync: git -C {workspace} reset --hard "
                f"{deploy.remote}/{deploy.branch}\n",
                flush=True,
            )

    # --- 4. Fetch origin and warn if config_file differs from base_branch ---
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=workspace,
        capture_output=True,
        timeout=30,
    )
    base_ref = f"origin/{deploy.base_branch}"
    config_path = str(profile.config_file)
    r = subprocess.run(
        ["git", "diff", "--stat", f"{base_ref}..HEAD", "--", config_path],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode == 0 and r.stdout.strip():
        print(
            f"\n[workspace] WARNING: {config_path} has diverged from {base_ref} — "
            f"previous experiment changes may still be applied.\n"
            f"  To reset: git -C {workspace} checkout {base_ref} -- {config_path} "
            f"&& git -C {workspace} commit -m 'sync: reset to {deploy.base_branch}' "
            f"&& git -C {workspace} push {deploy.remote} {deploy.branch}\n",
            flush=True,
        )


def typical_call_cost(results_dir: str | Path = "results") -> float | None:
    """Mean cost of one priced diagnosis across past manifests; None with no history."""
    from ..economics.pricing import Pricing  # noqa: PLC0415
    from .report import ReportError, load_campaign  # noqa: PLC0415
    from .scorer import _load_pricing_yaml, score_cost  # noqa: PLC0415

    pricing = Pricing.from_mapping(_load_pricing_yaml())
    costs: list[float] = []
    for path in sorted(Path(results_dir).glob("*.json")):
        try:
            costs += [c for c in score_cost(load_campaign(path), pricing).per_experiment if c]
        except (ReportError, OSError, ValueError, TypeError):
            continue
    return sum(costs) / len(costs) if costs else None


def minimum_wall_clock_s(windows: list[tuple[float, float]]) -> float:
    """The least wall clock a run can take: each scenario's warmup + baseline."""
    return sum(warmup + measure for warmup, measure in windows)


def wall_clock_warning(cap_h: float | None, windows: list[tuple[float, float]]) -> str:
    """Operator, 24 October 2026: a cap that cannot fit the scenarios is called out up front."""
    if cap_h is None:
        return ""
    need = minimum_wall_clock_s(windows)
    if need <= cap_h * 3600:
        return ""
    return (f"the wall-clock cap of {cap_h:g} h cannot fit the scenarios: their baselines alone need "
            f"{need / 3600:.2f} h ({len(windows)} scenario(s), warmup + measured window each), before any "
            "experiment. The run would pause at the cap.")


def _positive(name: str, value: float | None) -> float | None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be greater than 0, got {value}")
    return value


def resolve_spawn_rate(given: float | None, sla: Sla) -> float:
    """Users started per second: the flag, else the SLA's at_load, else Locust's 10.

    The wizard derives it from a scenario's Ramp (users / ramp seconds).
    """
    rate = given if given is not None else (sla.at_load or {}).get("spawn_rate", 10.0)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        raise ValueError(f"spawn rate must be a number, got {rate!r}") from None
    if rate <= 0:
        raise ValueError(f"spawn rate must be greater than 0, got {rate}")
    return rate


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
    stakeholder_request: str = "",
    spawn_rate: float | None = None,
    cost_cap_usd: float | None = None,
    wall_clock_cap_h: float | None = None,
    advisory: bool = False,
    mix: dict[str, Any] | None = None,
    requests: list[dict[str, Any]] | None = None,
    data: dict[str, dict[str, str]] | None = None,
    accept_tradeoffs: bool = False,
) -> Any:
    """Assemble a real campaign from configuration. Kept apart from ``cmd_run``
    so a test can build one without going through argparse."""
    from ..gateway import GatewayClient
    from .approval import FileApprovalGate, PreapprovedGate
    from .campaign import Campaign
    from .diagnosis import Diagnoser
    from .runner import Scenario

    if not run_id:
        run_id = time.strftime("run-%Y%m%d-%H%M%S", time.gmtime())

    profile = TargetProfile.named(profile_name)
    sla = Sla.load(sla_path)

    # Advisory never pushes, so a workspace out of step with the deploy branch
    # does not matter to it, and it needs no deploy pipeline at all.
    if profile.deploy.automated and workspace and not advisory:
        _check_workspace_sync(Path(workspace), profile)

    scenario = Scenario(
        name=scenario_name,
        host=sla.target_base_url,
        users=users,
        warmup_s=warmup_s,
        measure_s=measure_s,
        spawn_rate=resolve_spawn_rate(spawn_rate, sla),
        tags=(tag_for_endpoint(sla.endpoint),),
    )
    mix_targets: dict[str, float] = {}
    if requests:
        # Templated requests replace the built-in tasks; each endpoint is judged
        # on its own p99, exactly like a mix (Group 40).
        from .loaddata import resolve  # noqa: PLC0415

        scenario = replace(scenario, requests=tuple(requests), data=resolve(Path(state_dir), data or {}),
                           tags=())
        mix_targets = {r["endpoint"]: float(r.get("p99_ms") or sla.p99_ms) for r in requests}
    elif mix:
        # A traffic mix drives its own endpoints at its own weights, and every
        # verdict is per endpoint (DESIGN.md §7, Group 39).
        tag_mix, mix_targets = mix_from(mix, sla)
        scenario = replace(scenario, mix=tag_mix, tags=())

    measure = build_measure(
        profile,
        sla,
        jaeger_url=jaeger_url,
        jaeger_service=jaeger_service,
        trace_sampling_rate_pct=trace_sampling_rate_pct,
        state_dir=state_dir,
    )

    gate = (
        PreapprovedGate()
        if approve_mode == "preapproved"
        else FileApprovalGate(run_id=run_id, state_dir=state_dir, timeout_s=approval_timeout_s)
    )

    deployer = (
        None
        if advisory
        else GitPushDeployer(target=profile.deploy, workspace=workspace)
        if profile.deploy.automated
        else ManualDeployer(target=profile.deploy)
    )

    # Pipeline deploys (push → hook → rebuild → restart) handle the restart on
    # the target box. Running a local restart command before the push would try
    # to start the service on Box B, which is wrong. Set restarter=None so the
    # applicator skips the local step; the deploy hook is what puts the change
    # in force (DESIGN.md §19, applicator.py lines 577–581).
    restarter = (
        None
        if profile.deploy.automated
        else ManualRestarter(profile.restart)
        if profile.restart.manual
        else CommandRestarter(profile.restart, workspace=workspace)
    )

    gateway_client = GatewayClient()

    from ..core.memory.store import MemoryStore  # noqa: PLC0415
    from ..knowledge import default_embedder  # noqa: PLC0415

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
        # Policy memory enforces the SLA can't be overwritten by the agent (DESIGN.md 4.4).
        # The agent runtime's own store (memory.sqlite), opened with the same
        # embedder: a different one would mark the agent's vectors stale and
        # rebuild its index without them. Only an unembedded Policy record and
        # audit entries are written here, so starting a campaign makes no model call.
        memory_store=MemoryStore(Path(state_dir) / "memory.sqlite", embedder=default_embedder()),
        # Real campaigns hit a hosted, free-tier gateway that can be cold; a
        # scripted test campaign never sets this and skips the wait entirely.
        warm_up_gateway=gateway_client.warm_up,
        stakeholder_request=stakeholder_request,
        guarded_files=tuple(p for p in (sla.source_path, profile.source_path) if p),
        # §10 hooks: uploaded .py files in <state>/hooks, run in the target's
        # workspace with an allowlisted environment (Group 41).
        hooks=hooks_from_mapping(sla.hooks, workspace=workspace or None,
                                 hooks_dir=str(Path(state_dir) / "hooks"), run_id=run_id),
        confirm_config_changes=approve_mode == "file" and not advisory,
        advisory=advisory,
        mix_targets=mix_targets,
        accept_tradeoffs=bool(accept_tradeoffs and mix_targets),
        typical_call_cost_usd=typical_call_cost(),
        cost_cap_usd=_positive("cost cap", cost_cap_usd),
        wall_clock_cap_s=None if wall_clock_cap_h is None else _positive("wall-clock cap", wall_clock_cap_h) * 3600,
    )


def _series_path(root: Path, series_id: str) -> Path:
    return Path(root) / "series" / f"{series_id}.json"


def _series_for_run(root: Path, run_id: str) -> dict[str, Any] | None:
    for path in sorted((Path(root) / "series").glob("*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if run_id in (plan.get("runs") or {}).values():
            return plan
    return None


def _save_series(root: Path, plan: dict[str, Any]) -> None:
    path = _series_path(root, plan["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")


def _scenario_campaign(root: Path, plan: dict[str, Any], name: str, index: int, of: int) -> Any:
    """One declared scenario as its own campaign, with whatever cap is left."""
    sla = Sla.load(plan["kwargs"]["sla_path"])
    definition = sla.scenario(name)
    if definition is None:
        raise CampaignRefused(f"scenario {name!r} is no longer declared in {plan['kwargs']['sla_path']}")
    kwargs = dict(plan["kwargs"])
    caps = plan.get("caps") or {}
    if caps.get("cost_cap_usd") is not None:
        kwargs["cost_cap_usd"] = caps["cost_cap_usd"] - plan.get("spent_usd", 0.0)
    if caps.get("wall_clock_cap_h") is not None:
        kwargs["wall_clock_cap_h"] = caps["wall_clock_cap_h"] - plan.get("active_s", 0.0) / 3600
    for key in ("cost_cap_usd", "wall_clock_cap_h"):
        if kwargs.get(key) is not None and kwargs[key] <= 0:
            raise _SeriesCapReached(f"the series' {key.replace('_', ' ')} is used up before {name!r}")
    kwargs.update(scenario_name=name, users=definition["users"], warmup_s=definition["warmup_s"],
                  measure_s=definition["measure_s"], spawn_rate=definition.get("spawn_rate"),
                  mix=definition.get("mix"), requests=definition.get("requests"), data=definition.get("data"),
                  accept_tradeoffs=definition.get("accept_tradeoffs", False))
    campaign = build_campaign(run_id=f"{plan['id']}-{name}", state_dir=str(root), **kwargs)
    campaign.declared_scenario = name
    campaign.series = {"id": plan["id"], "index": index, "of": of, "scenario": name}
    return campaign


class _SeriesCapReached(RuntimeError):
    pass


def _record_scenario(plan: dict[str, Any], name: str, result: Any) -> None:
    plan.setdefault("runs", {})[name] = result.run_id
    from ..economics.pricing import Pricing  # noqa: PLC0415
    from .scorer import _load_pricing_yaml, score_cost  # noqa: PLC0415

    plan["spent_usd"] = plan.get("spent_usd", 0.0) + score_cost(
        result.as_dict(), Pricing.from_mapping(_load_pricing_yaml())).total
    finished = result.finished_at_epoch_s or time.time()
    plan["active_s"] = plan.get("active_s", 0.0) + max(0.0, finished - result.started_at_epoch_s - result.paused_seconds)


def _continue_series(root: Path, plan: dict[str, Any]) -> int:
    """Run the series' remaining scenarios, re-reading the SLA between each.

    Re-reading is what honours an operator's mid-series edit to a later
    scenario (Group 33). A pause stops the series where it is; `crucible resume`
    on that scenario's run carries the rest on afterwards.
    """
    import asyncio

    while True:
        try:
            declared = [s["name"] for s in Sla.load(plan["kwargs"]["sla_path"]).scenarios]
        except CampaignRefused as exc:
            plan["status"] = f"stopped: the SLA no longer loads ({exc})"
            break
        remaining = [n for n in declared if n not in plan["done"]]
        if not remaining:
            plan["status"] = "complete"
            break
        name = remaining[0]
        index, of = len(plan["done"]) + 1, len(plan["done"]) + len(remaining)
        try:
            campaign = _scenario_campaign(root, plan, name, index, of)
        except _SeriesCapReached as exc:
            series_id = plan["id"]
            plan["status"] = (f"paused: {exc}. Resume with a higher cap: `crucible resume {series_id} "
                              "--cost-cap ... / --wall-clock-cap ...`, or abort it")
            break
        except (CampaignRefused, FileNotFoundError, ValueError) as exc:
            plan["status"] = f"stopped: scenario {name!r} refused ({exc})"
            break
        plan.setdefault("runs", {})[name] = campaign.run_id
        _save_series(root, plan)
        print(_rule(f"series {plan['id']} | scenario {index} of {of}: {name} | run {campaign.run_id}"))
        result = asyncio.run(campaign.run())
        _record_scenario(plan, name, result)
        print(f"  {name}: {result.stopped_reason}")
        if result.paused:
            plan["status"] = f"paused in {name}: resume with `crucible resume {result.run_id}`"
            break
        if result.aborted:
            plan["status"] = f"aborted in {name}: {result.abort_reason}"
            break
        plan["done"].append(name)
        if result.cap_hit:
            plan["status"] = f"stopped: {result.stopped_reason}"
            break
        _save_series(root, plan)
    _save_series(root, plan)
    print(f"\nseries {plan['id']}: {plan['status']}")
    return OK


def _run_series(state_dir: Path, run_id: str, kwargs: dict[str, Any]) -> int:
    try:
        sla = Sla.load(kwargs["sla_path"])
    except CampaignRefused as exc:
        print(f"campaign refused: {exc}")
        return REFUSED
    if not sla.scenarios:
        print(f"campaign refused: {kwargs['sla_path']} declares no scenarios; run without --all-scenarios "
              "to use objective.at_load")
        return REFUSED
    series_id = run_id or time.strftime("series-%Y%m%d-%H%M%S", time.gmtime())
    caps = {"cost_cap_usd": kwargs.pop("cost_cap_usd", None), "wall_clock_cap_h": kwargs.pop("wall_clock_cap_h", None)}
    for key in ("scenario_name", "users", "warmup_s", "measure_s", "spawn_rate", "mix", "requests", "data",
                "accept_tradeoffs"):
        kwargs.pop(key, None)
    warning = wall_clock_warning(caps["wall_clock_cap_h"],
                                 [(sc["warmup_s"], sc["measure_s"]) for sc in sla.scenarios])
    if warning:
        print(f"WARNING: {warning}")
    plan = {"id": series_id, "started_at_epoch_s": time.time(), "kwargs": kwargs, "caps": caps,
            "done": [], "runs": {}, "spent_usd": 0.0, "active_s": 0.0, "status": "running"}
    return _continue_series(state_dir, plan)


def cmd_run(**kwargs: Any) -> int:
    """Run a campaign. Blocks until it finishes, is aborted, or is declined."""
    import asyncio

    state_dir = _state_dir(kwargs.pop("state_dir", None))
    run_id = kwargs.pop("run_id", "") or ""
    if kwargs.pop("all_scenarios", False):
        return _run_series(state_dir, run_id, kwargs)
    warning = wall_clock_warning(kwargs.get("wall_clock_cap_h"),
                                 [(kwargs.get("warmup_s", 120.0), kwargs.get("measure_s", 300.0))])
    if warning:
        print(f"WARNING: {warning}")
    try:
        campaign = build_campaign(run_id=run_id, state_dir=state_dir, **kwargs)
    except (CampaignRefused, FileNotFoundError, ValueError) as exc:
        print(f"campaign refused: {exc}")
        return REFUSED

    print(f"run {campaign.run_id}: {campaign.sla.name} against {campaign.sla.environment_name}")
    print(f"  approve from another terminal: crucible approve {campaign.run_id} --experiment 1")
    print(f"  pause (resumable):             crucible pause {campaign.run_id}")
    print(f"  abort (discard in-flight):     crucible abort {campaign.run_id}")

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
# ceiling
# ---------------------------------------------------------------------------


def cmd_ceiling(
    *,
    profile_name: str | None = None,
    sla_path: str | None = None,
    scenario_name: str | None = None,
    users: int = 50,
    warmup_s: float = 120.0,
    measure_s: float = 300.0,
    min_users: int = 0,
    max_users: int = 500,
    step_size: int = 50,
    run_id: str = "",
    state_dir: str | None = None,
    jaeger_url: str = "",
    jaeger_service: str = "",
    spawn_rate: float | None = None,
) -> int:
    """Characterise a service's capacity ceiling with a stepped load ramp.

    Operator must declare the intent and bounds (§20.1) — never inferred.
    Each step is a full measured run (warmup discarded, gauges sampled).
    The result is the knee as a pair: last level that met the SLA and first
    that did not (§20.4). Never compare this against experiment manifests.
    """
    from .ceiling import CeilingProbe
    from .runner import Scenario

    root = _state_dir(state_dir)
    if not profile_name:
        print("--profile is required")
        return REFUSED
    if not sla_path:
        print("--sla is required")
        return REFUSED

    try:
        profile = TargetProfile.named(profile_name)
        sla = Sla.load(sla_path)
        tag = tag_for_endpoint(sla.endpoint)
        rate = resolve_spawn_rate(spawn_rate, sla)
    except (CampaignRefused, FileNotFoundError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return REFUSED

    scenario = Scenario(
        name=scenario_name or "ceiling",
        host=sla.target_base_url,
        users=users,
        warmup_s=warmup_s,
        measure_s=measure_s,
        spawn_rate=rate,
        tags=(tag,),
    )
    measure = build_measure(profile, sla, jaeger_url=jaeger_url, jaeger_service=jaeger_service)

    probe = CeilingProbe(
        sla=sla,
        base_scenario=scenario,
        measure=measure,
        min_users=min_users or scenario.users,
        max_users=max_users,
        step_size=step_size,
        run_id=run_id or "",
        results_dir=root / "results",
        sla_path=sla.source_path,
    )

    print(f"ceiling probe: {sla.name} — {probe.min_users}..{probe.max_users} users, step {probe.step_size}")
    print(f"  SLA: {sla.p99_ms:.0f} ms p99, errors <= {sla.error_rate_pct:.1f}%")
    print("  (this deliberately drives the service until it breaks — pre-prod only)")

    try:
        result = probe.run()
    except CampaignRefused as refused:
        print(f"probe refused: {refused}")
        return REFUSED

    print(_rule("ceiling result"))
    for step in result.steps:
        marker = "PASS" if step.sla_met else ("ABORT" if step.aborted else "FAIL")
        p99_str = f"{step.load.get('p99_ms'):.0f} ms" if step.load.get("p99_ms") else "N/A"
        print(f"  {step.users:4d} users  [{marker}]  p99 {p99_str}")
    print()
    print(f"  last passing : {result.last_passing_users} users")
    print(f"  first failing: {result.first_failing_users} users")
    print(f"  stopped      : {result.stopped_reason}")
    print(f"  written to   : {result.run_id}.ceiling.json")
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
    skip_missing_fixtures: bool = False,
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

    async def warm_then_replay() -> Any:
        # The gateway is hosted on a free tier and spins down when idle; pay the
        # cold start before the first case rather than inside it.
        #
        # ONE event loop for both. The gateway's httpx client pools connections
        # per loop, and this was two `asyncio.run` calls until 26 September 2026:
        # the warm-up's connection outlived its loop, and the first case of every
        # replay failed with "Event loop is closed". Always the first case, so
        # always T1 on perflab_pool_starved -- the headline fixture was the one
        # replay could never measure, on every run, without looking flaky.
        await gateway.warm_up()
        return await runner.run(
            tasks,
            fixture_dir,
            task_set_name=str(Path(tasks_path).name),
            skip_missing_fixtures=skip_missing_fixtures,
        )

    print(f"replaying {len(tasks)} task(s) against fixtures in {fixture_dir}")
    try:
        result = asyncio.run(warm_then_replay())
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

    DESIGN.md 8: a diff across environments is flagged, not silently allowed.
    Every claim input is checked. Only a different scenario or load profile
    refuses (non-zero exit), because only those make the compared figures
    measure different things; every other difference is listed above them.
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

    if result.identical_setup:
        print("\n  Same environment, SLA, profile, scenario, collector, noise floor and")
        print("  model. These two campaigns measured the same thing.")
    elif result.comparable:
        print("\n  COMPARABLE, WITH DIFFERENCES. Same scenario and load; the setup differs:\n")
        for difference in result.differences:
            print(f"  - {difference}")
        print(f"\n  {result.warning()}")
    else:
        print("\n  NOT COMPARABLE. These campaigns did not measure the same thing:\n")
        for difference in result.blocking:
            print(f"  - {difference}")
        others = [d for d in result.differences if d not in result.blocking]
        if others:
            print("\n  Also different:")
            for difference in others:
                print(f"  - {difference}")
        print(f"\n  {result.warning()}")

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
    all_providers: bool = False,
    promql_url: str = "",
    promql_instance: str = "",
    datadog_base_url: str = "",
    datadog_api_key: str = "",
    datadog_application_key: str = "",
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
        try:
            k1_tag = tag or tag_for_endpoint(sla.endpoint)
        except ValueError as exc:
            print(f"capture refused: {exc}")
            return REFUSED
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
    if provider_name not in MEASURABLE_PROVIDERS:
        print(
            f"capture refused: {provider_name!r} is not in MEASURABLE_PROVIDERS "
            f"({', '.join(MEASURABLE_PROVIDERS)}). Add support in build_multi_measure first."
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

    run_mode = "all-providers" if all_providers else provider_name
    print(_rule(f"capture | {spec.id} | {run_mode} | tag {spec.scenario_tag}"))
    print(f"\n  ground truth : {spec.cause_family or '(none - healthy fixture)'}")
    print(f"  severity     : {spec.severity or '(unstated)'}")
    print("  set up by    : a human, BEFORE this command (Crucible does not set the")
    print("                 target up -- that is what keeps the measurement independent)")
    print(f"  expected     : {', '.join(f'{k}={v}' for k, v in spec.bottleneck_config.items())}")
    print("\n  If the target is NOT in that state, stop now: this would capture a")
    print("  snapshot of something else under this fixture's name.\n")

    profile = TargetProfile.named(profile_name or spec.profile)
    scenario = Scenario(
        name=scenario_name,
        host=sla.target_base_url,
        users=users,
        warmup_s=warmup_s,
        measure_s=measure_s,
        tags=(spec.scenario_tag,),
        expect_possible_failure=spec.expect_possible_failure,
    )

    if all_providers:
        # B1: one run, every configured provider. The same load window yields
        # three snapshots and the provider-agreement comparison uses identical
        # traffic rather than three independent runs. Only providers whose URL
        # (and credentials for Datadog) are supplied are actually queried.
        multi = build_multi_measure(
            profile,
            sla,
            promql_url=promql_url,
            promql_selector={"instance": promql_instance} if promql_instance else None,
            datadog_base_url=datadog_base_url,
            datadog_api_key=datadog_api_key,
            datadog_application_key=datadog_application_key,
        )
        try:
            _load, snapshots = multi(scenario, f"fixture-{spec.id}")
        except Exception as exc:  # noqa: BLE001
            print(f"capture refused: {exc}")
            return REFUSED
        import time as _time  # noqa: PLC0415

        from .collector import COLLECTOR_VERSION  # noqa: PLC0415
        from .fixtures import CapturedFixture  # noqa: PLC0415

        for pname, snap in snapshots.items():
            if snap.get("collector_version") != COLLECTOR_VERSION:
                print(
                    f"  SKIPPED {pname}: collector version mismatch "
                    f"({snap.get('collector_version')!r} vs {COLLECTOR_VERSION!r})"
                )
                continue
            captured_all = CapturedFixture(
                spec=spec,
                snapshot=snap,
                provider=pname,
                captured_at_epoch_s=float(snap.get("captured_at_epoch_s") or _time.time()),
                warmup_s=warmup_s or 0.0,
                measure_s=measure_s or 0.0,
            )
            path = captured_all.write(out_dir)
            print(f"  written: {path}  [{pname}]")
        if not spec.validated_at:
            print(
                f"\n  NOTE: {spec.id} has no validated_at date. Confirm the signal is "
                "actually present in the snapshot, then record the date in "
                f"{fixture_config_dir}/{spec.id}.yaml."
            )
        return OK

    if provider_name == "actuator":
        measure = build_measure(profile, sla)
    else:
        # build_measure always wires ActuatorMetricsProvider. For a non-Actuator
        # capture the snapshot must come from the requested provider, so use
        # build_multi_measure (which still drives gauge sampling through Actuator
        # during the load window) and select only the requested provider's snapshot.
        # Refusing early if that provider is unreachable is better than writing a
        # fixture labelled "promql" that contains Actuator numbers.
        _multi = build_multi_measure(
            profile,
            sla,
            promql_url=promql_url if provider_name == "promql" else "",
            datadog_base_url=datadog_base_url if provider_name == "datadog" else "",
            datadog_api_key=datadog_api_key if provider_name == "datadog" else "",
            datadog_application_key=datadog_application_key if provider_name == "datadog" else "",
        )
        _pname = provider_name

        def measure(scn: Any, rid: str) -> Any:
            load, snapshots = _multi(scn, rid)
            if _pname not in snapshots:
                raise FixtureError(
                    f"provider {_pname!r} was not reachable at capture time. "
                    f"Check URL and credentials, then retry."
                )
            return load, snapshots[_pname]

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


def cmd_export(
    *,
    sla_path: str = "config/slo.yaml",
    profile_name: str = "spring-boot",
    out: str = "",
) -> int:
    """Export the collection (SLA + profile name) as a shareable YAML.

    Credentials are never included (§8): target URL, environment kind and
    profile name travel; datasource passwords and API keys do not. A teammate
    imports the YAML and supplies their own environment.

    The output is a human-readable snapshot of what the investigation is
    configured to do, not a live config-file copy. It does not round-trip
    back into ``crucible init`` without review — that is deliberate, because
    importing silently overwrites an existing config is the kind of thing
    that should require a human decision.
    """
    import yaml  # noqa: PLC0415

    profile = TargetProfile.named(profile_name)
    sla = Sla.load(sla_path)

    # Strip anything that looks like a credential from the deploy config.
    deploy_export: dict[str, Any] = {}
    deploy = profile.deploy
    if deploy:
        deploy_export["remote"] = getattr(deploy, "remote", "") or ""
        deploy_export["branch"] = getattr(deploy, "branch", "") or ""
        deploy_export["base_branch"] = getattr(deploy, "base_branch", "") or ""
        # Never export the key path: the path is machine-specific and the
        # key itself must never leave the machine it lives on.

    doc = {
        "crucible_export": True,
        "collection": {
            "name": sla.name,
            "description": getattr(sla, "description", "") or "",
        },
        "target": {
            "base_url": sla.target_base_url,
            "endpoint": sla.endpoint,
            "environment_kind": sla.environment_kind,
        },
        "objective": {
            "latency_p99_ms": sla.latency_p99_ms,
            "error_rate_pct": sla.error_rate_pct,
        },
        "profile": profile_name,
        "deploy": deploy_export,
    }

    dumped = yaml.dump(doc, default_flow_style=False, sort_keys=False)
    if out:
        Path(out).write_text(dumped, encoding="utf-8")
        print(f"exported to {out}")
    else:
        print(dumped)
    return OK
