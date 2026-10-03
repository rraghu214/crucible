"""Crucible NiceGUI application — campaign path UI.

Six areas, left-rail navigation, live event stream from the campaign.
Design source: ``docs/ref/crucible-nicegui-screens.html``.

**Capability in, UI polish out** (DESIGN.md §16). The campaign path
(Home → New → live Campaign → Report) is built as interactive screens.
History, Benchmark and Settings are read-only views over the data the CLI
and captures produce. Everything else is exposed through the CLI.

Start: ``crucible serve`` (wires this into ``perf_serve``) or directly::

    uv run python -m crucible.ui.nicegui_app

Either way it reads from the working directory (or ``CRUCIBLE_ROOT`` if set).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nicegui import ui

from .perf_data import (
    PerfContext,
    active_runs,
    campaign_verdict_counts,
    claim,
    fmt_when,
    latest_campaign,
    list_campaigns,
    list_fixtures,
    list_replay_runs,
    list_tasks,
    load_profile,
    load_sla,
)

# ---------------------------------------------------------------------------
# Theme tokens (Chivo typography, thermal palette from the design file)
# ---------------------------------------------------------------------------

THEME = """
:root {
  --ink0: #0e0e0f; --ink1: #1c1c1f; --ink2: #2f2f35; --ink3: #666;
  --surf0: #f7f7f8; --surf1: #eeeeef; --surf2: #e0e0e2;
  --accent: #ef6b2f; --patina: #3bab8c; --molten: #d4a843;
  --ok: #3bab8c; --warn: #d4a843; --err: #e05252;
  font-family: 'Chivo', ui-sans-serif, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink0: #f0f0f2; --ink1: #d0d0d4; --ink2: #8888a0; --ink3: #555;
    --surf0: #0e0e0f; --surf1: #17171a; --surf2: #222226;
    --accent: #ef6b2f;
  }
}
body { background: var(--surf0); color: var(--ink0); margin: 0; }
.n-chip-ok  { background: #1a3a31; color: var(--ok); }
.n-chip-err { background: #3a1a1a; color: var(--err); }
.n-chip-warn{ background: #3a2e0a; color: var(--molten); }
.mono { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 0.85em; }
"""

# ---------------------------------------------------------------------------
# App-wide state (populated once at startup, refreshed on navigation)
# ---------------------------------------------------------------------------

_ctx: PerfContext | None = None


def _get_ctx() -> PerfContext:
    global _ctx
    if _ctx is None:
        root = Path(os.getenv("CRUCIBLE_ROOT", ".")).resolve()
        _ctx = PerfContext(root=root)
    return _ctx


# ---------------------------------------------------------------------------
# Shared layout
# ---------------------------------------------------------------------------

def _header_links() -> None:
    """Top-bar title and navigation links (NiceGUI header)."""
    with ui.link(target="/").classes("no-underline"):
        ui.html("""<span style="font-weight:700;font-size:1.1em;
            background:linear-gradient(135deg,#ef6b2f,#d4a843);
            -webkit-background-clip:text;-webkit-text-fill-color:transparent">
            Crucible</span>""")
    ui.space()
    for label, href in [("Home", "/"), ("New", "/new"), ("Campaign", "/campaign"),
                        ("History", "/history"), ("Benchmark", "/benchmark"),
                        ("Settings", "/settings")]:
        ui.link(label, href).classes("text-sm font-medium px-3 py-1")


def _page_layout(title: str):
    """Context manager that renders the standard page chrome."""
    ui.add_head_html(f"<style>{THEME}</style>")
    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Chivo:wght@400;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet">'
    )
    with ui.header().classes("items-center px-4 gap-2 shadow-sm bg-[var(--surf1)]"):
        _header_links()
    with ui.footer().classes("px-4 py-1 text-xs text-[var(--ink3)] bg-[var(--surf1)] flex gap-4"):
        ctx = _get_ctx()
        sla, sla_err = load_sla(ctx)
        profile, _ = load_profile(ctx)
        ui.label(f"service: {getattr(profile, 'name', '—')}")
        ui.label(f"env: {getattr(sla, 'environment_name', '—') if sla else '—'}")
        ui.label(f"collector: {_collector_version()}")
        ui.space()
        ui.label("Crucible")
    return ui.column().classes("max-w-5xl mx-auto px-4 py-6 gap-6 w-full")


def _collector_version() -> str:
    try:
        from ..perf.collector import COLLECTOR_VERSION
        return COLLECTOR_VERSION
    except ImportError:
        return "?"


# ---------------------------------------------------------------------------
# Chip helpers
# ---------------------------------------------------------------------------

def _verdict_chip(verdict: str) -> None:
    cls = {"IMPROVED": "n-chip-ok", "WORSE": "n-chip-err",
           "INCONCLUSIVE": "n-chip-warn", "ABORTED": "n-chip-warn"}.get(verdict, "")
    ui.badge(verdict, color=None).classes(f"text-xs {cls}")


def _status_chip(ok: bool, label_ok: str = "ok", label_fail: str = "fail") -> None:
    if ok:
        ui.badge(label_ok, color=None).classes("n-chip-ok text-xs")
    else:
        ui.badge(label_fail, color=None).classes("n-chip-err text-xs")


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@ui.page("/")
def page_home() -> None:
    with _page_layout("Home"):
        ctx = _get_ctx()
        ui.label("Home").classes("text-2xl font-bold")

        # Active campaigns
        runs = active_runs(ctx)
        if runs:
            with ui.card().classes("w-full"):
                ui.label("Active campaigns").classes("font-semibold text-sm text-[var(--ink3)]")
                for run in runs:
                    with ui.row().classes("items-center gap-2 py-1"):
                        ui.badge("RUNNING", color=None).classes("n-chip-ok text-xs")
                        ui.label(run.get("run_id", "?")).classes("mono")
                        ui.label(f"branch: {run.get('branch', '?')}").classes("text-xs text-[var(--ink3)]")
                        ui.link("View →", "/campaign").classes("text-xs text-[var(--accent)]")
        else:
            with ui.card().classes("w-full bg-[var(--surf1)]"):
                ui.label("No campaign running.").classes("text-sm text-[var(--ink3)]")
                ui.label("Start one: ").classes("text-xs text-[var(--ink3)]")
                ui.code("crucible run --help").classes("mono text-xs")

        # Recent campaigns
        campaigns, skipped = list_campaigns(ctx)
        with ui.card().classes("w-full"):
            ui.label("Recent campaigns").classes("font-semibold text-sm text-[var(--ink3)]")
            if not campaigns:
                ui.label("No results yet. Run crucible run … to start.").classes("text-sm text-[var(--ink3)]")
            else:
                recent = list(reversed(campaigns[-6:]))
                with ui.table(
                    columns=[
                        {"name": "run_id", "label": "Run", "field": "run_id", "align": "left"},
                        {"name": "sla", "label": "SLA", "field": "sla", "align": "left"},
                        {"name": "experiments", "label": "Experiments", "field": "experiments", "align": "right"},
                        {"name": "when", "label": "When", "field": "when", "align": "right"},
                    ],
                    rows=[
                        {
                            "run_id": c.get("run_id", "?"),
                            "sla": (c.get("sla") or {}).get("name", "?"),
                            "experiments": str(len(c.get("experiments") or [])),
                            "when": fmt_when(c.get("started_at_epoch_s")),
                        }
                        for c in recent
                    ],
                ).classes("w-full text-sm"):
                    pass
        if skipped:
            ui.label(f"{len(skipped)} result file(s) could not be read.").classes(
                "text-xs text-[var(--err)]")

        # Quick actions
        with ui.row().classes("gap-2"):
            ui.button("New campaign", icon="add", on_click=lambda: ui.navigate.to("/new")).classes(
                "bg-[var(--accent)] text-white")
            ui.button("Benchmark", icon="bar_chart", on_click=lambda: ui.navigate.to("/benchmark")).classes(
                "bg-[var(--surf2)]")


# ---------------------------------------------------------------------------
# New campaign
# ---------------------------------------------------------------------------

@ui.page("/new")
def page_new() -> None:
    with _page_layout("New"):
        ctx = _get_ctx()
        ui.label("New campaign").classes("text-2xl font-bold")

        sla, sla_err = load_sla(ctx)
        profile, profile_err = load_profile(ctx)

        if sla_err or profile_err:
            with ui.card().classes("w-full"):
                if sla_err:
                    ui.label(f"SLA: {sla_err}").classes("text-[var(--err)] text-sm")
                if profile_err:
                    ui.label(f"Profile: {profile_err}").classes("text-[var(--err)] text-sm")
            return

        with ui.card().classes("w-full"):
            ui.label("Configuration").classes("font-semibold text-sm text-[var(--ink3)]")
            with ui.grid(columns=2).classes("gap-4 w-full text-sm"):
                ui.label("Target:")
                ui.label(sla.target_base_url).classes("mono")
                ui.label("Profile:")
                ui.label(profile.name)
                ui.label("SLA:")
                ui.label(f"{sla.name} — p99 ≤ {sla.latency_p99_ms}ms, error ≤ {sla.error_rate_pct}%")
                ui.label("Environment:")
                ui.label(sla.environment_name or "—")

        with ui.card().classes("w-full"):
            ui.label("Run command").classes("font-semibold text-sm text-[var(--ink3)]")
            cmd = (
                "crucible run \\\n"
                f"  --sla config/slo.yaml \\\n"
                f"  --profile {profile.name} \\\n"
                "  --users 50 --warmup 120 --measure 300 \\\n"
                "  --approve preapproved"
            )
            ui.code(cmd, language="bash").classes("w-full text-xs")
            ui.label(
                "Remove --approve preapproved to use the interactive approval gate."
            ).classes("text-xs text-[var(--ink3)]")

        with ui.card().classes("w-full bg-[var(--surf1)]"):
            ui.label("Preflight (recommended)").classes("font-semibold text-sm text-[var(--ink3)]")
            ui.label(
                "Run a tiny end-to-end experiment before the campaign — deploy, restart, revert:"
            ).classes("text-xs text-[var(--ink3)]")
            ui.code("crucible preflight --sla config/slo.yaml --profile " + profile.name,
                    language="bash").classes("text-xs")


# ---------------------------------------------------------------------------
# Campaign live view
# ---------------------------------------------------------------------------

def _parked_proposals(ctx: PerfContext, run_id: str) -> list[dict[str, Any]]:
    """Return any pending (unanswered) proposals for this run."""
    try:
        from ..perf.approval import pending_approvals  # noqa: PLC0415
        return pending_approvals(ctx.state, run_id)
    except Exception:  # noqa: BLE001
        return []


@ui.page("/campaign")
def page_campaign() -> None:
    with _page_layout("Campaign"):
        ctx = _get_ctx()
        with ui.row().classes("items-center gap-3 w-full"):
            ui.label("Campaign").classes("text-2xl font-bold")
            ui.badge("live", color=None).classes("n-chip-ok text-xs")

        content = ui.column().classes("gap-4 w-full")

        def _refresh() -> None:
            content.clear()
            runs = active_runs(ctx)
            campaign = latest_campaign(ctx)
            with content:
                if not runs and not campaign:
                    with ui.card().classes("w-full bg-[var(--surf1)]"):
                        ui.label("No campaign running or completed.").classes("text-sm text-[var(--ink3)]")
                        ui.code("crucible run --help", language="bash").classes("text-xs")
                    return

                # Active run status + parked proposals
                if runs:
                    for run in runs:
                        rid = run.get("run_id", "?")
                        branch = run.get("branch", "?")
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.badge("RUNNING", color=None).classes("n-chip-ok text-xs")
                                ui.label(rid).classes("mono font-semibold")
                                ui.label(f"branch: {branch}").classes("text-xs text-[var(--ink3)]")
                            with ui.row().classes("gap-2 mt-2"):
                                ui.button("Pause", icon="pause").props("outline size=sm").on(
                                    "click",
                                    lambda _, r=rid: ui.notify(
                                        f"Run: crucible pause {r}", type="info")
                                )
                                ui.button("Abort", icon="stop").props("outline size=sm color=negative").on(
                                    "click",
                                    lambda _, r=rid: ui.notify(
                                        f"Run: crucible abort {r}", type="warning")
                                )

                        # Show any parked proposals
                        proposals = _parked_proposals(ctx, rid)
                        if proposals:
                            for p in proposals:
                                exp_num = p.get("experiment", "?")
                                changes = p.get("changes") or []
                                cause = p.get("cause_family", "?")
                                with ui.card().classes(
                                        "w-full border border-[var(--molten)] bg-[var(--surf1)]"):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.badge("AWAITING APPROVAL", color=None).classes(
                                            "n-chip-warn text-xs")
                                        ui.label(f"Experiment #{exp_num}").classes("font-semibold")
                                    ui.label(f"Diagnosis: {cause}").classes(
                                        "text-sm text-[var(--ink3)] mt-1")
                                    for ch in changes:
                                        prop = ch.get("prop", "?")
                                        val = ch.get("value", "?")
                                        old = ch.get("current", "?")
                                        ui.label(
                                            f"  {prop}: {old!r} → {val!r}"
                                        ).classes("mono text-sm")
                                    with ui.row().classes("gap-2 mt-2"):
                                        ui.label(
                                            f"crucible approve {rid} --experiment {exp_num}"
                                        ).classes("mono text-xs text-[var(--ink3)]")
                        else:
                            ui.label("Waiting for diagnosis…").classes(
                                "text-xs text-[var(--ink3)]")

                # Latest campaign result
                if campaign:
                    _render_campaign_card(campaign)

        _refresh()
        # Auto-refresh every 5 s while a run is active (§6 five-minute watchdog
        # cadence; the UI doesn't need to match that exactly, but 5 s keeps it
        # responsive to a proposal appearing).
        ui.timer(5.0, _refresh)


def _render_campaign_card(campaign: dict[str, Any]) -> None:
    run_id = campaign.get("run_id", "?")
    experiments = campaign.get("experiments") or []
    counts = campaign_verdict_counts(campaign)

    with ui.card().classes("w-full"):
        ui.label(f"Run: {run_id}").classes("font-semibold")
        sla_info = campaign.get("sla") or {}
        with ui.row().classes("gap-4 text-xs text-[var(--ink3)]"):
            ui.label(f"SLA: {sla_info.get('name', '?')}")
            ui.label(f"Profile: {campaign.get('profile', '?')}")
            ui.label(f"Experiments: {len(experiments)}")
            ui.label(f"Started: {fmt_when(campaign.get('started_at_epoch_s'))}")

        if counts:
            with ui.row().classes("gap-2 mt-2"):
                for verdict, count in counts.items():
                    _verdict_chip(f"{verdict} ×{count}")

        if not experiments:
            ui.label("No experiments recorded.").classes("text-xs text-[var(--ink3)] mt-2")
            return

        # Experiments table
        with ui.expansion("Experiments", icon="expand_more").classes("w-full mt-2"):
            for exp in experiments:
                n = exp.get("experiment", "?")
                verdict = exp.get("verdict", "?")
                cause = exp.get("diagnosis", {}).get("cause_family", "?") if exp.get("diagnosis") else "?"
                prop = (exp.get("proposal") or {}).get("prop", "?")
                old_val = str((exp.get("before") or {}).get(str(prop), "?"))
                new_val = str((exp.get("proposal") or {}).get("value", "?"))
                with ui.row().classes("items-center gap-2 py-1 text-xs"):
                    ui.label(f"#{n}").classes("mono w-6")
                    _verdict_chip(verdict)
                    ui.label(cause).classes("text-[var(--ink3)]")
                    if prop != "?":
                        ui.label(f"{prop}: {old_val} → {new_val}").classes("mono text-[var(--ink3)]")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@ui.page("/history")
def page_history() -> None:
    with _page_layout("History"):
        ctx = _get_ctx()
        ui.label("History").classes("text-2xl font-bold")

        campaigns, skipped = list_campaigns(ctx)
        if not campaigns:
            with ui.card().classes("w-full bg-[var(--surf1)]"):
                ui.label("No campaigns on disk yet.").classes("text-sm text-[var(--ink3)]")
                ui.code("crucible run … → results/*.json", language="bash").classes("text-xs")
            return

        rc = claim(ctx, ctx.live_results_doc)
        if rc:
            with ui.card().classes("w-full bg-[var(--surf2)]"):
                ui.label("Claim").classes("text-xs text-[var(--ink3)] font-semibold")
                ui.label(rc).classes("text-sm italic")

        with ui.table(
            columns=[
                {"name": "run_id", "label": "Run ID", "field": "run_id", "align": "left"},
                {"name": "sla", "label": "SLA", "field": "sla", "align": "left"},
                {"name": "experiments", "label": "Exps", "field": "experiments", "align": "right"},
                {"name": "verdicts", "label": "Verdicts", "field": "verdicts", "align": "left"},
                {"name": "when", "label": "When", "field": "when", "align": "right"},
            ],
            rows=[
                {
                    "run_id": c.get("run_id", "?"),
                    "sla": (c.get("sla") or {}).get("name", "?"),
                    "experiments": str(len(c.get("experiments") or [])),
                    "verdicts": " / ".join(f"{v}×{n}" for v, n in campaign_verdict_counts(c).items()),
                    "when": fmt_when(c.get("started_at_epoch_s")),
                }
                for c in reversed(campaigns)
            ],
        ).classes("w-full text-sm"):
            pass

        if skipped:
            ui.label(f"{len(skipped)} file(s) skipped.").classes("text-xs text-[var(--err)]")


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

@ui.page("/benchmark")
def page_benchmark() -> None:
    with _page_layout("Benchmark"):
        ctx = _get_ctx()
        ui.label("Benchmark").classes("text-2xl font-bold")

        # Replay results
        replay_runs = list_replay_runs(ctx)
        with ui.card().classes("w-full"):
            ui.label("Replay results").classes("font-semibold text-sm text-[var(--ink3)]")
            if not replay_runs:
                ui.label("No replay results yet.").classes("text-sm text-[var(--ink3)]")
                ui.code("crucible bench replay --tasks config/tasks/ --fixtures config/fixtures/",
                        language="bash").classes("text-xs")
            else:
                latest_replay = replay_runs[-1]
                cases = latest_replay.get("cases") or []
                n_pass = sum(1 for c in cases if c.get("passed"))
                n_total = len(cases)
                with ui.row().classes("gap-4 items-center"):
                    ui.label(f"{n_pass}/{n_total} passed").classes(
                        "text-lg font-bold " + ("text-[var(--ok)]" if n_pass == n_total else "text-[var(--warn)]"))
                    ui.label(f"File: {latest_replay.get('_file', '?')}").classes(
                        "text-xs text-[var(--ink3)]")

                # Task-class breakdown
                by_class: dict[str, list[dict[str, Any]]] = {}
                for c in cases:
                    tc = c.get("task_class", "?")
                    by_class.setdefault(tc, []).append(c)

                with ui.grid(columns=5).classes("gap-2 mt-2"):
                    for cls in sorted(by_class):
                        group = by_class[cls]
                        gpass = sum(1 for c in group if c.get("passed"))
                        with ui.card().classes("text-center p-3"):
                            ui.label(f"Class {cls}").classes("font-bold text-sm")
                            ok = gpass == len(group)
                            ui.label(f"{gpass}/{len(group)}").classes(
                                "text-lg " + ("text-[var(--ok)]" if ok else "text-[var(--warn)]"))

        # Tasks
        tasks = list_tasks(ctx)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Tasks").classes("font-semibold text-sm text-[var(--ink3)]")
                ui.badge(str(len(tasks)), color=None).classes("text-xs bg-[var(--surf2)]")
            if not tasks:
                ui.label("No task YAMLs in config/tasks/.").classes("text-sm text-[var(--ink3)]")
            else:
                with ui.table(
                    columns=[
                        {"name": "id", "label": "ID", "field": "id", "align": "left"},
                        {"name": "class", "label": "Class", "field": "class", "align": "center"},
                        {"name": "name", "label": "Name", "field": "name", "align": "left"},
                        {"name": "fixtures", "label": "Fixtures", "field": "fixtures", "align": "right"},
                    ],
                    rows=[
                        {
                            "id": t.get("id", "?"),
                            "class": t.get("task_class", "?"),
                            "name": t.get("name", "?"),
                            "fixtures": str(len(t.get("fixtures") or [])),
                        }
                        for t in tasks
                    ],
                ).classes("w-full text-sm"):
                    pass

        # Fixtures
        fixtures = list_fixtures(ctx)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Fixtures").classes("font-semibold text-sm text-[var(--ink3)]")
                ui.badge(str(len(fixtures)), color=None).classes("text-xs bg-[var(--surf2)]")
                validated = sum(1 for f in fixtures if f.get("validated_at"))
                ui.label(f"{validated} validated").classes("text-xs text-[var(--ink3)]")
            if not fixtures:
                ui.label("No fixture YAMLs in config/fixtures/.").classes("text-sm text-[var(--ink3)]")
            else:
                with ui.table(
                    columns=[
                        {"name": "id", "label": "ID", "field": "id", "align": "left"},
                        {"name": "cause", "label": "Cause", "field": "cause", "align": "left"},
                        {"name": "providers", "label": "Providers", "field": "providers", "align": "left"},
                        {"name": "validated", "label": "Validated", "field": "validated", "align": "center"},
                    ],
                    rows=[
                        {
                            "id": f.get("id", "?"),
                            "cause": f.get("ground_truth_cause_family", "?"),
                            "providers": ", ".join(f.get("providers") or []),
                            "validated": "✓" if f.get("validated_at") else "—",
                        }
                        for f in fixtures
                    ],
                ).classes("w-full text-sm"):
                    pass


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@ui.page("/settings")
def page_settings() -> None:
    with _page_layout("Settings"):
        ctx = _get_ctx()
        ui.label("Settings").classes("text-2xl font-bold")

        sla, sla_err = load_sla(ctx)
        profile, profile_err = load_profile(ctx)

        with ui.card().classes("w-full"):
            ui.label("Service configuration").classes("font-semibold text-sm text-[var(--ink3)]")
            with ui.grid(columns=2).classes("gap-2 text-sm w-full"):
                ui.label("Root directory:")
                ui.label(str(ctx.root)).classes("mono text-xs")
                ui.label("SLA file:")
                err_cls = "text-[var(--err)]" if sla_err else ""
                ui.label(ctx.sla_path + (f" — {sla_err}" if sla_err else "")).classes(f"mono text-xs {err_cls}")
                ui.label("Profile:")
                err_cls = "text-[var(--err)]" if profile_err else ""
                ui.label(ctx.profile_name + (f" — {profile_err}" if profile_err else "")).classes(
                    f"mono text-xs {err_cls}")
                if sla:
                    ui.label("Target URL:")
                    ui.label(sla.target_base_url).classes("mono text-xs")
                    ui.label("Environment:")
                    env_type = sla.environment_name or "—"
                    ui.label(env_type)
                ui.label("State directory:")
                ui.label(str(ctx.state)).classes("mono text-xs")
                ui.label("Collector version:")
                ui.label(_collector_version()).classes("mono text-xs")

        with ui.card().classes("w-full bg-[var(--surf1)]"):
            ui.label("CLI reference").classes("font-semibold text-sm text-[var(--ink3)]")
            for cmd, desc in [
                ("crucible run …", "Start a campaign"),
                ("crucible approve <run_id> --experiment N", "Approve a proposal"),
                ("crucible pause <run_id>", "Pause (hold without discarding)"),
                ("crucible abort <run_id>", "Abort (discard in-flight)"),
                ("crucible status", "Current campaign status"),
                ("crucible report <results/*.json>", "View a campaign report"),
                ("crucible capture …", "Capture a fixture snapshot"),
                ("crucible bench replay …", "Run replay benchmark"),
                ("crucible score <results/*.json>", "Score a campaign"),
            ]:
                with ui.row().classes("items-baseline gap-2 text-xs"):
                    ui.code(cmd).classes("mono")
                    ui.label(desc).classes("text-[var(--ink3)]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the NiceGUI dev server directly (not through ``crucible serve``)."""
    import argparse

    parser = argparse.ArgumentParser(description="Crucible NiceGUI app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", default=".", help="Crucible workspace root")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("CRUCIBLE_ROOT", str(Path(args.root).resolve()))

    ui.run(
        host=args.host,
        port=args.port,
        title="Crucible",
        favicon="🔥",
        reload=args.reload,
        show=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
