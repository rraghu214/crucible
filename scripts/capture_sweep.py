#!/usr/bin/env python3
"""Overnight capture sweep.

Walks every fixture YAML in ``config/fixtures/``, applies the declared
bottleneck_config, pushes to Box A, waits for the version URL to confirm, then
runs ``crucible capture`` for each provider listed.  Runs unattended after a
one-time human approval of the script and fixture list.

DESIGN contract (plan §B, 3 Oct 2026):
- "Crucible never sets up what it measures."  This script sets up the state;
  Crucible reads it.  The operator approves the fixture list once — the human
  still decides every state; the script just executes it at scale.
- One fixture → one commit on ``perftest_sandbox``.  Nothing goes to main or to
  any branch the operator works from (DESIGN.md §19.1a).
- No measurement begins until ``/api/version`` confirms the commit (§19.6).
- On a deploy failure the script logs the error and SKIPS the fixture, then
  tries to restore the baseline before continuing.  A capture on the wrong state
  is worse than no capture at all.

Usage (on Box B):
    python scripts/capture_sweep.py \\
        --slo config/slo.yaml \\
        --profile config/profiles/spring-boot.yaml \\
        --workspace /home/ubuntu/perf-lab \\
        --boxa-ip 10.0.0.79 \\
        --results-dir results/ \\
        [--dry-run] [--fixture <id>] [--resume] [--only-unvalidated]

With ``--dry-run`` nothing is pushed and no crucible command runs; it prints
what it would do.

With ``--resume`` fixtures that already have ``validated_at`` set are skipped.
With ``--only-unvalidated`` it skips fixtures with ``validated_at`` set.
Both flags behave the same; ``--resume`` is the ergonomic alias.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml  # pyyaml, already in the project's dev deps

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

log = logging.getLogger("sweep")


@dataclass
class FixtureSpec:
    id: str
    path: Path
    target_profile: str
    ground_truth: str
    severity: str
    bottleneck_config: dict[str, Any]
    setup: str
    scenario_tag: str
    providers: list[str]
    validated_at: str
    task_classes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "FixtureSpec":
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return cls(
            id=str(data.get("id", path.stem)),
            path=path,
            target_profile=str(data.get("target_profile", "")),
            ground_truth=str(data.get("ground_truth_cause_family", "")),
            severity=str(data.get("severity", "")),
            bottleneck_config=dict(data.get("bottleneck_config") or {}),
            setup=str(data.get("setup", "")),
            scenario_tag=str(data.get("scenario_tag", "")),
            providers=list(data.get("providers") or []),
            validated_at=str(data.get("validated_at") or ""),
            task_classes=list(data.get("task_classes") or []),
        )

    @property
    def jvm_flags(self) -> str:
        """``-Xmx128m`` etc. when the fixture needs a JVM flag; empty otherwise."""
        xmx = self.bottleneck_config.get("jvm.Xmx")
        return f"-Xmx{xmx}" if xmx else ""

    @property
    def app_properties(self) -> dict[str, Any]:
        """The subset of ``bottleneck_config`` that belongs in application.properties."""
        skip = {"jvm.Xmx", "endpoint"}
        return {k: v for k, v in self.bottleneck_config.items() if k not in skip}


# ---------------------------------------------------------------------------
# Baseline (restore between fixtures)
# ---------------------------------------------------------------------------

_BASELINE_APP_PROPERTIES: dict[str, Any] = {
    "spring.datasource.hikari.maximum-pool-size": 20,
    "spring.datasource.hikari.minimum-idle": 10,
    "spring.datasource.hikari.connection-timeout": 30000,
    "server.tomcat.threads.max": 200,
    "perflab.cache.enabled": True,
    "perflab.downstream.timeout-ms": 5000,
    "perflab.downstream.delay-seconds": 0.4,
    "perflab.churn.allocation-bytes": 1048576,
    "perflab.churn.retained-objects": 64,
    "perflab.slow.delay-ms": 200,
}

_BASELINE_JVM_FLAGS = ""  # empty = default heap


# ---------------------------------------------------------------------------
# application.properties editor
# ---------------------------------------------------------------------------

def _read_properties(path: Path) -> dict[str, str]:
    """Parse an application.properties file, preserving order."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            out[k.strip()] = v.strip()
    return out


def _write_properties_updates(path: Path, updates: dict[str, Any]) -> None:
    """Update specific properties in-place, preserving all other lines.

    Lines already present are replaced; lines not present are APPENDED.
    Comments and blank lines are preserved.
    """
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    remaining = dict(updates)

    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                val = remaining.pop(key)
                # convert booleans to lowercase for Spring
                val_str = str(val).lower() if isinstance(val, bool) else str(val)
                out.append(f"{key}={val_str}\n")
                continue
        out.append(line)

    for key, val in remaining.items():
        val_str = str(val).lower() if isinstance(val, bool) else str(val)
        out.append(f"{key}={val_str}\n")

    path.write_text("".join(out), encoding="utf-8")


# ---------------------------------------------------------------------------
# perflab.env writer (JVM overrides on Box A, via SSH)
# ---------------------------------------------------------------------------

def _set_boxa_java_opts(flags: str, boxa_ip: str, key_path: str, dry_run: bool) -> bool:
    """Write PERFLAB_JAVA_OPTS in ~/perflab.env on Box A via SSH.

    The post-receive hook sources this file before starting the JVM.  Returns
    True on success or dry_run; False on SSH failure.

    When flags is empty the function returns True immediately without SSH —
    the hook defaults to no extra JVM args, and the deploy key is git-shell
    restricted so shell commands over it will always fail.
    """
    if not flags:
        return True
    content = f'PERFLAB_JAVA_OPTS="{flags}"\n'
    command = f"printf '%s' {repr(content)} > ~/perflab.env"
    ssh_cmd = [
        "ssh",
        "-i", key_path,
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        f"ubuntu@{boxa_ip}",
        command,
    ]
    if dry_run:
        log.info("[dry-run] would ssh to Box A: %s", command)
        return True
    log.info("Setting ~/perflab.env on Box A (flags=%r)", flags)
    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        log.error("SSH to Box A failed: %s", result.stderr[:500])
        return False
    return True


# ---------------------------------------------------------------------------
# Git workspace operations
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str | Path, dry_run: bool = False) -> tuple[int, str]:
    if dry_run and args[0] == "push":
        log.info("[dry-run] git %s", " ".join(args))
        return 0, "dry-run"
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, (result.stderr or result.stdout or "").strip()


def _commit_and_push(
    workspace: Path,
    app_props: Path,
    message: str,
    remote: str = "perftest",
    branch: str = "perftest_sandbox",
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Stage application.properties, commit, push. Returns (ok, sha_or_error)."""
    # Sync local branch with remote before committing.  A previous push
    # rejection leaves local commits the remote doesn't have; resetting to
    # remote HEAD (with a stash/pop to preserve the current working-tree
    # edits) makes the next push a simple fast-forward.
    if not dry_run:
        code_f, _ = _git(["fetch", remote], workspace, dry_run=False)
        if code_f == 0:
            code_rv, remote_sha = _git(["rev-parse", f"{remote}/{branch}"], workspace)
            code_lv, local_sha = _git(["rev-parse", "HEAD"], workspace)
            if code_rv == 0 and code_lv == 0 and remote_sha.strip() != local_sha.strip():
                log.info("Diverged from %s/%s — stash, reset, pop", remote, branch)
                _git(["stash"], workspace, dry_run=False)
                _git(["reset", "--hard", f"{remote}/{branch}"], workspace, dry_run=False)
                _git(["stash", "pop"], workspace, dry_run=False)

    code, out = _git(["add", "--", str(app_props.relative_to(workspace))], workspace, dry_run=False)
    if code != 0:
        return False, f"git add failed: {out[:300]}"

    # Check if there's anything to commit
    code, status = _git(["status", "--porcelain"], workspace)
    if code == 0 and not status.strip():
        # Nothing staged — already at this state. Read the current sha.
        _code, sha = _git(["rev-parse", "HEAD"], workspace)
        log.info("workspace already at desired state (sha=%s)", sha[:12])
        return True, sha.strip()

    code, out = _git(["commit", "-m", message, "--", str(app_props.relative_to(workspace))], workspace, dry_run=False)
    if code != 0:
        return False, f"git commit failed: {out[:300]}"

    code, sha = _git(["rev-parse", "HEAD"], workspace)
    if code != 0:
        return False, f"git rev-parse failed: {sha[:300]}"
    sha = sha.strip()

    code, push_out = _git(["push", remote, f"{sha}:refs/heads/{branch}"], workspace, dry_run=dry_run)
    if code != 0:
        return False, f"git push failed: {push_out[:300]}"
    return True, sha


# ---------------------------------------------------------------------------
# Version polling
# ---------------------------------------------------------------------------

def _wait_for_commit(
    version_url: str,
    commit: str,
    timeout_s: float = 300.0,
    poll_s: float = 5.0,
    dry_run: bool = False,
) -> bool:
    if dry_run:
        log.info("[dry-run] would poll %s for commit %s", version_url, commit[:12])
        return True
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for Box A to report commit %s (timeout=%.0fs)", commit[:12], timeout_s)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(version_url, timeout=5) as resp:
                data = json.load(resp)
            reported = str(data.get("commit", "")).strip()
            if reported and (reported.startswith(commit[:7]) or commit.startswith(reported[:7])):
                log.info("Box A confirmed commit %s (reported %s)", commit[:12], reported[:12])
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(poll_s)
    log.error("Timed out waiting for commit %s", commit[:12])
    return False


# ---------------------------------------------------------------------------
# crucible capture
# ---------------------------------------------------------------------------

def _run_capture(
    fixture_id: str,
    provider: str,
    slo_path: str,
    profile_path: str,
    results_dir: str,
    promql_url: str,
    promql_instance: str,
    dry_run: bool,
) -> bool:
    cmd = [
        "crucible", "capture",
        "--sla", slo_path,
        "--profile", profile_path,
        "--fixture", fixture_id,
        "--provider", provider,
        "--out", results_dir,
    ]
    if provider == "promql" and promql_url:
        cmd += ["--promql-url", promql_url]
        if promql_instance:
            cmd += ["--promql-instance", promql_instance]

    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(cmd))
        return True
    log.info("Capturing fixture=%s provider=%s", fixture_id, provider)
    result = subprocess.run(cmd, capture_output=False, timeout=900)
    if result.returncode != 0:
        log.error("capture failed for %s/%s (exit %d)", fixture_id, provider, result.returncode)
        return False
    log.info("Captured %s/%s", fixture_id, provider)
    return True


# ---------------------------------------------------------------------------
# validated_at updater
# ---------------------------------------------------------------------------

def _mark_validated(fixture_path: Path) -> None:
    """Stamp validated_at with today's date in the YAML file."""
    today = str(date.today())
    text = fixture_path.read_text(encoding="utf-8")
    text = re.sub(
        r'^(validated_at:\s*)""?\s*$',
        f'validated_at: "{today}"',
        text,
        flags=re.MULTILINE,
    )
    fixture_path.write_text(text, encoding="utf-8")
    log.info("Marked %s validated_at=%s", fixture_path.name, today)


# ---------------------------------------------------------------------------
# Main sweep loop
# ---------------------------------------------------------------------------

def sweep(args: argparse.Namespace) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    workspace = Path(args.workspace)
    app_props = workspace / "src" / "main" / "resources" / "application.properties"

    if not app_props.exists():
        log.error("application.properties not found at %s", app_props)
        return 1

    all_fixtures = sorted(fixtures_dir.glob("*.yaml"), key=lambda p: p.name)
    specs = [FixtureSpec.load(f) for f in all_fixtures]

    if args.fixture:
        specs = [s for s in specs if s.id == args.fixture]
        if not specs:
            log.error("Fixture %r not found in %s", args.fixture, fixtures_dir)
            return 1

    if args.resume or args.only_unvalidated:
        before = len(specs)
        specs = [s for s in specs if not s.validated_at]
        log.info("Skipping %d already-validated fixtures", before - len(specs))

    if not specs:
        log.info("Nothing to capture.")
        return 0

    # Filter out fixtures with no providers (e.g. code_latency before /api/slow)
    capturable = [s for s in specs if s.providers]
    skipped = [s for s in specs if not s.providers]
    if skipped:
        log.warning("Skipping %d fixtures with empty providers: %s",
                    len(skipped), ", ".join(s.id for s in skipped))

    log.info("Plan: %d fixtures × ≤3 providers", len(capturable))
    for spec in capturable:
        log.info("  %s (%s, providers=%s)", spec.id, spec.severity, spec.providers)

    if args.plan:
        log.info("--plan: not executing.")
        return 0

    failed: list[str] = []
    succeeded: list[str] = []

    for spec in capturable:
        log.info("=" * 60)
        log.info("FIXTURE: %s  ground_truth=%s  tag=%s", spec.id, spec.ground_truth, spec.scenario_tag)

        # ----------------------------------------------------------------
        # Step 1: set JVM flags on Box A if needed
        # ----------------------------------------------------------------
        if spec.jvm_flags:
            ok = _set_boxa_java_opts(
                spec.jvm_flags, args.boxa_ip, args.key_path, args.dry_run
            )
            if not ok:
                log.error("Skipping %s: could not set JVM flags on Box A", spec.id)
                failed.append(spec.id)
                continue
        else:
            # Ensure any previous JVM override is cleared
            _set_boxa_java_opts("", args.boxa_ip, args.key_path, args.dry_run)

        # ----------------------------------------------------------------
        # Step 2: apply bottleneck_config to application.properties
        # ----------------------------------------------------------------
        if spec.app_properties:
            log.info("Applying: %s", spec.app_properties)
            _write_properties_updates(app_props, spec.app_properties)
        else:
            # Pure JVM fixture — restore baseline app properties so nothing
            # from a prior fixture bleeds in.
            _write_properties_updates(app_props, _BASELINE_APP_PROPERTIES)

        # ----------------------------------------------------------------
        # Step 3: commit and push
        # ----------------------------------------------------------------
        commit_msg = (
            f"fixture({spec.id}): {spec.ground_truth} ({spec.severity})\n\n"
            f"Automated capture sweep. Bottleneck: {spec.bottleneck_config}"
        )
        ok, sha_or_err = _commit_and_push(
            workspace,
            app_props,
            commit_msg,
            remote=args.remote,
            branch=args.branch,
            dry_run=args.dry_run,
        )
        if not ok:
            log.error("Skipping %s: deploy failed: %s", spec.id, sha_or_err)
            # Attempt to restore baseline before continuing
            _write_properties_updates(app_props, _BASELINE_APP_PROPERTIES)
            failed.append(spec.id)
            continue
        sha = sha_or_err

        # ----------------------------------------------------------------
        # Step 4: wait for Box A to confirm the commit
        # ----------------------------------------------------------------
        if not _wait_for_commit(
            args.version_url, sha,
            timeout_s=args.version_timeout_s,
            dry_run=args.dry_run,
        ):
            log.error("Skipping %s: Box A did not confirm commit %s", spec.id, sha[:12])
            failed.append(spec.id)
            # Try to restore baseline
            _write_properties_updates(app_props, _BASELINE_APP_PROPERTIES)
            ok2, sha2 = _commit_and_push(
                workspace, app_props,
                "sweep: restore baseline after failed wait",
                remote=args.remote, branch=args.branch, dry_run=args.dry_run,
            )
            if ok2:
                _wait_for_commit(args.version_url, sha2, timeout_s=120, dry_run=args.dry_run)
            continue

        # ----------------------------------------------------------------
        # Step 5: capture through each provider
        # ----------------------------------------------------------------
        capture_ok = True
        for provider in spec.providers:
            ok = _run_capture(
                fixture_id=spec.id,
                provider=provider,
                slo_path=args.slo,
                profile_path=args.profile,
                results_dir=args.results_dir,
                promql_url=args.promql_url,
                promql_instance=args.promql_instance,
                dry_run=args.dry_run,
            )
            if not ok:
                capture_ok = False
                log.error("Capture failed for %s/%s", spec.id, provider)

        if capture_ok:
            if not args.dry_run:
                _mark_validated(spec.path)
            succeeded.append(spec.id)
        else:
            failed.append(spec.id)

        # ----------------------------------------------------------------
        # Step 6: restore baseline before next fixture
        # ----------------------------------------------------------------
        log.info("Restoring baseline after %s", spec.id)
        _write_properties_updates(app_props, _BASELINE_APP_PROPERTIES)
        _set_boxa_java_opts("", args.boxa_ip, args.key_path, args.dry_run)
        ok_b, sha_b = _commit_and_push(
            workspace, app_props,
            f"sweep: restore baseline after {spec.id}",
            remote=args.remote, branch=args.branch, dry_run=args.dry_run,
        )
        if ok_b:
            _wait_for_commit(args.version_url, sha_b, timeout_s=120, dry_run=args.dry_run)

    log.info("=" * 60)
    log.info("Sweep complete. Succeeded: %d  Failed: %d", len(succeeded), len(failed))
    if succeeded:
        log.info("OK: %s", ", ".join(succeeded))
    if failed:
        log.error("FAILED: %s", ", ".join(failed))
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sweep.log", encoding="utf-8"),
        ],
    )
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures-dir", default="config/fixtures", help="Path to fixture YAMLs")
    parser.add_argument("--slo", default="config/slo.yaml", help="SLO YAML path")
    parser.add_argument("--profile", default="config/profiles/spring-boot.yaml", help="Profile YAML path")
    parser.add_argument("--results-dir", default="results", help="Where crucible capture writes snapshots")
    parser.add_argument("--workspace", required=True, help="Local perf-lab checkout (Box B)")
    parser.add_argument("--boxa-ip", default="10.0.0.79", help="Box A private IP")
    parser.add_argument("--key-path", default=os.path.expanduser("~/.ssh/perflab-deploy-key"),
                        help="SSH key for Box A (for JVM-flag fixtures)")
    parser.add_argument("--remote", default="perftest", help="git remote for Box A's bare repo")
    parser.add_argument("--branch", default="perftest_sandbox", help="sandbox branch on Box A")
    parser.add_argument("--version-url", default="http://10.0.0.79:8080/api/version",
                        help="Box A's /api/version endpoint for deploy verification")
    parser.add_argument("--version-timeout-s", type=float, default=300.0,
                        help="How long to wait for Box A to report the commit")
    parser.add_argument("--promql-url", default="http://10.0.0.79:9090",
                        help="Prometheus URL on Box A")
    parser.add_argument("--promql-instance", default="",
                        help="Optional Prometheus instance label filter")
    parser.add_argument("--fixture", default="", help="Capture only this fixture ID")
    parser.add_argument("--resume", action="store_true",
                        help="Skip fixtures that already have validated_at set")
    parser.add_argument("--only-unvalidated", action="store_true",
                        help="Alias for --resume")
    parser.add_argument("--plan", action="store_true",
                        help="Print what would run and exit without capturing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not push or run crucible capture; simulate only")
    args = parser.parse_args()
    sys.exit(sweep(args))


if __name__ == "__main__":
    main()
