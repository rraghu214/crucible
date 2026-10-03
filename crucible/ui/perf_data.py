"""Data layer for the Crucible UI — reads config, results and state, no writes.

Extracted from ``perf_ui.py`` so both the A2UI surface and the NiceGUI app share
one source of truth for what is on disk. A function here reads a file and returns
a typed Python value; it never formats text or builds a component.

Every function takes a ``PerfContext`` that points at the root of the workspace,
so a test can substitute a temporary directory without patching anything global.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PerfContext:
    """Paths a UI screen reads. Relative paths resolve against ``root``."""

    root: Path = field(default_factory=Path.cwd)
    sla_path: str = "config/slo.yaml"
    profile_name: str = "spring-boot"
    profiles_dir: str = "config/profiles"
    results_dir: str = "results"
    fixture_dir: str = "fixtures"
    fixture_config_dir: str = "config/fixtures"
    tasks_dir: str = "config/tasks"
    bench_dir: str = "docs/bench"
    locustfile: str = "locust/locustfile.py"
    replay_results_doc: str = "docs/BENCHMARK_REPLAY_RESULTS.md"
    live_results_doc: str = "docs/BENCHMARK_LIVE_RESULTS.md"
    state_dir: Path | None = None

    def path(self, relative: str) -> Path:
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.root / candidate

    @property
    def state(self) -> Path:
        if self.state_dir is not None:
            return Path(self.state_dir)
        from ..perf.approval import DEFAULT_STATE_DIR

        return Path(os.getenv("CRUCIBLE_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_sla(ctx: PerfContext, path: str | Path | None = None) -> tuple[Any, str]:
    from ..perf.campaign import CampaignRefused, Sla

    try:
        return Sla.load(ctx.path(str(path or ctx.sla_path))), ""
    except (CampaignRefused, OSError, ValueError) as exc:
        return None, str(exc)


def load_profile(ctx: PerfContext, name: str | None = None) -> tuple[Any, str]:
    from ..perf.profile import TargetProfile

    try:
        return TargetProfile.named(name or ctx.profile_name, directory=ctx.path(ctx.profiles_dir)), ""
    except (FileNotFoundError, ValueError) as exc:
        return None, str(exc)


def list_profiles(ctx: PerfContext) -> list[Any]:
    out = []
    for path in sorted(ctx.path(ctx.profiles_dir).glob("*.yaml")):
        profile, _error = load_profile(ctx, path.stem)
        if profile is not None:
            out.append(profile)
    return out


def list_collections(ctx: PerfContext) -> list[tuple[Path, Any, str]]:
    paths = sorted(ctx.path("config").glob("slo*.yaml")) or [ctx.path(ctx.sla_path)]
    return [(p, *load_sla(ctx, p)) for p in paths]


def list_campaigns(ctx: PerfContext) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Campaign manifests in ``results/``, oldest first."""
    from ..perf.report import ReportError, load_campaign

    loaded: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(ctx.path(ctx.results_dir).glob("*.json")):
        try:
            loaded.append(load_campaign(path))
        except ReportError as exc:
            skipped.append((path.name, str(exc)))
    loaded.sort(key=lambda c: float(c.get("started_at_epoch_s") or 0.0))
    return loaded, skipped


def active_runs(ctx: PerfContext) -> list[dict[str, Any]]:
    """Campaigns holding a deploy-branch lock right now."""
    runs = []
    for holder in sorted((ctx.state / "locks").glob("*.lock/holder.json")):
        try:
            data = json.loads(holder.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["branch"] = holder.parent.name.removesuffix(".lock")
        runs.append(data)
    return runs


def latest_campaign(ctx: PerfContext) -> dict[str, Any] | None:
    campaigns, _ = list_campaigns(ctx)
    return campaigns[-1] if campaigns else None


def list_replay_runs(ctx: PerfContext) -> list[dict[str, Any]]:
    runs = []
    # cmd_bench writes to results/replay.json (or results/replay-*.json).
    # Legacy location: docs/bench/replay-*.json. Check both.
    search_dirs = [ctx.path(ctx.results_dir), ctx.path(ctx.bench_dir)]
    seen: set[Path] = set()
    for directory in search_dirs:
        for path in sorted(directory.glob("replay*.json")):
            if path in seen:
                continue
            seen.add(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "cases" in data:
                data["_file"] = path.name
                runs.append(data)
    return runs


def list_fixtures(ctx: PerfContext) -> list[dict[str, Any]]:
    """Fixture YAML metadata (ground truth, providers, validated_at)."""
    import yaml

    out = []
    for path in sorted(ctx.path(ctx.fixture_config_dir).glob("*.yaml")):
        if path.name == "README.md":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "id" in data:
            out.append(data)
    return out


def list_tasks(ctx: PerfContext) -> list[dict[str, Any]]:
    """Task YAML definitions (id, task_class, name, fixtures, asserts_fixture)."""
    import yaml

    out = []
    for path in sorted(ctx.path(ctx.tasks_dir).glob("*.yaml")):
        if path.name == "README.md":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "id" in data:
            out.append(data)
    return out


def locust_tasks(ctx: PerfContext) -> list[dict[str, Any]]:
    """Tasks declared in the locustfile (via AST parse — never imported)."""
    try:
        tree = ast.parse(ctx.path(ctx.locustfile).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    tasks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        tag, weight, is_task = "", 1, False
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "tag" and deco.args:
                tag = str(getattr(deco.args[0], "value", ""))
            if getattr(deco, "id", "") == "task":
                is_task = True
            if isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "task":
                is_task = True
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    weight = int(deco.args[0].value)
        if not is_task:
            continue
        endpoint = ""
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "_get":
                named = next((k.value for k in call.keywords if k.arg == "name"), None)
                if isinstance(named, ast.Constant):
                    endpoint = str(named.value)
                elif call.args and isinstance(call.args[0], ast.Constant):
                    endpoint = str(call.args[0].value)
                break
        family = (ast.get_docstring(node) or "").split(".")[0].strip()
        tasks.append({"tag": tag, "endpoint": endpoint, "weight": weight, "family": family})
    return tasks


def git_version(ctx: PerfContext, relative: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "log", "-1", "--format=%h %cs", "--", relative],
            cwd=ctx.root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def claim(ctx: PerfContext, relative: str) -> str:
    """First blockquote in a results doc."""
    try:
        lines = ctx.path(relative).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    block: list[str] = []
    for line in lines:
        if line.startswith(">"):
            block.append(line.lstrip("> ").rstrip())
        elif block:
            break
    return " ".join(part for part in block if part)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_when(epoch: float | None) -> str:
    if not epoch:
        return "—"
    import datetime

    dt = datetime.datetime.fromtimestamp(float(epoch))
    now = datetime.datetime.now()
    delta = now - dt
    if delta.days >= 7:
        return dt.strftime("%d %b")
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    minutes = delta.seconds // 60
    return f"{minutes}m ago"


def fmt_minutes(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m {s:02d}s" if m else f"{s}s"


def campaign_verdict_counts(campaign: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for exp in campaign.get("experiments") or []:
        v = exp.get("verdict", "?")
        counts[v] = counts.get(v, 0) + 1
    return counts
