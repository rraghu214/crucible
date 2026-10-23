"""The New Test Run wizard's write path: the operator's values into slo.yaml and profile.yaml.

Operator decisions, 23 October 2026: Launch overwrites `config/slo.yaml`, and the
deploy fields of the Target & Repo step are written to the profile. Both files
stay editable by hand; the wizard reads them fresh every time it opens.

Three rules hold the write path to the same standard as a hand edit:

- **Only an operator writes.** The routes need the control token, and these are
  protected paths the agent's guard refuses (DESIGN.md §4.4). Nothing a model
  produced is written unless the operator submitted it in the form.
- **Never mid-campaign.** A campaign holding a deploy-branch lock was started
  against the files as they are; changing its goal under it would judge its
  verdicts against a goal it never ran for.
- **Re-validated before replacing.** The edited text is loaded by the same
  `Sla.load` / `TargetProfile.load` a campaign uses, and every written value is
  read back. A file that would not load, or would load differently, is never
  put in place.

Edits are targeted line replacements, not a YAML re-dump, so the files' comments
-- most of which record why a value is what it is -- survive.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..perf.campaign import CampaignRefused, Sla
from ..perf.deploy import BranchPolicyViolation
from ..perf.loadprofile import locust_tasks, tag_for_endpoint
from ..perf.profile import TargetProfile

MARKER = "# wizard:"


class WizardRefused(ValueError):
    pass


@dataclass(frozen=True)
class WizardForm:
    endpoint: str
    p99_ms: float
    error_rate_pct: float
    users: int
    config_file: str
    base_branch: str
    restart_command: list[str]
    health_url: str
    operator: str = "operator"
    provenance: str = ""


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return json.dumps(value)


_LINE = re.compile(r"^(?P<ind> *)(?P<key>[A-Za-z0-9_.-]+):[ \t]*(?P<val>[^#\r\n]*?)"
                   r"(?P<cmt>[ \t]+#[^\r\n]*)?(?P<nl>\r?\n)?$")


def set_value(text: str, keys: list[str], value: Any) -> str:
    """Replace the value at a nested key path, keeping the line's trailing comment.

    At each level only the block's direct children are considered, so a key of
    the same name deeper down (or in another block) is never the one replaced.
    """
    lines = text.splitlines(keepends=True)
    start, parent = 0, -1
    for depth, key in enumerate(keys):
        child, hit = None, None
        for i in range(start, len(lines)):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip(" "))
            if indent <= parent:
                break
            child = indent if child is None else child
            m = _LINE.match(lines[i])
            if indent == child and m and m.group("key") == key:
                hit = (i, m)
                break
        if hit is None:
            raise WizardRefused(f"cannot find '{'.'.join(keys[:depth + 1])}' to update")
        i, m = hit
        start, parent = i + 1, len(m.group("ind"))
    val = m.group("val").strip()
    if not val or val in ("|", ">", "|-", ">-"):
        raise WizardRefused(f"'{'.'.join(keys)}' is not a single-line value")
    lines[i] = f"{m.group('ind')}{keys[-1]}: {_scalar(value)}{m.group('cmt') or ''}{m.group('nl') or ''}"
    return "".join(lines)


def _stamp(text: str, before: str, note: str) -> str:
    """One `# wizard:` line above `before`, replacing any earlier one."""
    lines = [ln for ln in text.splitlines(keepends=True) if not ln.startswith(MARKER)]
    newline = "\r\n" if "\r\n" in text else "\n"
    for i, line in enumerate(lines):
        if line.startswith(before):
            lines.insert(i, f"{MARKER} {note}{newline}")
            return "".join(lines)
    raise WizardRefused(f"cannot find '{before}' to stamp")


def current(sla_path: Path, profile_path: Path, locustfile: Path) -> dict[str, Any]:
    sla = Sla.load(sla_path)
    profile = TargetProfile.load(profile_path)
    return {
        "endpoint": sla.endpoint, "p99_ms": sla.p99_ms, "error_rate_pct": sla.error_rate_pct,
        "users": int((sla.at_load or {}).get("users", 50)),
        "noise_p99_spread_pct": sla.noise_p99_spread_pct, "noise_measured_on": sla.noise_measured_on,
        "environment": f"{sla.environment_name} ({sla.environment_kind})", "target": sla.target_base_url,
        "profile": profile.name, "config_file": profile.config_file,
        "base_branch": profile.deploy.base_branch, "branch": profile.deploy.branch,
        "remote": profile.deploy.remote, "deploy_mode": profile.deploy.mode,
        "restart_command": list(profile.restart.command), "health_url": profile.restart.health_url,
        "endpoints": [{"endpoint": t["endpoint"], "tag": t["tag"], "family": t["family"]}
                      for t in locust_tasks(locustfile) if t["endpoint"]],
    }


def render(form: WizardForm, sla_path: Path, profile_path: Path, locustfile: Path) -> dict[str, str]:
    """The two files as Launch would write them, validated. Writes nothing."""
    try:
        tag_for_endpoint(form.endpoint, locustfile)
    except ValueError as exc:
        raise WizardRefused(str(exc)) from exc
    if not form.restart_command or not all(isinstance(a, str) and a for a in form.restart_command):
        raise WizardRefused("restart command must be a non-empty list of arguments")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    note = f"written {when} by {form.operator} from the dashboard" + (f"; {form.provenance}" if form.provenance else "")

    old_sla = sla_path.read_text(encoding="utf-8")
    new_sla = old_sla
    for keys, value in ((["objective", "endpoint"], form.endpoint), (["objective", "p99_ms"], form.p99_ms),
                        (["objective", "error_rate_pct"], form.error_rate_pct),
                        (["objective", "at_load", "users"], form.users)):
        new_sla = set_value(new_sla, keys, value)
    new_sla = _stamp(new_sla, "objective:", note)

    old_profile = profile_path.read_text(encoding="utf-8")
    new_profile = old_profile
    for keys, value in ((["config_file"], form.config_file), (["deploy", "base_branch"], form.base_branch),
                        (["restart", "health_url"], form.health_url)):
        new_profile = set_value(new_profile, keys, value)
    new_profile = set_value(new_profile, ["restart", "command"], form.restart_command)
    new_profile = _stamp(new_profile, "name:", note)

    _validate(form, new_sla, new_profile, sla_path, profile_path)
    return {"sla": new_sla, "profile": new_profile,
            "sla_diff": _diff(old_sla, new_sla, sla_path.name),
            "profile_diff": _diff(old_profile, new_profile, profile_path.name)}


def _diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"a/{name}", f"b/{name}", n=1))


def _validate(form: WizardForm, sla_text: str, profile_text: str, sla_path: Path, profile_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s, p = Path(tmp) / sla_path.name, Path(tmp) / profile_path.name
        s.write_text(sla_text, encoding="utf-8")
        p.write_text(profile_text, encoding="utf-8")
        try:
            sla, profile = Sla.load(s), TargetProfile.load(p)
        except (CampaignRefused, BranchPolicyViolation, ValueError, OSError) as exc:
            raise WizardRefused(f"the edited files would not load: {exc}") from exc
    read_back = {
        "endpoint": sla.endpoint, "p99_ms": sla.p99_ms, "error_rate_pct": sla.error_rate_pct,
        "users": (sla.at_load or {}).get("users"), "config_file": profile.config_file,
        "base_branch": profile.deploy.base_branch, "restart_command": list(profile.restart.command),
        "health_url": profile.restart.health_url,
    }
    for key, got in read_back.items():
        if got != getattr(form, key):
            raise WizardRefused(f"{key} would read back as {got!r}, not {getattr(form, key)!r}")


def estimate(*, mode: str, warmup_s: float, measure_s: float, experiments: int, verify_timeout_s: float,
             per_call_costs: list[float], ceiling_steps: int = 0) -> dict[str, Any]:
    """Wall clock and model cost, by phase. Time is the real budget (DESIGN.md §7).

    Deploy time is a range up to the profile's verify timeout, because how long
    a deploy takes is the pipeline's, not ours to assume. Model cost per call is
    the mean over past campaigns' priced diagnoses; with none on disk it is
    reported as not measured rather than invented. Model latency is not added
    to the wall clock: it was never measured either.
    """
    window = warmup_s + measure_s
    per_call = sum(per_call_costs) / len(per_call_costs) if per_call_costs else None
    rows: list[dict[str, Any]] = []
    if mode == "Ceiling Probe":
        rows.append({"phase": f"Load steps (x{ceiling_steps})", "what": f"{warmup_s:.0f} s warmup discarded + "
                     f"{measure_s:.0f} s held, per step; stops at the first step that misses the SLA",
                     "min_s": window, "max_s": window * ceiling_steps, "calls": 0})
    else:
        rows += [
            {"phase": "Baseline", "what": f"{warmup_s:.0f} s warmup discarded + {measure_s:.0f} s measured",
             "min_s": window, "max_s": window, "calls": 0},
            {"phase": f"Diagnosis (x{experiments})", "what": "one model call per experiment",
             "min_s": 0.0, "max_s": 0.0, "calls": experiments},
            {"phase": f"Deploy + verify (x{experiments})", "what": f"push, rebuild, proof the change is in force "
             f"(up to the profile's {verify_timeout_s:.0f} s timeout)", "min_s": 0.0,
             "max_s": verify_timeout_s * experiments, "calls": 0},
            {"phase": f"Re-measure (x{experiments})", "what": "same window after each change",
             "min_s": window, "max_s": window * experiments, "calls": 0},
        ]
    for row in rows:
        row["cost"] = None if per_call is None else row["calls"] * per_call
    calls = sum(r["calls"] for r in rows)
    return {"rows": rows, "min_s": sum(r["min_s"] for r in rows), "max_s": sum(r["max_s"] for r in rows),
            "calls": calls, "cost": None if per_call is None else calls * per_call,
            "per_call_cost": per_call, "priced_calls_on_record": len(per_call_costs)}


def write(rendered: dict[str, str], sla_path: Path, profile_path: Path, active_runs: list[dict[str, Any]]) -> None:
    if active_runs:
        held = ", ".join(str(r.get("run_id", "?")) for r in active_runs)
        raise WizardRefused(f"a campaign is running ({held}); the SLA and profile cannot change under it")
    staged = []
    for path, text in ((sla_path, rendered["sla"]), (profile_path, rendered["profile"])):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        staged.append((Path(tmp), path))
    for tmp, path in staged:
        os.replace(tmp, path)
