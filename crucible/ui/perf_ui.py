"""The nineteen Crucible screens, as validated A2UI surfaces.

``docs/crucible-screens-v2.html`` is the design source: its screens, its order,
its seven sidebar groups and its information hierarchy. This module renders that
structure from what Crucible actually has on disk -- ``config/``, ``fixtures/``,
``results/``, the state directory and ``docs/bench/`` -- through the same
declarative pattern as ``surface.py``: a flat component list linked by id, a data
model the components bind into, and :func:`validator.validate_surface` run over
every screen before it is served, exactly as it is over an untrusted agent's.

Three rules, each inherited rather than invented here:

**Nothing shown is invented (principle 1).** The design file is a mock-up. Its
figures -- ``payments-api``, "p99 1,300 -> 93 ms", "38 / 40" -- are examples of
what a screen holds, not data. Where Crucible has no source for a number the
design shows, the screen says *not measured* or *not built* and names why. It
never shows the mock-up's example, and never a zero standing in for "nobody
looked" (section 4.2's null-versus-zero rule, applied to the UI).

**Absence is declared (principle 2).** Screens 13-15 need a running campaign.
With none running they say so and point at the command that starts one, rather
than drawing an empty frame that reads as "all clear".

**The action set is not widened.** The catalog's closed set is
``approve / reject / rerun / request_data`` (``catalog.REGISTERED_ACTIONS``).
Approve and Skip on a parked proposal use ``approve`` / ``reject`` and are bound
to the parked request's exact parameters through ``hitl.decide_resume`` -- the
binding is the safety, as it is for the CLI. Read-only probes ("Test all",
"Run checks") use ``request_data``. Abort and Pause are shown as the CLI command
that does them: a browser Abort button needs a new registered action, which
widens the event invariant for *every* surface an agent can compose, and that is
the operator's decision to take, not this module's.

``DESIGN.md`` section 15 scoped UI to the campaign path only (setup -> live ->
report) and left the rest to the CLI. The week-4 brief asked for all nineteen.
They are built as read-mostly views over the same functions the CLI calls, so
the scope line that matters -- one implementation of each capability -- holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..auth import require_control
from . import actions as _actions
from .actions import ActionError
from .hitl import PendingAction, decide_resume
from .jobs import Job, get_job, list_jobs, read_output, start_job
from .knowledge import MAX_CHARS, KnowledgeBase, UploadRefused, default_embedder
from .validator import validate_surface

# ---------------------------------------------------------------------------
# Context: where every screen reads from
# ---------------------------------------------------------------------------


@dataclass
class PerfContext:
    """Every path a screen reads. Relative paths resolve against ``root``.

    One object rather than module constants so a test can point the whole UI at
    a temporary directory -- and so nothing here can quietly read a path the CLI
    would not.
    """

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
# A small builder for flat, id-linked surfaces
# ---------------------------------------------------------------------------


class _Surface:
    """Collects components and the data model they bind into.

    Every user-visible value goes into the data model and is bound, never
    written into a component property: a literal property is checked for markup
    by the validator, but keeping data out of structure is the rule surface.py
    states ("no value the surface shows is ever code") and it is simpler to keep
    than to reason about case by case.
    """

    def __init__(self, title: str, subtitle: str) -> None:
        self.components: list[dict[str, Any]] = []
        self.dm: dict[str, Any] = {}
        self._root: list[str] = []
        self._n = 0
        self.add(self.text(title, "heading"), self.text(subtitle, "subtitle"))

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def _bind(self, value: Any) -> dict[str, str]:
        key = self._id("v")
        self.dm[key] = value
        return {"$bind": f"/{key}"}

    def _put(self, component: dict[str, Any]) -> str:
        self.components.append(component)
        return component["id"]

    def add(self, *ids: str) -> None:
        self._root.extend(ids)

    # -- leaves -------------------------------------------------------------

    def text(self, value: Any, variant: str = "body") -> str:
        return self._put({"id": self._id("t"), "type": "Text", "variant": variant, "text": self._bind(str(value))})

    def terminal(self, lines: list[str] | str) -> str:
        """A literal, monospaced block. The perf client renders ``caption`` as
        pre-wrapped text with no markdown, which is what a terminal shows."""
        body = lines if isinstance(lines, str) else "\n".join(lines)
        return self.text(body, "caption")

    def notice(self, value: str, tone: str = "neutral") -> str:
        return self._put({"id": self._id("n"), "type": "Notice", "text": self._bind(value), "tone": tone})

    def stat(self, label: str, value: Any, *, unit: str = "", tone: str = "neutral", note: str = "") -> str:
        component = {"id": self._id("s"), "type": "StatTile", "label": label, "value": self._bind(str(value)),
                     "tone": tone, "delta": self._bind(note)}
        if unit:
            component["unit"] = unit
        return self._put(component)

    def table(self, columns: list[str], rows: list[dict[str, Any]], *, sortable: bool = False,
              filter_key: str = "") -> str:
        component: dict[str, Any] = {
            "id": self._id("dt"), "type": "DataTable", "columns": ",".join(columns),
            "rows": self._bind([{c: _cell(r.get(c)) for c in columns} for r in rows]),
            "sortable": sortable,
        }
        if filter_key:
            component["filterKey"] = filter_key
        return self._put(component)

    def bar_chart(self, title: str, data: list[dict[str, Any]], x: str, y: str) -> str:
        return self._put({"id": self._id("bc"), "type": "BarChart", "title": title, "data": self._bind(data),
                          "xKey": x, "yKey": y})

    def sparkline(self, values: list[float], tone: str = "neutral") -> str:
        return self._put({"id": self._id("sp"), "type": "Sparkline", "data": self._bind(values), "tone": tone})

    def button(self, label: str, action: str, args: dict[str, Any] | None = None) -> str:
        return self._put({"id": self._id("b"), "type": "Button", "label": label,
                          "onPress": {"action": action, "args": args or {}}})

    def choice(self, label: str, options: list[str], value: str, key: str) -> str:
        """An input whose value lives at ``/<key>`` so a button can read it back."""
        self.dm[key] = value
        return self._put({"id": self._id("ic"), "type": "InputChoice", "label": label,
                          "options": self._bind(options), "value": {"$bind": f"/{key}"}})

    def approval(self, summary: str, params: dict[str, Any], *, run_id: str, experiment: int) -> str:
        """The card is bound to the parked params; confirm sends those same params.

        The server re-checks them against the request file with
        ``hitl.decide_resume`` and then writes the decision with the params
        copied from the request, never from the client (approval.write_decision).
        """
        key = self._id("params")
        self.dm[key] = params
        return self._put({
            "id": self._id("ap"), "type": "ApprovalCard", "summary": self._bind(summary),
            "params": {"$bind": f"/{key}"},
            "confirm": {"action": "approve", "args": {"$bind": f"/{key}"}},
            "reject": {"action": "reject", "args": {"run_id": run_id, "experiment": experiment}},
        })

    # -- containers ---------------------------------------------------------

    def card(self, title: str, children: list[str]) -> str:
        return self._put({"id": self._id("c"), "type": "Card", "title": title, "children": list(children)})

    def row(self, children: list[str]) -> str:
        return self._put({"id": self._id("r"), "type": "Row", "align": "start", "justify": "start",
                          "children": list(children)})

    def column(self, children: list[str]) -> str:
        return self._put({"id": self._id("col"), "type": "Column", "children": list(children)})

    def tabs(self, labels: list[str], panels: list[str]) -> str:
        return self._put({"id": self._id("tb"), "type": "Tabs", "labels": ",".join(labels),
                          "children": list(panels)})

    def done(self) -> dict[str, Any]:
        root = {"id": "root", "type": "Column", "children": self._root}
        return {"root": "root", "components": [root, *self.components], "dataModel": self.dm}


def _cell(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value)}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(v) for v in value) or "--"
    return str(value)


def _when(epoch: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(epoch)))
    except (TypeError, ValueError):
        return "--"


def _minutes(seconds: float) -> str:
    minutes = seconds / 60.0
    return f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60.0:.1f} hr"


# ---------------------------------------------------------------------------
# Loaders. Each returns (value, error) so a screen can say what failed.
# ---------------------------------------------------------------------------


def _load_sla(ctx: PerfContext, path: str | Path | None = None) -> tuple[Any, str]:
    from ..perf.campaign import CampaignRefused, Sla

    try:
        return Sla.load(ctx.path(str(path or ctx.sla_path))), ""
    except (CampaignRefused, OSError, ValueError) as exc:
        return None, str(exc)


def _load_profile(ctx: PerfContext, name: str | None = None) -> tuple[Any, str]:
    from ..perf.profile import TargetProfile

    try:
        return TargetProfile.named(name or ctx.profile_name, directory=ctx.path(ctx.profiles_dir)), ""
    except (FileNotFoundError, ValueError) as exc:
        return None, str(exc)


def _profiles(ctx: PerfContext) -> list[Any]:
    out = []
    for path in sorted(ctx.path(ctx.profiles_dir).glob("*.yaml")):
        profile, _error = _load_profile(ctx, path.stem)
        if profile is not None:
            out.append(profile)
    return out


def _collections(ctx: PerfContext) -> list[tuple[Path, Any, str]]:
    """Every SLA file under ``config/``. An SLA is a saved investigation."""
    paths = sorted(ctx.path("config").glob("slo*.yaml")) or [ctx.path(ctx.sla_path)]
    return [(p, *_load_sla(ctx, p)) for p in paths]


def _campaigns(ctx: PerfContext) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Campaign manifests in ``results/``, oldest first, and the files skipped."""
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


def _active_runs(ctx: PerfContext) -> list[dict[str, Any]]:
    """Campaigns holding a deploy-branch lock right now (campaign.BranchLock)."""
    runs = []
    for holder in sorted((ctx.state / "locks").glob("*.lock/holder.json")):
        try:
            data = json.loads(holder.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["branch"] = holder.parent.name.removesuffix(".lock")
        runs.append(data)
    return runs


def _knowledge_summary(ctx: PerfContext) -> str:
    # Listing makes no embed call, but the store must be opened with the corpus's
    # own embedder: any other one marks every vector stale and empties memory.faiss.
    documents = KnowledgeBase.under(ctx.state, default_embedder()).documents()
    if not documents:
        return "none indexed"
    return f"{len(documents)} indexed: " + ", ".join(f"{d['filename']} ({d['chunks']} chunks)" for d in documents)


def _pricing() -> Any:
    from ..economics.pricing import Pricing
    from ..perf.scorer import _load_pricing_yaml

    return Pricing.from_mapping(_load_pricing_yaml())


def _campaign_cost(campaign: dict[str, Any], pricing: Any) -> float:
    from ..perf.scorer import score_cost

    return score_cost(campaign, pricing).total


def _replay_runs(ctx: PerfContext) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(ctx.path(ctx.bench_dir).glob("replay-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "cases" in data:
            data["_file"] = path.name
            runs.append(data)
    return runs


def _git_version(ctx: PerfContext, relative: str) -> str:
    """The last commit that touched a file: the only version slo.yaml has."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "log", "-1", "--format=%h %cs", "--", relative],
            cwd=ctx.root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _locust_tasks(ctx: PerfContext) -> list[dict[str, Any]]:
    """The load profile's tasks, read from the locustfile's source, never run."""
    from ..perf.loadprofile import locust_tasks

    return locust_tasks(ctx.path(ctx.locustfile))


def _claim(ctx: PerfContext, relative: str) -> str:
    """The first blockquote in a results document: the claim, verbatim."""
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
# Screens 1-4 -- Structure
# ---------------------------------------------------------------------------


def screen_home(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Home", "One install, several services. Each service holds one or more collections.")
    collections = _collections(ctx)
    campaigns, _skipped = _campaigns(ctx)
    active = _active_runs(ctx)
    profile, _ = _load_profile(ctx)

    services: dict[str, dict[str, Any]] = {}
    for path, sla, error in collections:
        name = sla.environment_name if sla else f"(unreadable: {path.name})"
        entry = services.setdefault(name, {"collections": [], "error": error})
        if sla:
            entry["collections"].append(f"{sla.name} ({sla.endpoint})")
    rows = []
    for name, entry in services.items():
        status = f"campaign running: {active[0]['run_id']}" if active else (
            f"{len(campaigns)} campaign(s) on record" if campaigns else "no campaigns on record")
        rows.append({
            "service": name,
            "runtime": f"{profile.name} ({profile.runtime})" if profile else "profile not loadable",
            "telemetry": "actuator",
            "collections": entry["collections"] or entry["error"],
            "status": status,
        })
    s.add(s.card("Services", [
        s.text(f"{len(services)} configured, {len(active)} campaign(s) running. A service is the "
               "environment an SLA names; its runtime is the CLI's profile (the SLA does not name one)."),
        s.table(["service", "runtime", "telemetry", "collections", "status"], rows),
    ]))

    tree = ["Install"]
    for name, entry in services.items():
        tree.append(f"+- {name}  -- runtime . telemetry . git . restart . guardrails")
        if len(entry["collections"]) > 1:
            tree.extend(f"   +- {c}  -- endpoint . SLA . scenarios . plan . hooks" for c in entry["collections"])
        else:
            tree.append(f"   investigating: {', '.join(entry['collections']) or '(none)'}")
    s.add(s.card("The shape of it", [
        s.terminal(tree),
        s.text("Progressive disclosure (DESIGN.md section 2): with one investigation per service the "
               "collection layer is not shown. It appears when a second SLA is saved under config/."),
    ]))

    kept = sum(1 for c in campaigns for e in c.get("experiments", []) if e.get("kept"))
    pricing = _pricing()
    spend = sum(_campaign_cost(c, pricing) for c in campaigns)
    s.add(s.card("Across all services", [s.row([
        s.stat("campaigns", len(campaigns), note=f"manifests in {ctx.results_dir}/"),
        s.stat("changes kept", kept),
        s.stat("total spend", f"${spend:.4f}", note="priced from recorded tokens"),
        s.stat("playbooks", "not built", tone="warn",
               note="Playbook memory and promotion are not implemented"),
    ])]))
    return s.done()


def screen_service(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    sla, sla_error = _load_sla(ctx)
    profile, profile_error = _load_profile(ctx)
    name = sla.environment_name if sla else "service"
    s = _Surface("Service settings", f"Everything true about {name} regardless of what you are "
                 "investigating. Shared by every collection under it.")
    if not sla or not profile:
        s.add(s.notice(f"Cannot load the service: {sla_error or profile_error}", "bad"))
        return s.done()

    deploy, restart = profile.deploy, profile.restart
    rows = [
        {"setting": "runtime profile", "value": f"{profile.name} ({profile.runtime})", "from": str(profile.source_path)},
        {"setting": "telemetry", "value": f"actuator @ {sla.target_base_url}",
         "from": "build_measure (the only provider campaigns read)"},
        {"setting": "traces", "value": "none unless `crucible run --jaeger-url` is given", "from": "per run"},
        {"setting": "git / deploy", "value": f"{deploy.mode}: {deploy.remote or '(unset)'} -> "
                                              f"{deploy.branch or '(unset)'} (from {deploy.base_branch or '(unset)'})",
         "from": "profile deploy"},
        {"setting": "restart", "value": "manual" if restart.manual else " ".join(restart.command) or "(none)",
         "from": "profile restart"},
        {"setting": "tunable properties", "value": len(profile.allowed_properties), "from": "profile"},
        {"setting": "protected paths", "value": len(profile.protected_paths), "from": "profile"},
        {"setting": "noise floor", "value": f"{sla.noise_p99_spread_pct:.2f}% (measured {sla.noise_measured_on})",
         "from": sla.noise_source or "slo.yaml"},
        {"setting": "cpu steal abort", "value": f"{sla.cpu_steal_abort_pct:.1f}%", "from": "slo.yaml"},
        {"setting": "knowledge documents", "value": _knowledge_summary(ctx),
         "from": "uploaded in New Test Run; searched by the Live AI chat"},
    ]
    s.add(s.card("Shared configuration", [s.table(["setting", "value", "from"], rows)]))

    fallbacks = _env("CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS")
    model = _env("CRUCIBLE_MODEL") or "(not pinned in env -- pass --model per run)"
    scope = [
        {"level": "global (env)", "value": f"{_env('GLC_BASE_URL') or 'http://127.0.0.1:8111'} . "
                                           f"{_env('CRUCIBLE_GATEWAY_PROVIDER') or 'gemini'} . {model}",
         "applies": "default"},
        {"level": "service", "value": "no service-level override exists", "applies": "--"},
        {"level": "collection", "value": "no collection-level override exists", "applies": "--"},
        {"level": "run override", "value": "`crucible run --provider --model`", "applies": "wins when given"},
    ]
    children = [s.table(["level", "value", "applies"], scope)]
    if fallbacks:
        children.append(s.notice(f"CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS is set ({fallbacks}). AGENTS.md "
                                 "non-negotiable 3: it stays empty -- no cross-provider failover, ever.", "bad"))
    else:
        children.append(s.notice("Cross-provider failover is disabled (fallback list empty). What actually "
                                 "served each call is read off the gateway response and recorded.", "good"))
    s.add(s.card("Model provider -- nearest scope wins", children))

    s.add(s.card("Collections under this service", [s.table(
        ["collection", "endpoint", "goal"],
        [{"collection": c.name, "endpoint": c.endpoint, "goal": f"p99 {c.p99_ms:.0f} ms, errors {c.error_rate_pct}%"}
         for _p, c, _e in _collections(ctx) if c is not None and c.environment_name == sla.environment_name],
    )]))
    return s.done()


def screen_collection(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    sla, error = _load_sla(ctx)
    s = _Surface("Collection", "One saved investigation. Endpoint, goal, scenarios, hooks. Exports as a file "
                 "you commit next to the code.")
    if not sla:
        s.add(s.notice(f"The SLA does not load: {error}", "bad"))
        return s.done()
    load = sla.at_load or {}
    version = _git_version(ctx, ctx.sla_path) or "uncommitted"
    s.add(s.card(sla.name, [s.table(["field", "value"], [
        {"field": "endpoint", "value": sla.endpoint},
        {"field": "goal", "value": f"p99 <= {sla.p99_ms:.0f} ms . error rate <= {sla.error_rate_pct}%"},
        {"field": "environment", "value": f"{sla.environment_name} [{sla.environment_kind}]"},
        {"field": "load", "value": f"{load.get('users', '?')} users . spawn {load.get('spawn_rate', '?')}/s . "
                                   f"think {load.get('think_time_s', '?')} s"},
        {"field": "scenarios", "value": "one: the SLA's at_load (no scenario list is declared)"},
        {"field": "plan", "value": "5 experiments (crucible run default) . $0.05 budget (config/budgets.yaml)"},
        {"field": "slo.yaml", "value": f"last commit {version}"},
    ])]))
    s.add(s.card("Hooks", [s.notice(
        "Not implemented. DESIGN.md section 10 specifies before_each / after_each / on_abort, and that a "
        "failing hook blocks the experiment. No code reads them yet, so no hook runs -- including the "
        "database reset that would keep experiment 3 comparable with experiment 1. PerfLab's scenarios "
        "are read-only, which is why that has not bitten.", "warn")]))
    s.add(s.card("Config scope for this collection", [s.table(["level", "experiment ceiling", "applies"], [
        {"level": "global", "experiment ceiling": "5 (`crucible run --experiments`)", "applies": "in effect"},
        {"level": "service", "experiment ceiling": "no override mechanism", "applies": "--"},
        {"level": "collection", "experiment ceiling": "no override mechanism", "applies": "--"},
    ])]))
    export = [
        f"# {ctx.sla_path} -- environments and credentials are NOT exported",
        f"collection: {sla.name}",
        f"endpoint: {sla.endpoint}",
        f"goal: {{p99_ms: {sla.p99_ms:.0f}, error_rate_pct: {sla.error_rate_pct}}}",
        f"at_load: {json.dumps(load)}",
        f"noise: {{p99_spread_pct: {sla.noise_p99_spread_pct}, measured_on: {sla.noise_measured_on}}}",
    ]
    s.add(s.card("Export", [s.terminal(export), s.text(
        "The whole file is served read-only at /v1/perf/slo.yaml. It is protected against the agent "
        "twice (profile protected path, and Policy memory) -- reading it is allowed, writing is not.")]))
    return s.done()


def screen_environments(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.campaign import CampaignRefused, check_environment

    s = _Surface("Environments", "The same collection, pointed at a different instance. Credentials live "
                 "here and are never exported.")
    rows = []
    for _path, sla, error in _collections(ctx):
        if sla is None:
            rows.append({"environment": "(unreadable)", "kind": error})
            continue
        try:
            check_environment(sla)
            accepted = "runnable"
        except CampaignRefused as refused:
            accepted = f"REFUSED: {refused}"
        rows.append({
            "environment": sla.environment_name, "kind": sla.environment_kind, "base URL": sla.target_base_url,
            "auth": "none declared", "instances": "1 (Actuator pins load to one instance)",
            "campaigns": accepted,
        })
    s.add(s.card("Environments", [s.table(["environment", "kind", "base URL", "auth", "instances", "campaigns"], rows)]))
    campaigns, _ = _campaigns(ctx)
    seen = sorted({(c.get("sla") or {}).get("environment_name", "?") for c in campaigns})
    children = [s.text("A campaign records its environment on the manifest, and `crucible diff` refuses a "
                       "comparison across environments rather than printing a delta (DESIGN.md section 8).")]
    if len(seen) > 1:
        children.append(s.notice(f"Campaigns on record span {len(seen)} environments ({', '.join(seen)}). "
                                 "Their results are not comparable with each other.", "warn"))
    s.add(s.card("Why results are not comparable across environments", children))
    return s.done()


# ---------------------------------------------------------------------------
# Screens 5-10 -- Getting set up
# ---------------------------------------------------------------------------


def screen_profile(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Target profile", "One YAML grants authority. One SKILL.md teaches the model how this runtime "
                 "fails. They never mix.")
    profiles = _profiles(ctx)
    chosen = next((p for p in profiles if p.name == q.get("profile")), None) or \
        next((p for p in profiles if p.name == ctx.profile_name), None)
    if chosen is None:
        s.add(s.notice("No profile loads from config/profiles/.", "bad"))
        return s.done()
    s.add(s.card("Runtime", [
        s.row([s.button(f"{p.name} ({p.runtime})", "request_data", {"profile": p.name}) for p in profiles]),
        s.text(f"Showing {chosen.name}: {chosen.source_path}  .  skill {chosen.skill_file or '(none)'}"),
    ]))
    every = sorted({f for p in profiles for f in p.cause_families})
    s.add(s.card("Cause families -- a vocabulary, not a closed set", [
        s.table(["family", "on this runtime"], [
            {"family": f, "on this runtime": "declared" if f in chosen.cause_families else "not valid here"}
            for f in every]),
        s.text("A family another runtime declares is not offered here. An undeclared cause may still be "
               "named and is recorded as novel (DESIGN.md section 5); naming one grants no property."),
    ]))
    prop_rows = []
    for prop in sorted(chosen.allowed_properties):
        b = chosen.allowed_properties[prop]
        span = "one of " + ", ".join(map(str, b.allowed_values)) if b.allowed_values else (
            f"{_cell(b.minimum)} - {_cell(b.maximum)}" if b.minimum is not None or b.maximum is not None else "any")
        prop_rows.append({"property": prop, "type": b.kind, "bounds": span})
    s.add(s.card("Tunable -- authority granted, from the profile", [s.table(["property", "type", "bounds"], prop_rows)]))
    restart, deploy = chosen.restart, chosen.deploy
    s.add(s.card("Operational contract", [s.table(["contract", "value"], [
        {"contract": "config lives in", "value": chosen.config_file},
        {"contract": "restart", "value": "manual" if restart.manual else " ".join(restart.command) or "(none)"},
        {"contract": "health", "value": restart.health_url or "(unset)"},
        {"contract": "health timeout", "value": f"{restart.health_timeout_s:.0f} s"},
        {"contract": "deploy", "value": f"{deploy.mode} -> {deploy.branch or '(unset)'}"},
        {"contract": "proof the change is running", "value": deploy.version_url or "(none: unverified rung)"},
        {"contract": "protected paths", "value": ", ".join(chosen.protected_paths)},
    ])]))
    s.add(s.card("Database between experiments", [s.text(
        "No reset hook exists (screen 3). PerfLab's scenarios are read-only, so none is needed there; a "
        "writing scenario would need one before its experiments are comparable.")]))
    return s.done()


def _telemetry_rows(profile: Any) -> list[dict[str, Any]]:
    rows = []
    timers = set(profile.window_timers)
    for role, meter in {**profile.snapshot_metrics, **profile.gauges}.items():
        promql = profile.promql.get(meter)
        datadog = profile.datadog.get(meter)
        if meter in timers:
            unit = "seconds -> ms (window delta)"
        elif role in profile.gauges:
            unit = "gauge, sampled during load"
        else:
            unit = "as reported"
        rows.append({
            "canonical": role, "actuator": meter,
            "promql": (promql.get("count") or promql.get("value")) if isinstance(promql, dict) else promql,
            "datadog": ((datadog.get("count") or datadog.get("value")) + f" ({datadog.get('unit', 'raw')})")
            if isinstance(datadog, dict) else datadog,
            "unit": unit,
        })
    return rows


def screen_telemetry(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Telemetry", "The agent asks for canonical metric names. The adapter turns them into your "
                 "provider's queries and converts the units.")
    profile, error = _load_profile(ctx)
    sla, _ = _load_sla(ctx)
    if profile is None:
        s.add(s.notice(error, "bad"))
        return s.done()
    s.add(s.card("Provider", [s.table(["provider", "adapter", "used by campaigns", "status"], [
        {"provider": "actuator", "adapter": "yes", "used by campaigns": "yes", "status": "the one provider measured"},
        {"provider": "promql", "adapter": "yes, unit tested", "used by campaigns": "no",
         "status": "build_measure does not construct it; Prometheus not running on Box A"},
        {"provider": "datadog", "adapter": "yes, unit tested", "used by campaigns": "no",
         "status": "build_measure does not construct it; no credentials"},
    ]), s.text("One PromQL adapter covers Prometheus, Mimir, Thanos, VictoriaMetrics and the managed "
               "Prometheus services. Capture refuses a provider the measurement does not read.")]))

    rows = _telemetry_rows(profile)
    probe_note = "Not probed. Test all reads each meter from the target once."
    if q.get("probe") and sla is not None:
        rows, probe_note = _probe_metrics(profile, sla, rows)
    s.add(s.card("Metric mapping", [
        s.table(["canonical", "actuator", "promql", "datadog", "unit"] + (["probe"] if q.get("probe") else []), rows),
        s.row([s.button("Test all", "request_data", {"probe": "1"})]),
        s.text(probe_note),
    ]))
    s.add(s.card("What this provider can answer", [s.table(["evidence", "available", "meaning"], [
        {"evidence": "metrics", "available": True, "meaning": "units normalised, gauges sampled during load"},
        {"evidence": "traces", "available": False,
         "meaning": "downstream is reported unchecked, never eliminated; sampling rate null, not 100"},
        {"evidence": "endpoint breakdown", "available": True, "meaning": "http.server.requests by uri"},
        {"evidence": "thread meters", "available": False,
         "meaning": "mapped but never sampled (docs/ref/DEBT.md); thread_pool_saturation cannot be confirmed"},
        {"evidence": "service-wide percentiles", "available": False,
         "meaning": "single instance: Actuator percentiles cannot be combined across instances"},
    ])]))
    return s.done()


def _probe_metrics(profile: Any, sla: Any, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """One read per meter, stopping at the first sign there is no route at all."""
    from ..perf.providers import ActuatorMetricsProvider
    from ..perf.watchdog import probe_http

    reachable, status = probe_http(sla.target_base_url.rstrip("/") + "/actuator/health", timeout_s=3.0)
    if not reachable:
        for row in rows:
            row["probe"] = "not reached"
        return rows, (f"{sla.target_base_url} did not answer /actuator/health (status {status}); no meter was "
                      "read. From a machine without a route to the target that is the expected answer.")
    provider = ActuatorMetricsProvider(sla.target_base_url, timeout_s=3.0)
    ok = 0
    try:
        for row in rows:
            try:
                got = provider.fetch(row["actuator"])
            except Exception:  # noqa: BLE001 - a failed read is the answer
                got = None
            row["probe"] = "ok" if got else "no data"
            ok += bool(got)
    finally:
        provider.close()
    return rows, f"{ok} of {len(rows)} meters returned data from {sla.target_base_url}."


def screen_setup(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Setup overview", "Four required. Everything else buys the agent a capability, and says so.")
    sla, sla_error = _load_sla(ctx)
    profile, profile_error = _load_profile(ctx)
    load = (sla.at_load or {}) if sla else {}
    required = [
        {"item": "target", "state": "set" if sla and sla.target_base_url else "missing",
         "detail": sla.target_base_url if sla else sla_error},
        {"item": "goal and load", "state": "set" if sla else "missing",
         "detail": f"p99 <= {sla.p99_ms:.0f} ms . {load.get('users', '?')} users" if sla else sla_error},
        {"item": "runtime profile", "state": "set" if profile else "missing",
         "detail": f"{profile.name} . {len(profile.allowed_properties)} tunable" if profile else profile_error},
        {"item": "metrics", "state": "set" if sla else "missing",
         "detail": "Actuator + Micrometer; reachability is checked by Preflight, not here"},
    ]
    s.add(s.card("Required", [s.table(["item", "state", "detail"], required)]))
    deploy = profile.deploy if profile else None
    optional = [
        {"item": "git repository", "state": "connected" if deploy and deploy.remote else "not connected",
         "buys": "commits each experiment to the sandbox branch and records the sha it ran against"},
        {"item": "traces", "state": "not connected", "buys": "rule out a slow downstream call instead of "
                                                               "declaring it unchecked"},
        {"item": "CI/CD", "state": f"{deploy.mode}" if deploy else "--",
         "buys": "verifies against a real deploy; the target must prove the commit (DESIGN.md 19.6)"},
        {"item": "knowledge", "state": "not built", "buys": "stops the agent blaming the app for a gateway throttle"},
        {"item": "production read", "state": "not built", "buys": "shapes load from real traffic, not a guess"},
    ]
    s.add(s.card("Optional -- each adds something the agent can do", [s.table(["item", "state", "buys"], optional)]))
    can = [
        ("diagnose from metrics, units normalised, gauges sampled during load", bool(sla and profile)),
        (f"propose changes to {len(profile.allowed_properties) if profile else 0} properties, inside bounds",
         bool(profile)),
        ("verify by re-measuring, then keep or revert on the measurement", bool(sla and profile)),
        ("check downstream calls", False),
    ]
    s.add(s.card("With this setup the agent can", [s.table(["capability", "yes"],
                                                           [{"capability": c, "yes": ok} for c, ok in can])]))
    return s.done()


def screen_requirements(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Requirements", "Building the goal before anything runs. Every number keeps its source. "
                 "Produces slo.yaml.")
    sla, error = _load_sla(ctx)
    s.add(s.card("Evidence -- add what you have", [s.table(["source", "state"], [
        {"source": "documents", "state": "not built"},
        {"source": "spreadsheet", "state": "not built"},
        {"source": "production read", "state": "not built"},
        {"source": "your answers", "state": f"{ctx.sla_path}, written by hand; `crucible init` refuses to "
                                           "generate one (a threshold nobody chose is a goalpost that arrived "
                                           "by accident)"},
    ])]))
    if sla is None:
        s.add(s.notice(error, "bad"))
        return s.done()
    load = sla.at_load or {}
    rows = [
        {"field": "p99 gate", "value": f"{sla.p99_ms:.0f} ms", "source": f"operator, {ctx.sla_path}"},
        {"field": "error rate", "value": f"<= {sla.error_rate_pct}%", "source": f"operator, {ctx.sla_path}"},
        {"field": "load", "value": f"{load.get('users', '?')} users, think {load.get('think_time_s', '?')} s",
         "source": f"operator, {ctx.sla_path}"},
        {"field": "noise floor", "value": f"{sla.noise_p99_spread_pct:.2f}%",
         "source": f"{sla.noise_source} ({sla.noise_measured_on})"},
        {"field": "cpu steal abort", "value": f"{sla.cpu_steal_abort_pct:.1f}%", "source": "slo.yaml rationale"},
    ]
    s.add(s.card("Draft -- values resolved", [s.table(["field", "value", "source"], rows)]))
    s.add(s.card("Unresolved -- the agent will not guess these", [s.table(["value", "state"], [
        {"value": "stretch target", "state": "not declared; the SLA is a single gate, not a band"},
        {"value": "SLA floor", "state": "not declared"},
        {"value": "scenarios beyond at_load", "state": "not declared"},
    ]), s.text("There is no store for open questions yet; these are read off the fields the SLA does not have.")]))
    s.add(s.card("Template", [s.text("The current slo.yaml is served read-only at /v1/perf/slo.yaml. The "
                                     "requirements chat is not built.")]))
    return s.done()


def screen_scenarios(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.fixtures import DEFAULT_MEASURE_S, DEFAULT_WARMUP_S

    s = _Surface("Scenarios", "Driven by slo.yaml. Every edit is a new commit, and each campaign records "
                 "the SLA it ran against.")
    sla, error = _load_sla(ctx)
    if sla is None:
        s.add(s.notice(error, "bad"))
        return s.done()
    s.add(s.card("Latency goal", [s.table(["band", "value"], [
        {"band": "stretch", "value": "not declared"},
        {"band": "floor", "value": "not declared"},
        {"band": "hard gate", "value": f"p99 <= {sla.p99_ms:.0f} ms"},
    ]), s.text("A band needs three numbers; this SLA declares one. Nothing is inferred for the other two.")]))
    tasks = _locust_tasks(ctx)
    if tasks:
        s.add(s.card("Traffic mix -- one tag per cause family, one tag per scenario", [
            s.bar_chart("task weight by tag", [{"tag": t["tag"], "weight": t["weight"]} for t in tasks], "tag", "weight"),
            s.table(["tag", "endpoint", "weight", "family"], tasks),
            s.text("Run one tag at a time. An untagged run measured a blend on Box A in which /api/downstream "
                   "owned the p99 while /api/db sat at a ninth of the traffic."),
        ]))
    load = sla.at_load or {}
    per = DEFAULT_WARMUP_S + DEFAULT_MEASURE_S
    s.add(s.card("Scenarios", [s.table(["scenario", "tags", "load", "window", "repeats"], [
        {"scenario": "db-latency (crucible run)", "tags": "db",
         "load": f"{load.get('users', 50)} users", "window": f"{DEFAULT_WARMUP_S:.0f} s discarded + "
                                                            f"{DEFAULT_MEASURE_S:.0f} s measured",
         "repeats": "1 per state"},
    ])]))
    s.add(s.card("Running totals", [s.row([
        s.stat("per measurement", _minutes(per), note="warmup + window"),
        s.stat("campaign, 5 experiments", _minutes(per * 6), note="baseline + 5 re-measures, before deploys"),
        s.stat("model spend", "see Budget", note="measured per call in replay"),
    ]), s.notice("One measurement per state, so no variance bounds on any single figure. No 24-hour scenario "
                 "and no ceiling probe (push_beyond) is declared; ceiling discovery needs an operator flag "
                 "(DESIGN.md section 20).", "warn")]))
    return s.done()


def screen_budget(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    import yaml

    from ..perf.fixtures import DEFAULT_MEASURE_S, DEFAULT_WARMUP_S
    from ..perf.scorer import score_replay

    s = _Surface("Budget", "The plans differ on confidence, not features. All three run the same loop.")
    try:
        budgets = yaml.safe_load(ctx.path("config/budgets.yaml").read_text(encoding="utf-8")) or {}
        tiers = yaml.safe_load(ctx.path("config/tiers.yaml").read_text(encoding="utf-8")) or {}
    except OSError as exc:
        s.add(s.notice(str(exc), "bad"))
        return s.done()
    s.add(s.card("Plans", [s.notice(
        "Quick look / Standard / Thorough are not implemented. What exists is one budget and a tier ladder, "
        "shown below; the experiment count is `crucible run --experiments` (default 5).", "warn")]))
    s.add(s.card("Budget, from config/budgets.yaml", [s.table(["setting", "value"], [
        {"setting": "default budget per run", "value": f"${budgets.get('default_budget')}"},
        {"setting": "downgrade at", "value": f"{float(budgets.get('downgrade_at', 0)) * 100:.0f}% spent"},
        {"setting": "refuse at", "value": f"{float(budgets.get('refuse_at', 0)) * 100:.0f}% spent"},
        {"setting": "max calls per run", "value": budgets.get("max_calls_per_run")},
    ]), s.table(["tier", "provider", "model"], [
        {"tier": name, "provider": (t.get("request") or {}).get("provider"), "model": (t.get("request") or {}).get("model")}
        for name, t in (tiers.get("tiers") or {}).items()
    ])]))
    replay = score_replay(_replay_runs(ctx), pricing=_pricing()) if _replay_runs(ctx) else None
    lines: list[str] = []
    if replay and replay["cases"]:
        per_in = replay["input_tokens"] / replay["cases"]
        per_out = replay["output_tokens"] / replay["cases"]
        per_call = replay["cost"] / replay["cases"]
        lines = [
            f"measured per diagnosis call ({replay['cases']} replay cases, {', '.join(replay['models_used'])})",
            f"  input   {per_in:>8.0f} tokens",
            f"  output  {per_out:>8.0f} tokens",
            f"  cost    ${per_call:.5f}",
            "",
            "a campaign makes one diagnosis call per experiment",
            f"  5 experiments x ${per_call:.5f}  = ${5 * per_call:.4f}",
            "",
            "duration comes from the scenario, not the plan",
            f"  {DEFAULT_WARMUP_S:.0f} s warmup + {DEFAULT_MEASURE_S:.0f} s measured = {_minutes(DEFAULT_WARMUP_S + DEFAULT_MEASURE_S)}",
            f"  x (1 baseline + 5 re-measures)          = {_minutes(6 * (DEFAULT_WARMUP_S + DEFAULT_MEASURE_S))}",
            "  + deploy and restart per experiment      (not measured here)",
        ]
    else:
        lines = ["no replay results under docs/bench/, so no measured cost per call"]
    s.add(s.card("Where the estimate comes from", [s.terminal(lines), s.text(
        "Context composition is measured, not estimated: it is the snapshot and the prompt, since knowledge "
        "chunks and source slices are not built. Wall clock is the real budget; model spend is a rounding error.")]))
    return s.done()


# ---------------------------------------------------------------------------
# Screens 11-12 -- Before it runs
# ---------------------------------------------------------------------------


def screen_plan(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.commands import cmd_plan

    s = _Surface("Plan", "crucible plan -- says what it intends and changes nothing. For a tool that edits a "
                 "running service, \"show me first\" is a command, not a checkbox.")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cmd_plan(profile_name=ctx.profile_name, sla_path=str(ctx.path(ctx.sla_path)))
    s.add(s.card("crucible plan", [s.terminal(f"$ crucible plan\n{out.getvalue().rstrip()}\n\nexit {code}")]))
    s.add(s.card("Plan is not preflight", [s.text(
        "Plan reads configuration and changes nothing. Preflight exercises the target for real. Run plan to "
        "review scope, preflight to check the plumbing.")]))
    return s.done()


def screen_preflight(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.commands import preflight_checks

    s = _Surface("Preflight", "One tiny experiment end to end, so a three-hour run does not fail at minute twenty.")
    sla, sla_error = _load_sla(ctx)
    profile, profile_error = _load_profile(ctx)
    if sla is None or profile is None:
        s.add(s.notice(f"Blocked before any check: {sla_error or profile_error}", "bad"))
        return s.done()
    if not q.get("run"):
        s.add(s.card("Checks", [
            s.text("Not run in this view. Run checks performs the read-only checks: environment kind, protected "
                   "paths, metrics, the version endpoint and the deploy declaration. The apply/restart/revert "
                   "rehearsal restarts the target, so it is only ever `crucible preflight --apply-probe`."),
            s.row([s.button("Run checks", "request_data", {"run": "1"})]),
        ]))
    else:
        checks = preflight_checks(profile, sla, str(ctx.path(ctx.sla_path)), probe_timeout_s=3.0)
        rows = [{"check": c.name, "result": "pass" if c.ok else ("FAIL" if c.blocking else "warn"),
                 "detail": c.detail} for c in checks]
        blocked = [c for c in checks if not c.ok and c.blocking]
        s.add(s.card("Checks", [s.table(["check", "result", "detail"], rows)]))
        s.add(s.notice(
            "Blocked: " + "; ".join(f"{c.name} -- {c.detail}" for c in blocked) if blocked else
            "Ready, as far as read-only checks can tell. Model spend so far: $0 -- no diagnosis call was made.",
            "bad" if blocked else "good"))
    restart, deploy = profile.restart, profile.deploy
    s.add(s.card("Manual overrides", [s.table(["step", "mode", "when manual"], [
        {"step": "restart service", "mode": "manual" if restart.manual else "automatic",
         "when manual": "pauses, shows the command, waits for `crucible approve --manual-step-done`"},
        {"step": "revert change", "mode": "automatic", "when manual": "--"},
        {"step": "deploy via pipeline", "mode": "automatic" if deploy.automated else "manual",
         "when manual": "blocks with the profile's instructions and records the step on the manifest"},
        {"step": "database reset", "mode": "not declared", "when manual": "hooks are not implemented"},
    ])]))
    return s.done()


# ---------------------------------------------------------------------------
# Screens 13-15 -- While it runs
# ---------------------------------------------------------------------------


def _latest(ctx: PerfContext) -> dict[str, Any] | None:
    campaigns, _ = _campaigns(ctx)
    return campaigns[-1] if campaigns else None


def screen_live(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.approval import pending_approvals, pending_manual_steps
    from ..perf.campaign import abort_requested

    s = _Surface("Live campaign", "The engineer walks away. This screen has to answer \"what happened while I "
                 "was gone\" at a glance.")
    active = _active_runs(ctx)
    profile, _ = _load_profile(ctx)
    if not active:
        latest = _latest(ctx)
        s.add(s.notice("No campaign is running: no deploy-branch lock is held under "
                       f"{ctx.state / 'locks'}. Start one with `crucible run --run-id <id>`.", "neutral"))
        if latest:
            s.add(s.text(f"Most recent on record: {latest.get('run_id')}, finished "
                         f"{_when(latest.get('finished_at_epoch_s'))}. See Report."))
        s.add(_guardrails(s, profile, None))
        return s.done()

    run = active[0]
    run_id = str(run.get("run_id", ""))
    elapsed = time.time() - float(run.get("since_epoch_s") or time.time())
    approvals = pending_approvals(ctx.state, run_id)
    manual = pending_manual_steps(ctx.state, run_id)
    aborting = abort_requested(ctx.state, run_id)
    status = "aborting" if aborting else ("needs you" if approvals or manual else "running")
    s.add(s.card(f"run {run_id}", [s.row([
        s.stat("status", status, tone="warn" if status != "running" else "neutral"),
        s.stat("p99 now", "not streamed", note="measured values reach the manifest when the campaign ends"),
        s.stat("experiment", f"{approvals[0]['experiment']} waiting" if approvals else "--"),
        s.stat("spent", "on the manifest", note="priced when the campaign writes it"),
        s.stat("elapsed", _minutes(elapsed), note=f"lock on {run.get('branch')}"),
    ]), s.terminal([
        f"abort (discards in-flight, redeploys the last good commit):  crucible abort {run_id}",
        "pause: not a command. An unanswered approval pauses the campaign; nothing measured is discarded.",
    ])]))
    for request in approvals:
        params = dict(request.get("params") or {})
        claims = [
            f"cause: {request.get('cause_family') or 'unnamed'}",
            f"evidence: {', '.join(request.get('evidence_cited') or []) or 'none cited'}",
            _prediction(request),
            f"file: {profile.config_file if profile else 'the profile config file'} -- current value is read "
            "at apply time and recorded on the manifest",
            str(request.get("summary") or ""),
        ]
        s.add(s.card(f"Proposed change -- experiment {request.get('experiment')} -- your approval needed", [
            s.text("\n".join(f"- {c}" for c in claims if c)),
            s.approval(str(request.get("summary") or "proposed change"), params,
                       run_id=run_id, experiment=int(request.get("experiment") or 0)),
            s.text("Approve is bound to exactly these values. The server re-checks them against the parked "
                   "request, and writes the decision with the request's values, never the browser's. Needs "
                   "the control token."),
        ]))
    for step in manual:
        s.add(s.notice(f"Manual step waiting: {step.get('step') or step}. Confirm with "
                       f"`crucible approve {run_id} --experiment {step.get('experiment')} --manual-step-done --as <you>`.",
                       "warn"))
    s.add(s.card("Ruled out so far", [s.text(
        "The ruled-out list is written to the manifest when the campaign ends; it is not streamed while it runs.")]))
    s.add(_guardrails(s, profile, None))
    return s.done()


def _prediction(request: dict[str, Any]) -> str:
    """The agent's own numbers, labelled as claims -- and absent ones said so."""
    p99, confidence = request.get("predicted_p99_ms"), request.get("confidence")
    said = []
    said.append(f"predicts p99 {p99:.0f} ms" if isinstance(p99, (int, float)) else "no p99 prediction")
    said.append(f"confidence {confidence:.2f}" if isinstance(confidence, (int, float)) else "no confidence stated")
    return ", ".join(said) + " (the agent's claims; the verdict re-measures)"


def _guardrails(s: _Surface, profile: Any, campaign: dict[str, Any] | None) -> str:
    if profile is None:
        return s.notice("Profile not loadable; guardrails unknown.", "bad")
    refused = sum(1 for e in (campaign or {}).get("experiments", []) if e.get("refused_by_guard"))
    allowed = s.table(["may change", "bounds"], [
        {"may change": p, "bounds": f"{_cell(b.minimum)} - {_cell(b.maximum)}" if b.minimum is not None else b.kind}
        for p, b in sorted(profile.allowed_properties.items())])
    protected = s.table(["may read, never write"], [{"may read, never write": p} for p in profile.protected_paths])
    never = s.text("Never runs: git push outside the deploy capability, force-push, any command outside the "
                   f"allowlist. Refused attempts: {refused if campaign else 'recorded on the manifest'}.")
    return s.tabs(["Guardrails (collapsed)", "May change", "Protected"],
                  [s.text(f"{len(profile.allowed_properties)} properties allowed, "
                          f"{len(profile.protected_paths)} paths protected."), allowed, s.column([protected, never])])


_LOOP = (
    # (step, role, how the manifest shows it happened)
    ("run_baseline_load", "perception", "baseline.load"),
    ("collect_snapshot", "perception", "baseline.snapshot"),
    ("recall_history", "memory", "prior_findings"),
    ("diagnose", "decision", "diagnosis"),
    ("guard_check", "critic", "proposal"),
    ("request_approval", "human gate", "approval"),
    ("apply_change", "action", "apply_result"),
    ("deploy_and_verify", "action", "deploy"),
    ("run_verify_load", "perception", "after"),
    ("compare_verdict", "critic", "verdict"),
    ("write_manifest", "memory", "verdict"),
)


def screen_graph(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    s = _Surface("Plan graph", "What the campaign loop ran, step by step, per experiment.")
    s.add(s.notice(
        "A perf campaign is a fixed loop in crucible/perf/campaign.py, not a planner-grown graph: its steps are "
        "known up front and none is added as outcomes land. The design's growing graph describes the general "
        "agent's S13 runtime, which a campaign does not run on. This screen shows the loop's steps with the "
        "state each experiment's manifest records.", "neutral"))
    active = _active_runs(ctx)
    campaign = _latest(ctx)
    if active:
        s.add(s.notice(f"Run {active[0].get('run_id')} is in progress. Its steps are not observable until its "
                       "manifest is written; a parked approval (Live campaign) shows it reached request_approval.",
                       "warn"))
    if campaign is None:
        s.add(s.text("No campaign manifest on record, so there is no run to lay out."))
    else:
        baseline = campaign.get("baseline") or {}
        panels, labels = [], []
        for e in campaign.get("experiments", []) or [{}]:
            rows = []
            for step, role, evidence in _LOOP:
                if evidence.startswith("baseline."):
                    state = "done" if baseline.get(evidence.split(".", 1)[1]) else "not reached"
                elif evidence == "verdict":
                    state = "done" if e.get("verdict") else "not reached"
                elif evidence == "prior_findings":
                    # Absent is "not recorded", never "done with nothing found":
                    # an empty recall and a recall nobody logged read differently.
                    findings = e.get("prior_findings")
                    state = f"done ({len(findings)} findings)" if isinstance(findings, list) else "not recorded"
                else:
                    state = "done" if e.get(evidence) else "not reached"
                rows.append({"step": step, "role": role, "state": state})
            panels.append(s.table(["step", "role", "state"], rows))
            labels.append(f"exp {e.get('experiment', '?')}")
        s.add(s.card(f"run {campaign.get('run_id')}", [s.tabs(labels, panels)]))
        hypotheses = [{"family": r, "state": "ruled out"} for r in campaign.get("ruled_out") or []]
        hypotheses += [{"family": e.get("cause_family") or "(none)", "state": e.get("verdict")}
                       for e in campaign.get("experiments", [])]
        s.add(s.card("Hypotheses", [s.table(["family", "state"], hypotheses)]))
    s.add(s.card("Why reasoning is cumulative", [s.terminal([
        "write_manifest --> results/<run_id>.json",
        "                        |",
        "                        v",
        "recall_history <-- JournalIndex.load(results/)   (next experiment, next campaign)",
    ]), s.text("A disproven hypothesis is not proposed again: the journal RAG feeds prior findings into "
               "the next diagnosis (crucible/perf/journal.py).")]))
    return s.done()


def screen_watchdog(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.watchdog import SUSPENDED_ON_CEILING_PROBE, TRIPWIRES, WatchdogThresholds

    s = _Surface("Watchdog", "Every 5 minutes during a long run. Arithmetic, not a model call -- 288 checks on a "
                 "24-hour scenario would cost more than the whole campaign.")
    sla, error = _load_sla(ctx)
    if sla is None:
        s.add(s.notice(error, "bad"))
        return s.done()
    thresholds = WatchdogThresholds.from_sla(sla)
    limit = {
        "target_reachable": f"{thresholds.unreachable_checks} consecutive failed probes",
        "error_rate": f"above {thresholds.error_rate_pct}%",
        "error_rate_trend": f"above +{thresholds.error_rate_trend_pct_per_check}%/check",
        "throughput_collapse": f"{thresholds.throughput_collapse_pct:.0f}% below reference",
        "latency_ceiling": f"above {thresholds.latency_ceiling_ms / 1000:.0f} s",
        "load_generator_alive": f"no beat for {thresholds.load_generator_beat_max_age_s:.0f} s",
        "host_contention": f"cpu steal above {thresholds.cpu_steal_abort_pct:.1f}%",
    }
    campaign = _latest(ctx)
    record = next((e.get("watchdog") for e in reversed((campaign or {}).get("experiments", [])) if e.get("watchdog")),
                  None)
    statuses = _last_statuses(record)
    active = _active_runs(ctx)
    header = (f"Run {active[0].get('run_id')} is in progress; its readings are recorded on the manifest when "
              "the window ends, not streamed." if active else
              "No campaign is running. Statuses below are the last recorded window's, or unknown when none was.")
    s.add(s.card("Tripwires", [
        s.text(header),
        s.table(["tripwire", "status", "threshold", "reading"], [
            {"tripwire": t, "status": statuses.get(t, {}).get("status", "unknown"), "threshold": limit[t],
             "reading": statuses.get(t, {}).get("reading", "no reading")} for t in TRIPWIRES]),
        s.text(f"Thresholds from {thresholds.source}. A tripwire nothing read is unknown, never ok."),
    ]))
    s.add(s.card("On a ceiling probe, the subject tripwires are suspended", [
        s.table(["tripwire", "on push_beyond: true"], [
            {"tripwire": t, "on push_beyond: true": "suspended_on_ceiling_probe" if t in SUSPENDED_ON_CEILING_PROBE
             else "still aborts (an instrument, not the subject)"} for t in TRIPWIRES]),
    ]))
    s.add(s.card("On abort", [s.table(["action"], [
        {"action": "load generator stopped"},
        {"action": "working tree reverted to the last committed experiment"},
        {"action": "manifest written with verdict ABORTED and the tripwire that fired"},
        {"action": "you are told, with the last healthy reading attached"},
    ])]))
    return s.done()


def _last_statuses(record: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Per-tripwire status and reading from the last check a manifest recorded."""
    if not isinstance(record, dict):
        return {}
    checks = record.get("checks") or []
    last = checks[-1] if checks else {}
    out: dict[str, dict[str, str]] = {}
    for wire in last.get("tripwires") or []:
        if isinstance(wire, dict) and wire.get("name"):
            out[str(wire["name"])] = {"status": str(wire.get("status", "unknown")),
                                      "reading": _cell(wire.get("reading"))}
    return out


# ---------------------------------------------------------------------------
# Screens 16-17 -- Afterwards
# ---------------------------------------------------------------------------


_NO_MANIFESTS = (
    "No campaign manifests in {where}. The one live campaign on record (21 September 2026) ran on Box B and its "
    "manifest lives there; docs/W2_E2E_RESULT.md summarises it, and docs/BENCHMARK_LIVE_RESULTS.md says why the "
    "benchmark campaigns have not run."
)


def screen_report(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.commands import _harness_sha
    from ..perf.report import build_report

    s = _Surface("Report", "The audience is the teammate who asks \"why did you change the pool size?\"")
    campaigns, _ = _campaigns(ctx)
    if not campaigns:
        s.add(s.notice(_NO_MANIFESTS.format(where=f"{ctx.results_dir}/"), "neutral"))
        return s.done()
    campaign = next((c for c in campaigns if c.get("run_id") == q.get("run_id")), campaigns[-1])
    report = build_report(campaign, harness_sha=_harness_sha())
    s.add(s.card(f"run {report.run_id}", [
        s.text(report.headline),
        s.text(f"started {_when(campaign.get('started_at_epoch_s'))} . "
               f"stopped: {report.stopped_reason or '--'}", "caption"),
    ]))
    rows = []
    for e in report.experiments:
        mark = "kept" if e["kept"] else ("refused by guard" if e["refused_by_guard"] else "ruled out")
        rows.append({"exp": e["experiment"], "cause": e["cause_family"], "result": mark,
                     "change": ", ".join(f"{k}={v}" for k, v in e["changes"].items()) or "no change",
                     "evidence": e["why"]})
    s.add(s.card("Experiments", [s.table(["exp", "cause", "result", "change", "evidence"], rows)]))
    if report.measurement:
        s.add(s.card("Measurement", [s.row([
            s.stat(k, f"{v['before']:.1f} -> {v['after']:.1f}") for k, v in report.measurement.items()])]))
    cal = report.calibration
    s.add(s.card("Calibration -- a tracked signal, never a decision input", [
        s.table(["exp", "predicted p99", "measured p99", "direction"], [
            {"exp": p["experiment"], "predicted p99": p["predicted_p99_ms"], "measured p99": p["measured_p99_ms"],
             "direction": p["direction"]} for p in cal.get("pairs", [])]),
        s.text(str(cal.get("note", ""))),
    ]))
    s.add(s.card("Limits of this result", [s.text("\n".join(f"- {line}" for line in report.limits) or "- none recorded")]))
    s.add(s.card("Reproduction", [s.terminal([f"{k:<32} {v}" for k, v in report.reproduction.items()])]))
    if len(campaigns) > 1:
        s.add(s.row([s.button(f"report {c.get('run_id')}", "request_data", {"run_id": c.get("run_id")})
                     for c in campaigns[-8:]]))
    return s.done()


def screen_history(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.journal import JournalIndex, journal_summary
    from ..perf.report import compare, final_measurement, summarise_outcome

    s = _Surface("History & diff", "Every campaign on record, and the only safe way to put two side by side.")
    campaigns, skipped = _campaigns(ctx)
    if not campaigns:
        s.add(s.notice(_NO_MANIFESTS.format(where=f"{ctx.results_dir}/"), "neutral"))
    else:
        # The last KEPT experiment's re-measure, never simply the last one (report.py).
        # A campaign that kept nothing has no "after" and is left off the trend
        # rather than drawn as its baseline, which would be a state nobody runs.
        def p99_after(c: dict[str, Any]) -> Any:
            return (final_measurement(c).get("p99_ms") or {}).get("after")

        points = [float(p) for p in map(p99_after, campaigns) if p is not None]
        if points:
            s.add(s.card("p99 after each campaign", [s.sparkline(points)]))
        rows = [{"run": c.get("run_id"), "date": _when(c.get("started_at_epoch_s")),
                 "outcome": summarise_outcome(c), "p99 after": p99_after(c),
                 "environment": (c.get("sla") or {}).get("environment_name")} for c in campaigns]
        ids = [str(c.get("run_id")) for c in campaigns]
        children = [
            s.table(["run", "date", "outcome", "p99 after", "environment"], rows, sortable=True, filter_key="outcome"),
            s.text("Filter by outcome: CHANGE_KEPT, NOTHING_TO_FIX, NO_CHANGE_KEPT."),
            s.row([s.button(f"open {i}", "request_data", {"screen": "16", "run_id": i}) for i in ids[-6:]]),
        ]
        s.add(s.card("Campaigns", children))
        a, b = q.get("a") or (ids[-2] if len(ids) > 1 else ids[-1]), q.get("b") or ids[-1]
        compare_children = [s.row([
            s.choice("A", ids, a, "compare_a"), s.choice("B", ids, b, "compare_b"),
            s.button("Compare", "request_data", {"a": {"$bind": "/compare_a"}, "b": {"$bind": "/compare_b"}}),
        ])]
        if q.get("a") and q.get("b"):
            ca = next((c for c in campaigns if c.get("run_id") == a), None)
            cb = next((c for c in campaigns if c.get("run_id") == b), None)
            if ca and cb:
                result = compare(ca, cb)
                if result.identical_setup:
                    message, tone = "Setup matches: these two measured the same thing.", "good"
                elif result.comparable:
                    message, tone = ("Compared with differences:\n"
                                     + "\n".join(f"- {d}" for d in result.differences)), "warn"
                else:
                    message, tone = ("NOT COMPARABLE -- setup moved:\n"
                                     + "\n".join(f"- {d}" for d in result.differences)), "warn"
                compare_children.append(s.notice(message, tone))
                compare_children.append(s.table(["", "A", "B"], [
                    {"": "run", "A": result.run_a, "B": result.run_b},
                    {"": "outcome", "A": result.outcome_a, "B": result.outcome_b},
                    {"": "p99 after", "A": (result.measurement_a.get("p99_ms") or {}).get("after"),
                     "B": (result.measurement_b.get("p99_ms") or {}).get("after")},
                ]))
        s.add(s.card("Compare two campaigns", compare_children))
    if skipped:
        s.add(s.card("Files in results/ that are not campaigns", [s.table(
            ["file", "why"], [{"file": f, "why": why} for f, why in skipped])]))
    summary = journal_summary(JournalIndex.load(ctx.path(ctx.results_dir)))
    s.add(s.card("Playbooks", [
        s.notice("Playbook memory and promotion are not built. What exists is the journal the next diagnosis "
                 "reads (prior findings), summarised here.", "warn"),
        s.table(["journal", "count"], [{"journal": k, "count": v} for k, v in summary.items()
                                       if not isinstance(v, dict)]),
    ]))
    return s.done()


# ---------------------------------------------------------------------------
# Screen 18 -- Testing itself
# ---------------------------------------------------------------------------


def screen_benchmark(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    from ..perf.fixtures import FixtureError, load_fixtures, load_specs
    from ..perf.replay import ReplayError, load_task_dir
    from ..perf.scorer import score_replay

    s = _Surface("Benchmark", "Crucible testing itself. This is what makes any claim about it mean something.")
    try:
        tasks = load_task_dir(ctx.path(ctx.tasks_dir))
        specs = load_specs(ctx.path(ctx.fixture_config_dir))
    except (ReplayError, FixtureError, OSError) as exc:
        s.add(s.notice(f"Task set or fixture plan does not load: {exc}", "bad"))
        return s.done()
    captured, refused = load_fixtures(ctx.path(ctx.fixture_dir))
    captured_ids = {f.spec.id for f in captured}
    declared_cases = sum(len(t.fixtures) for t in tasks)
    runnable = [t for t in tasks if t.fixtures and all(f in captured_ids for f in t.fixtures)]
    s.add(s.card("Task set v1", [s.row([
        s.stat("tasks", len(tasks), note="a behaviour the harness must exhibit"),
        s.stat("fixtures declared", len(specs), note="one target state, true cause recorded first"),
        s.stat("snapshots captured", len(captured), note="a fixture seen through one provider"),
        s.stat("test cases", declared_cases, note=f"{sum(len(t.fixtures) for t in runnable)} runnable today"),
    ])]))

    runs = _replay_runs(ctx)
    replay = score_replay(runs, pricing=_pricing()) if runs else None
    campaigns, _ = _campaigns(ctx)
    s.add(s.card("How it runs -- replay is cheap, live is not", [s.table(["mode", "cases", "tests", "cost"], [
        {"mode": "replay", "cases": f"{replay['cases']} over {replay['runs']} run(s)" if replay else "none",
         "tests": "diagnosis, refusal, confidence",
         "cost": f"${replay['cost']:.4f}" if replay else "--"},
        {"mode": "live", "cases": len(campaigns), "tests": "apply, restart, re-measure, keep or revert",
         "cost": "not yet measured" if not campaigns else "see History"},
    ])]))
    stale = [f"{path}: {why}" for path, why in refused]
    missing = [f"{sp.id}.{p}" for sp in specs for p in sp.providers
               if not ctx.path(ctx.fixture_dir).joinpath(f"{sp.id}.{p}.json").exists()]
    excluded = [sp.id for sp in specs if not sp.providers]
    notes = []
    if stale:
        notes.append(s.notice("Stale snapshots, refused by collector_version -- recapture:\n" +
                              "\n".join(f"- {x}" for x in stale), "bad"))
    if missing:
        notes.append(s.notice("Declared but not captured:\n" + "\n".join(f"- {x}" for x in missing), "warn"))
    if excluded:
        notes.append(s.text(f"Excluded from capture by declaration (providers: []): {', '.join(excluded)}."))
    if not notes:
        notes.append(s.text("Every declared snapshot is captured and current."))
    s.add(s.card("Snapshots", notes))

    names = {"A": "diagnose and repair", "B": "discriminate lookalikes", "C": "integrity boundary",
             "D": "absence and refusal", "E": "ambiguity"}
    class_rows = []
    for key, label in names.items():
        bucket = (replay or {}).get("by_class", {}).get(key) or {"passed": 0, "graded": 0}
        score = f"{bucket['passed']} / {bucket['graded']}" if bucket["graded"] else "not measured"
        has_task = any(t.task_class.upper().startswith(key) for t in tasks)
        class_rows.append({"class": f"{key} . {label}", "replay score": score,
                           "note": "" if has_task else "no task of this class in config/tasks/"})
    s.add(s.card("By task class (replay)", [s.table(["class", "replay score", "note"], class_rows), s.text(
        "Graded by crucible.perf.scorer.score_replay, which calls no model. Class C carries two caveats in "
        "docs/BENCHMARK_REPLAY_RESULTS.md: the trap was never tempted, and T3's prompt is not sent.")]))

    quadrant = {"CORRECT": [], "LUCKY": [], "UNLUCKY": [], "WRONG": []}
    panels = [s.text(f"{k}: " + ("not yet measured -- no scored live campaign" if not v else ", ".join(v)))
              for k, v in quadrant.items()]
    s.add(s.card("Diagnosis against outcome", [s.tabs(list(quadrant), panels), s.text(
        "Every quadrant needs to know whether the fix worked, which only a live campaign can measure. Replay "
        f"shows the diagnosis half alone: correct on {_correct_count(replay)}.")]))
    claims = [c for c in (_claim(ctx, ctx.replay_results_doc), _claim(ctx, ctx.live_results_doc)) if c]
    s.add(s.card("The claim this supports", [s.text(c) for c in claims] or [
        s.text("No results document yet -- no claim.")]))
    return s.done()


def _correct_count(replay: dict[str, Any] | None) -> str:
    if not replay:
        return "not measured"
    graded = sum(r["graded"] for r in replay["per_case"] if r["task_class"] in ("A", "B", "C"))
    passed = sum(r["passed"] for r in replay["per_case"] if r["task_class"] in ("A", "B", "C"))
    return f"{passed} of {graded} cases with a ground-truth cause (classes A-C)"


# ---------------------------------------------------------------------------
# Screen 19 -- Terminal
# ---------------------------------------------------------------------------


_CLI_GROUPS = {
    "everyday": ("init", "plan", "preflight", "run", "status", "approve", "abort", "clear-abort"),
    "afterwards": ("report", "diff", "score"),
    "benchmark": ("bench", "capture"),
}


def screen_cli(ctx: PerfContext, q: dict[str, str]) -> dict[str, Any]:
    import argparse

    from ..cli import build_parser
    from ..perf.approval import ApprovalRequest

    s = _Surface("CLI", "The UI is a view onto this. Many engineers will never open a browser.")
    parser = build_parser()
    commands: dict[str, str] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public iterator
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for choice in action._choices_actions:  # noqa: SLF001
                commands[choice.dest] = choice.help or ""
    s.add(s.card("Install and first run", [s.terminal([
        "$ uv tool install crucible",
        "$ crucible init",
        "$ crucible plan",
        "$ crucible serve            # this UI at http://127.0.0.1:8113/perf",
    ])]))
    placed: set[str] = set()
    for group, names in _CLI_GROUPS.items():
        rows = [{"command": f"crucible {n}", "does": commands[n]} for n in names if n in commands]
        placed.update(n for n in names if n in commands)
        s.add(s.card(group.capitalize(), [s.table(["command", "does"], rows)]))
    rest = [{"command": f"crucible {n}", "does": h} for n, h in commands.items() if n not in placed]
    if rest:
        s.add(s.card("Other", [s.table(["command", "does"], rest)]))
    card = ApprovalRequest(run_id="<run-id>", experiment=1, summary="", params={"<property>": "<value>"},
                           cause_family="<cause family>").render()
    s.add(s.card("What the terminal shows during a run", [s.terminal([
        "$ crucible run --run-id <run-id>",
        "run <run-id>: <sla> against <environment>",
        "  approve from another terminal: crucible approve <run-id> --experiment 1",
        "  abort:                         crucible abort <run-id>",
        "",
        "# crucible status <run-id> shows the parked proposal:",
        card,
    ]), s.text("Rendered by ApprovalRequest.render() with placeholders -- the real format, no example values.")]))
    return s.done()


# ---------------------------------------------------------------------------
# The registry, in the design's order and groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    number: int
    title: str
    group: str
    build: Callable[[PerfContext, dict[str, str]], dict[str, Any]]
    #: Screens that need a running campaign. They must render a declared empty
    #: state without one rather than fail (principle 2).
    needs_campaign: bool = False


GROUPS = ("Structure", "Getting set up", "Before it runs", "While it runs", "Afterwards", "Testing itself",
          "Terminal")

SCREENS: tuple[Screen, ...] = (
    Screen(1, "Home", "Structure", screen_home),
    Screen(2, "Service settings", "Structure", screen_service),
    Screen(3, "Collection", "Structure", screen_collection),
    Screen(4, "Environments", "Structure", screen_environments),
    Screen(5, "Target profile", "Getting set up", screen_profile),
    Screen(6, "Telemetry", "Getting set up", screen_telemetry),
    Screen(7, "Setup overview", "Getting set up", screen_setup),
    Screen(8, "Requirements", "Getting set up", screen_requirements),
    Screen(9, "Scenarios", "Getting set up", screen_scenarios),
    Screen(10, "Budget", "Getting set up", screen_budget),
    Screen(11, "Plan", "Before it runs", screen_plan),
    Screen(12, "Preflight", "Before it runs", screen_preflight),
    Screen(13, "Live campaign", "While it runs", screen_live, needs_campaign=True),
    Screen(14, "Plan graph", "While it runs", screen_graph, needs_campaign=True),
    Screen(15, "Watchdog", "While it runs", screen_watchdog, needs_campaign=True),
    Screen(16, "Report", "Afterwards", screen_report),
    Screen(17, "History & diff", "Afterwards", screen_history),
    Screen(18, "Benchmark", "Testing itself", screen_benchmark),
    Screen(19, "CLI", "Terminal", screen_cli),
)


def navigation() -> dict[str, Any]:
    return {"groups": [{"name": g, "screens": [{"number": sc.number, "title": sc.title}
                                               for sc in SCREENS if sc.group == g]} for g in GROUPS]}


def build_screen(number: int, ctx: PerfContext | None = None, query: dict[str, str] | None = None) -> dict[str, Any]:
    """One screen, validated exactly as an untrusted surface would be.

    A builder that raises does not blank the page: the error becomes a bad
    Notice on an otherwise valid surface, because an operator at 3am needs the
    reason, not a 500.
    """
    screen = next((sc for sc in SCREENS if sc.number == number), None)
    if screen is None:
        raise KeyError(number)
    ctx = ctx or PerfContext()
    try:
        built = screen.build(ctx, dict(query or {}))
    except Exception as exc:  # noqa: BLE001 - reported on the surface, never swallowed
        failed = _Surface(screen.title, "This screen could not be built.")
        failed.add(failed.notice(f"{type(exc).__name__}: {exc}", "bad"))
        built = failed.done()
    result = validate_surface(built)
    return {
        "number": screen.number, "title": screen.title, "group": screen.group,
        "surface": {"root": built["root"], "components": result.accepted, "dataModel": built["dataModel"]},
        "rejections": [r.as_dict() for r in result.rejections],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter()
_CLIENT = Path(__file__).parent / "client" / "perf.html"


def _context(request: Request) -> PerfContext:
    return getattr(request.app.state, "perf_context", None) or PerfContext()


@router.get("/v1/perf/screens")
async def perf_navigation():
    return navigation()


@router.get("/v1/perf/screens/{number}")
async def perf_screen(number: int, request: Request):
    try:
        return build_screen(number, _context(request), dict(request.query_params))
    except KeyError:
        raise HTTPException(404, f"no screen {number}") from None


@router.get("/v1/perf/slo.yaml", response_class=PlainTextResponse)
async def perf_slo(request: Request):
    """The SLA, read-only. Reading it is allowed; only an operator writes it."""
    ctx = _context(request)
    try:
        return ctx.path(ctx.sla_path).read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(404, "no SLA file") from None


class DecisionBody(BaseModel):
    action: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)
    responder: str = "ui"
    reason: str = ""


@router.post("/v1/perf/runs/{run_id}/experiments/{experiment}/decision",
             dependencies=[Depends(require_control)])
async def perf_decision(run_id: str, experiment: int, body: DecisionBody, request: Request):
    """Answer a parked proposal from the browser, bound exactly as the CLI is.

    Two checks, both inherited. ``decide_resume`` compares what the browser
    sent with what the campaign parked and refuses any difference, so a
    tampered client cannot approve a value nobody was shown. Then
    ``write_decision`` copies the params from the request file, never from this
    body -- and the campaign's own gate checks them a third time when it reads
    the decision.
    """
    from ..perf.approval import write_decision

    ctx = _context(request)
    run_dir = ctx.state / "approvals" / run_id
    request_file = run_dir / f"{experiment:03d}.request.json"
    if not request_file.exists():
        raise HTTPException(404, f"no parked approval for experiment {experiment} of run {run_id}")
    if (run_dir / f"{experiment:03d}.decision.json").exists():
        raise HTTPException(409, "this proposal has already been decided")
    parked = json.loads(request_file.read_text(encoding="utf-8"))
    pending = PendingAction(run_id=run_id, node_id=f"experiment-{experiment}",
                            summary=str(parked.get("summary", "")), params=dict(parked.get("params") or {}))
    args = body.args if body.action == "approve" else {}
    decision = decide_resume(pending, body.action, args)
    if not decision.allowed:
        raise HTTPException(409, decision.reason)
    path = write_decision(ctx.state, run_id, experiment, action=body.action,
                          responder=f"ui:{body.responder}", reason=body.reason)
    return {"written": path.name, "action": body.action, "reason": decision.reason}


def _knowledge(request: Request) -> KnowledgeBase:
    kb = getattr(request.app.state, "knowledge", None)
    if kb is None:
        kb = KnowledgeBase.under(_context(request).state, default_embedder())
        request.app.state.knowledge = kb
    return kb


class DocumentBody(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_CHARS)


@router.post("/v1/perf/knowledge/documents", status_code=202, dependencies=[Depends(require_control)])
async def perf_knowledge_upload(body: DocumentBody, request: Request):
    """Queue one document for indexing; poll the GET route for its status."""
    try:
        upload = _knowledge(request).submit(body.filename, body.text, author="operator:ui")
    except UploadRefused as exc:
        raise HTTPException(422, str(exc)) from exc
    return asdict(upload)


@router.get("/v1/perf/knowledge/documents")
async def perf_knowledge_list(request: Request):
    kb = _knowledge(request)
    return {"documents": await asyncio.to_thread(kb.documents), "uploads": kb.uploads()}


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    screen: str = Field(default="overview", max_length=64)


_SCREEN_HINTS: dict[str, str] = {
    "history":    "The user is on the History & Diff screen, which shows past campaign runs in a table with outcome pills (IMPROVED / REVERTED / BASELINE MET) and p99 before/after columns. They may ask about specific run IDs like TR-001, TR-002, TR-003.",
    "active":     "The user is watching an active campaign in progress. The screen shows real-time logs and the current PDMA loop phase.",
    "results":    "The user is on the Results screen for a completed campaign run.",
    "guardrails": "The user is on the Guardrails screen, which shows which properties the agent may change and within what bounds.",
    "benchmarks": "The user is on the Benchmarks screen, which shows replay eval results across 8 cause families and the LUCKY quadrant scoring.",
    "new-test":   "The user is in the New Test Run wizard, configuring a campaign.",
    "preflight":  "The user is on the Preflight screen, which exercises the deploy loop before a real campaign.",
    "overview":   "The user is on the Overview dashboard.",
}

_CHAT_SYSTEM = """You are the Crucible AI assistant embedded in the Crucible performance-engineering product.

Crucible is an autonomous performance engineer: it runs load experiments against a target service, diagnoses bottlenecks from its telemetry, proposes bounded config changes under human approval, and verifies every change with a fresh measurement.

The facts below are read from the operator's configuration and campaign manifests on disk. Knowledge excerpts come from documents the operator uploaded. Prefer both over general knowledge. When you use an excerpt, name its file. If neither the facts nor the excerpts cover the question, say so instead of guessing.

Answer concisely. You may use only these HTML tags: <strong>, <code>, <em>, <br>, <ul>, <li>, <p>. Stay factual: only claim things that were measured or are written in the facts or excerpts."""

_REPLY_TAGS = re.compile(r"&lt;(/?)(strong|code|em|ul|li|p)&gt;")
_REPLY_BR = re.compile(r"&lt;br\s*/?&gt;")


def _safe_reply(text: str) -> str:
    """Escape everything, then re-allow a few attribute-free tags.

    The reply goes into the page as HTML and the prompt now carries uploaded
    document text, so a document could otherwise inject script into the
    operator's browser, where the control token lives. Allowlist, never blocklist.
    """
    escaped = html.escape(text, quote=False)
    return _REPLY_BR.sub("<br>", _REPLY_TAGS.sub(r"<\1\2>", escaped))


def _chat_facts(ctx: PerfContext) -> str:
    lines: list[str] = []
    sla, sla_error = _load_sla(ctx)
    if sla is None:
        lines.append(f"- SLA: could not be read ({sla_error})")
    else:
        load = sla.at_load or {}
        lines += [
            f"- SLA '{sla.name}' ({ctx.sla_path}): {sla.endpoint} p99 <= {sla.p99_ms:g} ms, "
            f"errors <= {sla.error_rate_pct:g}%, at {load.get('users', '?')} users",
            f"- Environment: {sla.environment_name} ({sla.environment_kind}), target {sla.target_base_url}",
            f"- Noise floor: {sla.noise_p99_spread_pct:g}% p99 spread; smaller changes are inconclusive",
        ]
    profile, profile_error = _load_profile(ctx)
    if profile is None:
        lines.append(f"- Target profile: could not be read ({profile_error})")
    else:
        lines += [
            f"- Target profile '{profile.name}' ({profile.runtime}); config file {profile.config_file}",
            f"- Cause families: {', '.join(profile.cause_families)}",
            f"- Properties the agent may change: {', '.join(profile.allowed_properties)}",
        ]
    campaigns, _skipped = _campaigns(ctx)
    for campaign in campaigns[-5:]:
        base = ((campaign.get("baseline") or {}).get("load") or {}).get("p99_ms")
        verdicts = ", ".join(f"#{e.get('experiment')} {e.get('verdict')}" for e in campaign.get("experiments") or [])
        lines.append(f"- Campaign {campaign.get('run_id')}: baseline p99 {base} ms; "
                     f"experiments {verdicts or 'none'}; stopped: {campaign.get('stopped_reason', '--')}")
    if not campaigns:
        lines.append("- No campaign manifests on disk yet.")
    return "\n".join(lines)


@router.post("/v1/perf/chat")
async def perf_chat(body: ChatBody, request: Request):
    """Chat grounded in the configuration on disk and the uploaded knowledge documents."""
    from ..gateway import GatewayClient

    ctx = _context(request)
    screen_hint = _SCREEN_HINTS.get(body.screen, f"The user is on the '{body.screen}' screen.")
    hits, excerpts = await _retrieve(request, body.message)
    prompt = (f"[Context: {screen_hint}]\n\nFacts:\n{_chat_facts(ctx)}\n\n"
              f"Knowledge excerpts:\n{excerpts}\n\nUser: {body.message}")
    try:
        result = await GatewayClient().chat(
            prompt=prompt,
            system=_CHAT_SYSTEM,
            request={"max_tokens": 500, "temperature": 0},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"gateway error: {exc}") from exc
    return {"reply": _safe_reply(result.get("text") or "(no reply)"), "sources": _sources(hits)}


async def _retrieve(request: Request, query: str) -> tuple[list[dict[str, Any]], str]:
    """Knowledge hits and their prompt text. A search failure is said, not hidden."""
    try:
        hits = await asyncio.to_thread(_knowledge(request).search, query, 4)
    except Exception as exc:  # noqa: BLE001 - the caller still answers, and says what it could not read
        return [], f"(The knowledge base could not be searched: {type(exc).__name__}.)"
    excerpts = "\n\n".join(f"[{h['source']}{' > ' + str(h['heading']) if h.get('heading') else ''}]\n{h['text']}"
                           for h in hits)
    return hits, excerpts or "(no uploaded document matched)"


def _sources(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for h in hits:
        entry = {"source": h["source"], "heading": h.get("heading")}
        if entry not in sources:
            sources.append(entry)
    return sources


class ScenarioRow(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
    users: int = Field(ge=1, le=100_000)
    duration_s: int = Field(ge=30, le=86_400)
    ramp_s: int = Field(ge=0, le=86_400)


class CeilingBounds(BaseModel):
    start_users: int = Field(ge=1, le=100_000)
    step: int = Field(ge=1, le=100_000)
    max_users: int = Field(ge=1, le=100_000)
    hold_s: int = Field(ge=30, le=86_400)


class WizardBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=200)
    p99_ms: float = Field(gt=0, le=600_000)
    error_rate_pct: float = Field(ge=0, le=100)
    users: int = Field(ge=1, le=100_000)
    config_file: str = Field(min_length=1, max_length=300)
    base_branch: str = Field(min_length=1, max_length=200)
    restart_command: str = Field(min_length=1, max_length=500)
    health_url: str = Field(min_length=1, max_length=500)
    provenance: str = Field(default="", max_length=300)
    mode: str = Field(default="Semi-Automated", pattern="^(Advisory|Semi-Automated|Fully Automated|Ceiling Probe)$")
    scenarios: list[ScenarioRow] = Field(default_factory=list, max_length=6)
    experiments: int = Field(default=5, ge=1, le=50)
    goal: str = Field(default="", max_length=1000)
    run_id: str = Field(default="", max_length=80)
    ceiling: CeilingBounds | None = None


def _wizard_paths(ctx: PerfContext) -> tuple[Path, Path, Path]:
    return (ctx.path(ctx.sla_path), ctx.path(ctx.profiles_dir) / f"{ctx.profile_name}.yaml",
            ctx.path(ctx.locustfile))


def _wizard_form(body: WizardBody, operator: str) -> Any:
    import shlex

    from .wizard import WizardForm, WizardRefused

    try:
        command = shlex.split(body.restart_command)
    except ValueError as exc:
        raise WizardRefused(f"restart command: {exc}") from exc
    return WizardForm(endpoint=body.endpoint, p99_ms=float(body.p99_ms), error_rate_pct=float(body.error_rate_pct),
                      users=body.users, config_file=body.config_file, base_branch=body.base_branch,
                      restart_command=command, health_url=body.health_url, operator=operator,
                      provenance=body.provenance)


@router.get("/v1/perf/wizard")
async def perf_wizard_current(request: Request):
    """The values the wizard opens with: whatever slo.yaml and the profile say now."""
    from .wizard import current

    try:
        return current(*_wizard_paths(_context(request)))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/v1/perf/wizard/preview")
async def perf_wizard_preview(body: WizardBody, request: Request):
    """The diffs Launch would write. Writes nothing."""
    from .wizard import WizardRefused, render

    try:
        rendered = render(_wizard_form(body, "operator"), *_wizard_paths(_context(request)))
    except WizardRefused as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"sla_diff": rendered["sla_diff"], "profile_diff": rendered["profile_diff"],
            "notes": _launch_notes(body)}


@router.post("/v1/perf/wizard/check")
async def perf_wizard_check(request: Request):
    """Test connection: preflight's read-only checks, the same list the CLI prints."""
    from ..perf.campaign import Sla
    from ..perf.commands import preflight_checks
    from ..perf.profile import TargetProfile

    ctx = _context(request)
    sla_path, profile_path, _ = _wizard_paths(ctx)
    try:
        sla, profile = Sla.load(sla_path), TargetProfile.load(profile_path)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {"checks": [{"name": "configuration loads", "ok": False, "blocking": True, "detail": str(exc)}]}
    checks = await asyncio.to_thread(preflight_checks, profile, sla, str(sla_path),
                                     workspace=os.getenv("CRUCIBLE_WORKSPACE", "."), apply_probe=False)
    return {"checks": [{"name": c.name, "ok": c.ok, "blocking": c.blocking, "detail": c.detail} for c in checks]}


@router.post("/v1/perf/wizard/budget")
async def perf_wizard_budget(body: WizardBody, request: Request):
    """The Budget step's arithmetic, from the operator's inputs and past manifests."""
    import math

    from ..perf.fixtures import DEFAULT_WARMUP_S
    from ..perf.scorer import score_cost
    from .wizard import estimate

    ctx = _context(request)
    profile, error = _load_profile(ctx)
    if profile is None:
        raise HTTPException(422, error)
    pricing = _pricing()
    campaigns, _ = _campaigns(ctx)
    per_call = [c for campaign in campaigns for c in score_cost(campaign, pricing).per_experiment if c]
    if body.mode == "Ceiling Probe":
        if body.ceiling is None:
            raise HTTPException(422, "a ceiling probe needs its bounds")
        c = body.ceiling
        steps = max(1, math.floor((c.max_users - c.start_users) / c.step) + 1)
        result = estimate(mode=body.mode, warmup_s=DEFAULT_WARMUP_S, measure_s=c.hold_s, experiments=0,
                          verify_timeout_s=0, per_call_costs=per_call, ceiling_steps=steps)
    else:
        if not body.scenarios:
            raise HTTPException(422, "at least one scenario is required")
        result = estimate(mode=body.mode, warmup_s=DEFAULT_WARMUP_S, measure_s=body.scenarios[0].duration_s,
                          experiments=body.experiments, verify_timeout_s=float(profile.deploy.verify_timeout_s or 0),
                          per_call_costs=per_call)
    result["currency"] = pricing.currency
    return result


def _launch_notes(body: WizardBody) -> list[str]:
    notes = []
    if body.mode == "Advisory":
        notes.append("Advisory mode is not built yet (Group B); Launch will refuse it.")
    if body.mode != "Ceiling Probe" and len(body.scenarios) > 1:
        notes.append(f"Only the first scenario ({body.scenarios[0].name}) runs; "
                     "multi-scenario campaigns come in Group B.")
    if body.mode != "Ceiling Probe" and body.scenarios:
        notes.append("Ramp is not applied yet: Locust spawns at its fixed 10 users/s. "
                     "A 120 s warmup is discarded before the scenario's measured duration.")
    return notes


@router.post("/v1/perf/wizard/launch", dependencies=[Depends(require_control)])
async def perf_wizard_launch(body: WizardBody, request: Request):
    """Write the operator's SLA and deploy settings, then start the run as a CLI job."""
    from .wizard import WizardRefused, render, write

    ctx = _context(request)
    if body.mode == "Advisory":
        raise HTTPException(409, "Advisory mode is not built yet (Group B). Nothing was written.")
    if body.mode == "Ceiling Probe" and body.ceiling is None:
        raise HTTPException(422, "a ceiling probe needs its bounds (DESIGN.md 20.2)")
    if body.mode != "Ceiling Probe" and not body.scenarios:
        raise HTTPException(422, "at least one scenario is required")
    sla_path, profile_path, locustfile = _wizard_paths(ctx)
    try:
        rendered = render(_wizard_form(body, "operator:ui"), sla_path, profile_path, locustfile)
        write(rendered, sla_path, profile_path, _active_runs(ctx))
    except WizardRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        if body.mode == "Ceiling Probe":
            c = body.ceiling
            args = _actions.ceiling_args(profile=ctx.profile_name, sla=str(sla_path), users=c.start_users,
                                         measure=float(c.hold_s), step=c.step, max_users=c.max_users,
                                         run_id=body.run_id, state_dir=str(ctx.state))
        else:
            first = body.scenarios[0]
            args = _actions.run_args(
                profile=ctx.profile_name, sla=str(sla_path), scenario=first.name, users=first.users,
                measure=float(first.duration_s), experiments=body.experiments, run_id=body.run_id,
                state_dir=str(ctx.state), workspace=os.getenv("CRUCIBLE_WORKSPACE", ""),
                approve="preapproved" if body.mode == "Fully Automated" else "file",
                stakeholder_request=body.goal,
            )
    except ActionError as exc:
        raise HTTPException(422, f"the files were written, but the run could not start: {exc}") from exc
    job = start_job(ctx.state, ctx.root, args, label=f"{body.mode.lower()} {body.run_id or ''}".strip())
    return {"job": _job_dict(job), "sla_diff": rendered["sla_diff"], "profile_diff": rendered["profile_diff"],
            "notes": _launch_notes(body)}


class SuggestBody(BaseModel):
    kind: str = Field(pattern="^(sla|scenarios)$")
    goal: str = Field(default="", max_length=1000)
    endpoint: str = Field(default="", max_length=200)
    mode: str = Field(default="", max_length=40)


@router.post("/v1/perf/suggest", dependencies=[Depends(require_control)])
async def perf_suggest(body: SuggestBody, request: Request):
    """Pre-fill values for the wizard. The operator edits and submits them; nothing is written here."""
    from ..gateway import GatewayClient
    from .suggest import SYSTEM, SuggestionRefused, build_prompt, parse

    hits, excerpts = await _retrieve(request, f"{body.goal} {body.endpoint}".strip() or body.kind)
    prompt = build_prompt(body.kind, goal=body.goal, endpoint=body.endpoint, mode=body.mode,
                          facts=_chat_facts(_context(request)), excerpts=excerpts)
    try:
        result = await GatewayClient().chat(prompt=prompt, system=SYSTEM,
                                            request={"max_tokens": 600, "temperature": 0})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"gateway error: {exc}") from exc
    try:
        suggestion = parse(body.kind, result.get("text") or "")
    except SuggestionRefused as exc:
        raise HTTPException(502, f"unusable suggestion: {exc}") from exc
    suggestion["rationale"] = html.escape(suggestion["rationale"], quote=False)
    return {"kind": body.kind, "suggestion": suggestion, "sources": _sources(hits),
            "model": result.get("model"), "provider": result.get("provider")}


# ---------------------------------------------------------------------------
# Job action endpoints — write operations that spawn long-lived processes
# ---------------------------------------------------------------------------


def _job_dict(j: Job) -> dict[str, Any]:
    return {
        "id": j.id, "label": j.label, "status": j.status,
        "started_at": j.started_at_epoch_s, "exit_code": j.exit_code,
        "command_line": j.command_line,
    }


class RunBody(BaseModel):
    run_id: str = ""
    profile: str = ""
    sla: str = ""
    scenario: str = ""
    users: int | None = None
    warmup: float | None = None
    measure: float | None = None
    experiments: int | None = None
    workspace: str = ""
    approve: str = "file"
    approval_timeout: float | None = None
    provider: str = ""
    model: str = ""
    stakeholder_request: str = ""


@router.post("/v1/perf/jobs/run", dependencies=[Depends(require_control)])
async def perf_run(body: RunBody, request: Request):
    """Start a ``crucible run`` campaign as a background job."""
    ctx = _context(request)
    try:
        args = _actions.run_args(
            run_id=body.run_id, profile=body.profile, sla=body.sla,
            scenario=body.scenario, users=body.users, warmup=body.warmup,
            measure=body.measure, experiments=body.experiments,
            workspace=body.workspace, approve=body.approve,
            approval_timeout=body.approval_timeout,
            provider=body.provider, model=body.model,
            state_dir=str(ctx.state),
            stakeholder_request=body.stakeholder_request,
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    label = f"run {body.run_id}" if body.run_id else "crucible run"
    job = start_job(ctx.state, ctx.root, args, label=label)
    return _job_dict(job)


class PreflightBody(BaseModel):
    profile: str = ""
    sla: str = ""
    workspace: str = ""
    apply_probe: bool = False
    probe_timeout: float | None = None


@router.post("/v1/perf/jobs/preflight", dependencies=[Depends(require_control)])
async def perf_preflight_job(body: PreflightBody, request: Request):
    """Start a ``crucible preflight`` check as a background job."""
    ctx = _context(request)
    try:
        args = _actions.preflight_args(
            profile=body.profile, sla=body.sla, workspace=body.workspace,
            apply_probe=body.apply_probe, probe_timeout=body.probe_timeout,
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    label = "crucible preflight --apply-probe" if body.apply_probe else "crucible preflight"
    job = start_job(ctx.state, ctx.root, args, label=label)
    return _job_dict(job)


class CeilingBody(BaseModel):
    run_id: str = ""
    profile: str = ""
    sla: str = ""
    scenario: str = ""
    users: int | None = None
    warmup: float | None = None
    measure: float | None = None
    min_users: int | None = None
    max_users: int  # required per DESIGN.md §20.2
    step: int | None = None


@router.post("/v1/perf/jobs/ceiling", dependencies=[Depends(require_control)])
async def perf_ceiling(body: CeilingBody, request: Request):
    """Start a ``crucible ceiling`` probe as a background job."""
    ctx = _context(request)
    try:
        args = _actions.ceiling_args(
            run_id=body.run_id, profile=body.profile, sla=body.sla,
            scenario=body.scenario, users=body.users, warmup=body.warmup,
            measure=body.measure, min_users=body.min_users,
            max_users=body.max_users, step=body.step,
            state_dir=str(ctx.state),
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    label = f"ceiling {body.run_id}" if body.run_id else "crucible ceiling"
    job = start_job(ctx.state, ctx.root, args, label=label)
    return _job_dict(job)


class BenchBody(BaseModel):
    tasks: str = "config/tasks"
    fixtures: str = "results"
    profile: str = ""
    sla: str = ""
    out: str = ""
    provider: str = ""
    model: str = ""
    skip_missing_fixtures: bool = True


@router.post("/v1/perf/jobs/bench", dependencies=[Depends(require_control)])
async def perf_bench_job(body: BenchBody, request: Request):
    """Start a ``crucible bench`` replay run as a background job."""
    ctx = _context(request)
    try:
        args = _actions.bench_args(
            tasks=body.tasks or str(ctx.path(ctx.tasks_dir)),
            fixtures=body.fixtures or str(ctx.path(ctx.fixture_dir)),
            profile=body.profile, sla=body.sla, out=body.out,
            provider=body.provider, model=body.model,
            skip_missing_fixtures=body.skip_missing_fixtures,
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = start_job(ctx.state, ctx.root, args, label="crucible bench")
    return _job_dict(job)


class AbortBody(BaseModel):
    reason: str = ""
    clear: bool = False


@router.post("/v1/perf/campaign/{run_id}/abort", dependencies=[Depends(require_control)])
async def perf_abort(run_id: str, body: AbortBody, request: Request):
    """Abort a running campaign (discards in-flight, redeploys last good commit)."""
    ctx = _context(request)
    try:
        args = _actions.abort_args(
            run_id=run_id, reason=body.reason, clear=body.clear,
            state_dir=str(ctx.state),
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = start_job(ctx.state, ctx.root, args, label=f"abort {run_id}")
    return _job_dict(job)


class PauseBody(BaseModel):
    reason: str = ""


@router.post("/v1/perf/campaign/{run_id}/pause", dependencies=[Depends(require_control)])
async def perf_pause(run_id: str, body: PauseBody, request: Request):
    """Pause a running campaign (holds the state; nothing measured is lost)."""
    ctx = _context(request)
    try:
        args = _actions.pause_args(run_id=run_id, reason=body.reason, state_dir=str(ctx.state))
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = start_job(ctx.state, ctx.root, args, label=f"pause {run_id}")
    return _job_dict(job)


class ResumeBody(BaseModel):
    profile: str = ""
    sla: str = ""
    scenario: str = ""
    workspace: str = ""


@router.post("/v1/perf/campaign/{run_id}/resume", dependencies=[Depends(require_control)])
async def perf_resume(run_id: str, body: ResumeBody, request: Request):
    """Resume a paused campaign."""
    ctx = _context(request)
    try:
        args = _actions.resume_args(
            run_id=run_id, state_dir=str(ctx.state),
            profile=body.profile, sla=body.sla,
            scenario=body.scenario, workspace=body.workspace,
        )
    except ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = start_job(ctx.state, ctx.root, args, label=f"resume {run_id}")
    return _job_dict(job)


@router.get("/v1/perf/jobs")
async def perf_list_jobs(request: Request, limit: int = 50):
    """List recent background jobs, newest first."""
    ctx = _context(request)
    jobs = list_jobs(ctx.state, limit=limit)
    return {"jobs": [_job_dict(j) for j in jobs]}


@router.get("/v1/perf/jobs/{job_id}")
async def perf_get_job(job_id: str, request: Request):
    """Get a single job's metadata."""
    ctx = _context(request)
    j = get_job(ctx.state, job_id)
    if j is None:
        raise HTTPException(404, f"no job {job_id}")
    return _job_dict(j)


@router.get("/v1/perf/jobs/{job_id}/output", response_class=PlainTextResponse)
async def perf_job_output(job_id: str, request: Request):
    """Tail the output log of a job (last 200 KB)."""
    ctx = _context(request)
    j = get_job(ctx.state, job_id)
    if j is None:
        raise HTTPException(404, f"no job {job_id}")
    return read_output(j)


@router.get("/v1/perf/campaign/active")
async def perf_active_campaign(request: Request):
    """Return live campaign summary with pending approval details — polled by the UI every few seconds."""
    from ..perf.approval import pending_approvals, pending_manual_steps
    from ..perf.campaign import abort_requested

    ctx = _context(request)
    active = _active_runs(ctx)
    if not active:
        # surface the latest job that is still RUNNING so the UI can track it
        running_jobs = [j for j in list_jobs(ctx.state, limit=20) if j.status == "RUNNING"]
        return {"active": False, "running_job": _job_dict(running_jobs[0]) if running_jobs else None}

    run = active[0]
    run_id = str(run.get("run_id", ""))
    elapsed = time.time() - float(run.get("since_epoch_s") or time.time())
    approvals = pending_approvals(ctx.state, run_id)
    manual = pending_manual_steps(ctx.state, run_id)
    aborting = abort_requested(ctx.state, run_id)
    status = "aborting" if aborting else ("needs_approval" if approvals else ("manual_step" if manual else "running"))

    # find the running job for log tailing
    running_jobs = [j for j in list_jobs(ctx.state, limit=20) if j.status == "RUNNING"]
    job = next((j for j in running_jobs if run_id in j.label), running_jobs[0] if running_jobs else None)

    return {
        "active": True,
        "run_id": run_id,
        "elapsed_s": round(elapsed),
        "branch": run.get("branch"),
        "status": status,
        "approvals": approvals,
        "manual_steps": manual,
        "job": _job_dict(job) if job else None,
    }


@router.get("/v1/perf/runs")
async def perf_list_runs(request: Request):
    """List completed campaign runs, newest first."""
    from ..perf.report import final_measurement, summarise_outcome

    ctx = _context(request)
    campaigns, _ = _campaigns(ctx)
    rows = []
    for c in reversed(campaigns):
        m = final_measurement(c)
        p99 = m.get("p99_ms") or {}
        rows.append({
            "run_id": c.get("run_id"),
            "started_at": c.get("started_at_epoch_s"),
            "outcome": summarise_outcome(c),
            "p99_before": p99.get("before"),
            "p99_after": p99.get("after"),
        })
    return {"runs": rows}


@router.get("/v1/perf/runs/{run_id}/report")
async def perf_run_report(run_id: str, request: Request):
    """Return a full campaign report as JSON."""
    from ..perf.commands import _harness_sha
    from ..perf.report import build_report

    ctx = _context(request)
    campaigns, _ = _campaigns(ctx)
    # allow 'latest' as a shorthand
    if run_id == "latest":
        campaign = campaigns[-1] if campaigns else None
    else:
        campaign = next((c for c in campaigns if c.get("run_id") == run_id), None)
    if campaign is None:
        raise HTTPException(404, f"no campaign {run_id!r}")
    report = build_report(campaign, harness_sha=_harness_sha())
    return report.as_dict()


@router.get("/v1/perf/campaign/{run_id}/state")
async def perf_campaign_state(run_id: str, request: Request):
    """Return the current state of a campaign: active, pending approvals, lock."""
    from ..perf.approval import pending_approvals, pending_manual_steps
    from ..perf.campaign import abort_requested

    ctx = _context(request)
    active = _active_runs(ctx)
    is_active = any(r.get("run_id") == run_id for r in active)
    approvals = pending_approvals(ctx.state, run_id) if is_active else []
    manual_steps = pending_manual_steps(ctx.state, run_id) if is_active else []
    aborting = abort_requested(ctx.state, run_id) if is_active else False
    status = "aborting" if aborting else ("needs_approval" if approvals else ("manual_step" if manual_steps else ("running" if is_active else "idle")))
    return {
        "run_id": run_id,
        "status": status,
        "approvals": approvals,
        "manual_steps": manual_steps,
    }


@router.get("/perf", response_class=HTMLResponse)
@router.get("/perf/", response_class=HTMLResponse)
async def perf_client():
    if not _CLIENT.exists():
        raise HTTPException(500, "perf client missing")
    return _CLIENT.read_text(encoding="utf-8")


_DASHBOARD = Path(__file__).parent / "client" / "crucible-ui.html"


@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
async def dashboard():
    """The Crucible SPA — design-quality campaign UI served as one static file."""
    if not _DASHBOARD.exists():
        raise HTTPException(500, "dashboard client missing")
    return _DASHBOARD.read_text(encoding="utf-8")


__all__ = [
    "GROUPS",
    "SCREENS",
    "PerfContext",
    "Screen",
    "build_screen",
    "navigation",
    "router",
]
