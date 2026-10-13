"""Crucible NiceGUI application: everything the CLI does, from a browser.

DESIGN.md §15 (operator decision, 11 October 2026): the UI does everything the
CLI does, with no easier path to any of it.

How that is held to:

- **Every action is the CLI.** A button builds the argv a person would type
  (:mod:`crucible.ui.actions`) and runs the real ``crucible`` entry point as a
  detached job (:mod:`crucible.ui.jobs`). Defaults, guards, refusals and exit
  codes are the CLI's own; the UI cannot reach a code path the CLI cannot.
- **Approval is bound.** The approve dialog shows the parked parameters and
  offers no value field. ``crucible approve`` copies the values from the parked
  request.
- **Touching the target is confirmed.** ``run``, ``resume``, ``ceiling``, a live
  ``capture`` and ``preflight --apply-probe`` each open a dialog naming the
  target and environment first.
- **Not on loopback, not without a token.** When ``CRUCIBLE_UI_TOKEN`` is set,
  every action needs it; ``serve-ui`` refuses a non-loopback host without one.

Pages read state from disk through :mod:`crucible.ui.perf_data`.

Start: ``crucible serve-ui`` or ``uv run python -m crucible.ui.nicegui_app``.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import app, ui

from . import actions as A
from .jobs import FINISHED, RUNNING, Job, get_job, list_jobs, read_output, start_job
from .perf_data import (
    PerfContext,
    abort_markers,
    active_runs,
    campaign_verdict_counts,
    claim,
    fmt_when,
    latest_campaign,
    list_campaigns,
    list_fixtures,
    list_profiles,
    list_replay_runs,
    list_tasks,
    load_profile,
    load_sla,
    pause_markers,
    paused_runs,
    pending_decisions,
    pending_manual,
)

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
    --ink0: #f0f0f2; --ink1: #d0d0d4; --ink2: #8888a0; --ink3: #8a8a95;
    --surf0: #0e0e0f; --surf1: #17171a; --surf2: #222226;
  }
}
body { background: var(--surf0); color: var(--ink0); margin: 0; }
.n-chip-ok  { background: #1a3a31; color: var(--ok); }
.n-chip-err { background: #3a1a1a; color: var(--err); }
.n-chip-warn{ background: #3a2e0a; color: var(--molten); }
.mono { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 0.85em; }
.joblog { white-space: pre-wrap; max-height: 460px; overflow: auto; background: var(--surf1);
          padding: 10px; border-radius: 6px; width: 100%; }
"""

NAV = [("Home", "/"), ("New", "/new"), ("Campaign", "/campaign"), ("History", "/history"),
       ("Benchmark", "/benchmark"), ("Jobs", "/jobs"), ("Settings", "/settings")]

# ---------------------------------------------------------------------------
# Context, operator identity and the control token
# ---------------------------------------------------------------------------

_ctx: PerfContext | None = None


def _get_ctx() -> PerfContext:
    global _ctx
    if _ctx is None:
        _ctx = PerfContext(root=Path(os.getenv("CRUCIBLE_ROOT", ".")).resolve())
    return _ctx


def _state_arg() -> str:
    return str(_get_ctx().state)


def _user_store() -> dict[str, Any]:
    try:
        return app.storage.user
    except (AssertionError, RuntimeError):
        return {}


def _operator() -> str:
    return str(_user_store().get("operator", ""))


def token_required() -> bool:
    return bool(os.getenv("CRUCIBLE_UI_TOKEN", ""))


def _authorised() -> bool:
    required = os.getenv("CRUCIBLE_UI_TOKEN", "")
    if not required:
        return True
    supplied = str(_user_store().get("token", ""))
    return bool(supplied) and hmac.compare_digest(supplied.encode(), required.encode())


def storage_secret() -> str:
    return os.getenv("CRUCIBLE_UI_SECRET") or secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _page_layout(title: str):
    ui.add_head_html(f"<style>{THEME}</style>")
    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Chivo:wght@400;600;700'
        '&family=JetBrains+Mono&display=swap" rel="stylesheet">'
    )
    ui.page_title(f"Crucible · {title}")
    with ui.header().classes("items-center px-4 gap-1 shadow-sm bg-[var(--surf1)] flex-wrap"):
        with ui.link(target="/").classes("no-underline"):
            ui.html('<span style="font-weight:700;font-size:1.1em;background:linear-gradient('
                    '135deg,#ef6b2f,#d4a843);-webkit-background-clip:text;'
                    '-webkit-text-fill-color:transparent">Crucible</span>')
        ui.space()
        for label, href in NAV:
            ui.link(label, href).classes("text-sm font-medium px-2 py-1")
    with ui.footer().classes("px-4 py-1 text-xs text-[var(--ink3)] bg-[var(--surf1)] flex gap-4"):
        ctx = _get_ctx()
        sla, _ = load_sla(ctx)
        ui.label(f"profile: {ctx.profile_name}")
        ui.label(f"env: {sla.environment_name} ({sla.environment_kind})" if sla else "env: —")
        ui.label(f"collector: {_collector_version()}")
        ui.space()
        ui.label(f"operator: {_operator() or 'not set'}")
        if token_required():
            ui.label("control token: " + ("entered" if _authorised() else "required"))
    return ui.column().classes("max-w-5xl mx-auto px-4 py-6 gap-5 w-full")


def _collector_version() -> str:
    from ..perf.collector import COLLECTOR_VERSION

    return COLLECTOR_VERSION


def _chip(text: str, tone: str = "") -> None:
    ui.badge(text, color=None).classes(f"text-xs {tone}")


def _verdict_chip(verdict: str) -> None:
    key = verdict.split(" ")[0]
    tone = {"IMPROVED": "n-chip-ok", "WORSE": "n-chip-err", "INCONCLUSIVE": "n-chip-warn",
            "ABORTED": "n-chip-warn", "NOT_MEASURED": "n-chip-warn"}.get(key, "")
    _chip(verdict, tone)


def _section(title: str):
    card = ui.card().classes("w-full")
    with card:
        ui.label(title).classes("font-semibold text-sm text-[var(--ink3)]")
    return card


# ---------------------------------------------------------------------------
# Running an action
# ---------------------------------------------------------------------------


def _job_panel(container: ui.element, job_id: str) -> None:
    """A live view of one job's output, polling its log until it finishes."""
    ctx = _get_ctx()
    with container:
        with ui.card().classes("w-full") as card:
            header = ui.row().classes("items-center gap-2 w-full")
            output = ui.label("").classes("mono joblog")
    last: dict[str, Any] = {"status": None, "size": -1}

    def refresh() -> None:
        job = get_job(ctx.state, job_id)
        if job is None:
            output.set_text("job not found")
            timer.deactivate()
            return
        if last["status"] != (job.status, job.exit_code):
            last["status"] = (job.status, job.exit_code)
            header.clear()
            with header:
                _job_status_chip(job)
                ui.label(job.label).classes("font-semibold")
                ui.label(job.command_line).classes("mono text-xs text-[var(--ink3)]")
                ui.space()
                ui.link("open", f"/jobs?id={job.id}").classes("text-xs text-[var(--accent)]")
        text = read_output(job)
        if len(text) != last["size"]:
            last["size"] = len(text)
            output.set_text(text or "(no output yet)")
        if job.status != RUNNING:
            timer.deactivate()

    timer = ui.timer(1.0, refresh)
    card.on("remove", lambda: timer.deactivate())
    refresh()


def _job_status_chip(job: Job) -> None:
    if job.status == RUNNING:
        _chip("RUNNING", "n-chip-warn")
    elif job.status == FINISHED:
        _chip(f"EXIT {job.exit_code}", "n-chip-ok" if job.exit_code == 0 else "n-chip-err")
    else:
        _chip("UNKNOWN", "")


def _target_line() -> str:
    sla, error = load_sla(_get_ctx())
    if sla is None:
        return f"SLA could not be read ({error}); the CLI will refuse if it cannot either."
    return (f"Target {sla.target_base_url} in environment {sla.environment_name} "
            f"({sla.environment_kind}).")


def launch(build: Callable[[], list[str]], label: str, output: ui.element | None) -> None:
    """Build the argv, confirm if it touches the target, then run it as a job."""
    if not _authorised():
        ui.notify("Enter the control token on the Settings page first.", type="negative")
        return
    try:
        args = build()
    except A.ActionError as exc:
        ui.notify(str(exc), type="negative", multi_line=True)
        return

    def go() -> None:
        ctx = _get_ctx()
        job = start_job(ctx.state, ctx.root, args, label)
        ui.notify(f"started: {label}", type="info")
        if output is not None:
            output.clear()
            _job_panel(output, job.id)
        else:
            ui.navigate.to(f"/jobs?id={job.id}")

    if not A.touches_target(args):
        go()
        return
    with ui.dialog() as dialog, ui.card().classes("max-w-xl"):
        ui.label(f"Confirm: {label}").classes("text-lg font-semibold")
        ui.label("This drives load against, changes, or restarts the live target.")
        ui.label(_target_line()).classes("text-sm")
        ui.label("crucible " + " ".join(args)).classes("mono text-xs joblog")
        with ui.row().classes("gap-2 justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat").mark("cancel")
            ui.button("Run it", on_click=lambda: (dialog.close(), go())).classes(
                "bg-[var(--accent)] text-white").mark("run-it")
    dialog.open()


def _num(widget: ui.number | None) -> float | None:
    return None if widget is None or widget.value in (None, "") else widget.value


def _int(widget: ui.number | None) -> int | None:
    value = _num(widget)
    return None if value is None else int(value)


def _profile_options() -> list[str]:
    names = [p.name for p in list_profiles(_get_ctx())]
    return names or [_get_ctx().profile_name]


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


@ui.page("/")
def page_home() -> None:
    with _page_layout("Home"):
        ctx = _get_ctx()
        ui.label("Home").classes("text-2xl font-bold")

        waiting = len(pending_decisions(ctx)) + len(pending_manual(ctx))
        paused = paused_runs(ctx)
        if waiting or paused:
            with ui.card().classes("w-full border border-[var(--molten)]"):
                if waiting:
                    ui.label(f"{waiting} decision(s) waiting on you.").classes("font-semibold")
                if paused:
                    ui.label(f"{len(paused)} paused campaign(s) can be resumed.")
                ui.link("Go to Campaign →", "/campaign").classes("text-sm text-[var(--accent)]")

        with _section("Active campaigns"):
            runs = active_runs(ctx)
            if not runs:
                ui.label("No campaign holds a deploy-branch lock.").classes("text-sm text-[var(--ink3)]")
            for run in runs:
                with ui.row().classes("items-center gap-2"):
                    _chip("RUNNING", "n-chip-ok")
                    ui.label(run.get("run_id", "?")).classes("mono")
                    ui.label(f"branch {run.get('branch', '?')}").classes("text-xs text-[var(--ink3)]")

        with _section("Recent campaigns"):
            campaigns, skipped = list_campaigns(ctx)
            if not campaigns:
                ui.label("No campaign manifests in results/ yet.").classes("text-sm text-[var(--ink3)]")
            else:
                _campaign_table(list(reversed(campaigns[-6:])))
            if skipped:
                ui.label(f"{len(skipped)} file(s) in results/ could not be read.").classes(
                    "text-xs text-[var(--err)]")

        with _section("Recent jobs"):
            jobs = list_jobs(ctx.state, limit=5)
            if not jobs:
                ui.label("Nothing run from the UI yet.").classes("text-sm text-[var(--ink3)]")
            for job in jobs:
                _job_row(job)

        with ui.row().classes("gap-2"):
            ui.button("New campaign", icon="add", on_click=lambda: ui.navigate.to("/new")).classes(
                "bg-[var(--accent)] text-white")
            ui.button("Benchmark", icon="bar_chart", on_click=lambda: ui.navigate.to("/benchmark"))


def _campaign_table(campaigns: list[dict[str, Any]]) -> None:
    ui.table(
        columns=[
            {"name": "run_id", "label": "Run", "field": "run_id", "align": "left"},
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
                "verdicts": " / ".join(f"{v}×{n}" for v, n in campaign_verdict_counts(c).items()) or "—",
                "when": fmt_when(c.get("started_at_epoch_s")),
            }
            for c in campaigns
        ],
    ).classes("w-full text-sm")


def _job_row(job: Job) -> None:
    with ui.row().classes("items-center gap-2 text-sm"):
        _job_status_chip(job)
        ui.link(job.label, f"/jobs?id={job.id}").classes("font-medium")
        ui.label(job.command_line).classes("mono text-xs text-[var(--ink3)]")
        ui.label(fmt_when(job.started_at_epoch_s)).classes("text-xs text-[var(--ink3)]")


# ---------------------------------------------------------------------------
# New: run, plan, preflight, ceiling
# ---------------------------------------------------------------------------


@ui.page("/new")
def page_new() -> None:
    with _page_layout("New"):
        ctx = _get_ctx()
        ui.label("New campaign").classes("text-2xl font-bold")
        sla, sla_err = load_sla(ctx)
        profile, profile_err = load_profile(ctx)
        with _section("What a campaign will run against"):
            if sla_err or profile_err:
                for err in (sla_err, profile_err):
                    if err:
                        ui.label(err).classes("text-[var(--err)] text-sm")
            if sla:
                with ui.grid(columns=2).classes("gap-2 text-sm"):
                    ui.label("Target")
                    ui.label(sla.target_base_url).classes("mono")
                    ui.label("Environment")
                    ui.label(f"{sla.environment_name} ({sla.environment_kind})")
                    ui.label("SLA")
                    ui.label(f"{sla.name}: p99 ≤ {sla.p99_ms} ms, errors ≤ {sla.error_rate_pct}% "
                             f"on {sla.endpoint}")
                    ui.label("Profile")
                    ui.label(profile.name if profile else "—")

        out = ui.column().classes("w-full")

        with _section("1 · Check first"):
            ui.label("plan reads configuration and changes nothing. preflight probes the target; "
                     "with the apply probe it also restarts it.").classes("text-xs text-[var(--ink3)]")
            with ui.row().classes("gap-3 items-end flex-wrap"):
                c_profile = ui.select(_profile_options(), value=ctx.profile_name, label="profile")
                c_sla = ui.input("SLA file", value=ctx.sla_path)
                c_workspace = ui.input("workspace (target repo)", value=os.getenv("CRUCIBLE_WORKSPACE", "."))
                c_probe = ui.checkbox("apply probe (restarts the target)")
            with ui.row().classes("gap-2"):
                ui.button("Plan", icon="visibility", on_click=lambda: launch(
                    lambda: A.plan_args(profile=c_profile.value, sla=c_sla.value), "plan", out)).mark("plan")
                ui.button("Preflight", icon="fact_check", on_click=lambda: launch(
                    lambda: A.preflight_args(profile=c_profile.value, sla=c_sla.value,
                                             workspace=c_workspace.value, apply_probe=c_probe.value),
                    "preflight", out))

        with _section("2 · Start a campaign"):
            with ui.row().classes("gap-3 flex-wrap"):
                r_profile = ui.select(_profile_options(), value=ctx.profile_name, label="profile")
                r_sla = ui.input("SLA file", value=ctx.sla_path)
                r_scenario = ui.input("scenario", value="db-latency")
                r_workspace = ui.input("workspace (target repo)", value=os.getenv("CRUCIBLE_WORKSPACE", "."))
                r_run_id = ui.input("run id (blank = generated)")
            with ui.row().classes("gap-3 flex-wrap"):
                r_users = ui.number("users", value=50, min=1, format="%d")
                r_warmup = ui.number("warmup s (discarded)", value=120, min=0)
                r_measure = ui.number("measure s", value=300, min=1)
                r_experiments = ui.number("max experiments", value=5, min=1, format="%d")
            with ui.row().classes("gap-3 flex-wrap"):
                r_provider = ui.input("model provider", value=os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"))
                r_model = ui.input("model", value=os.getenv("CRUCIBLE_MODEL", ""))
            with ui.expansion("Tracing (optional)").classes("w-full"):
                with ui.row().classes("gap-3 flex-wrap"):
                    r_jaeger = ui.input("Jaeger query URL")
                    r_jaeger_svc = ui.input("Jaeger service name")
                    r_sampling = ui.number("head sampling %, blank if unknown")
            r_request = ui.textarea("stakeholder request (context only; the SLA stays the authority)").classes("w-full")
            ui.label("Approval").classes("text-sm font-semibold mt-2")
            r_approve = ui.radio(
                {"file": "Ask me for every change (recommended)",
                 "preapproved": "Do not ask; recorded on the manifest as preapproved"},
                value="file",
            )
            r_timeout = ui.number("approval timeout s", value=3600, min=1)
            ui.button("Start campaign", icon="play_arrow", on_click=lambda: launch(
                lambda: A.run_args(
                    profile=r_profile.value, sla=r_sla.value, scenario=r_scenario.value,
                    users=_int(r_users), warmup=_num(r_warmup), measure=_num(r_measure),
                    experiments=_int(r_experiments), run_id=r_run_id.value, state_dir=_state_arg(),
                    workspace=r_workspace.value, approve=r_approve.value,
                    approval_timeout=_num(r_timeout), provider=r_provider.value, model=r_model.value,
                    jaeger_url=r_jaeger.value, jaeger_service=r_jaeger_svc.value,
                    trace_sampling_rate=_num(r_sampling), stakeholder_request=r_request.value,
                ),
                "campaign", out)).classes("bg-[var(--accent)] text-white mt-2").mark("start-campaign")

        with _section("3 · Ceiling probe (operator-declared, DESIGN.md §20)"):
            ui.label("Raises load in steps until the SLA is missed. This changes the load profile, "
                     "so only an operator can start it, inside bounds the operator sets.").classes(
                "text-xs text-[var(--ink3)]")
            with ui.row().classes("gap-3 flex-wrap"):
                k_users = ui.number("first step users", value=50, min=1, format="%d")
                k_min = ui.number("min users", value=0, min=0, format="%d")
                k_max = ui.number("max users (required)", value=500, min=1, format="%d")
                k_step = ui.number("step", value=50, min=1, format="%d")
                k_warmup = ui.number("warmup s", value=120, min=0)
                k_measure = ui.number("measure s", value=300, min=1)
                k_scenario = ui.input("scenario (blank = SLA default)")
                k_run_id = ui.input("run id (blank = generated)")
            ui.button("Start ceiling probe", icon="trending_up", on_click=lambda: launch(
                lambda: A.ceiling_args(
                    profile=c_profile.value, sla=c_sla.value, scenario=k_scenario.value,
                    users=_int(k_users), warmup=_num(k_warmup), measure=_num(k_measure),
                    min_users=_int(k_min), max_users=_int(k_max), step=_int(k_step),
                    run_id=k_run_id.value, state_dir=_state_arg(),
                ),
                "ceiling probe", out)).mark("start-ceiling")


# ---------------------------------------------------------------------------
# Campaign: decisions, pause, abort, resume
# ---------------------------------------------------------------------------


def _signature(ctx: PerfContext) -> str:
    latest = latest_campaign(ctx) or {}
    parts = [
        [(r.get("run_id"), r.get("experiment")) for r in pending_decisions(ctx)],
        [(r.get("run_id"), r.get("experiment")) for r in pending_manual(ctx)],
        [r.get("run_id") for r in active_runs(ctx)],
        [r["run_id"] for r in paused_runs(ctx)],
        [r["run_id"] for r in pause_markers(ctx)],
        [r["run_id"] for r in abort_markers(ctx)],
        (latest.get("run_id"), len(latest.get("experiments") or [])),
    ]
    return repr(parts)


@ui.page("/campaign")
def page_campaign() -> None:
    with _page_layout("Campaign"):
        ctx = _get_ctx()
        with ui.row().classes("items-center gap-3 w-full"):
            ui.label("Campaign").classes("text-2xl font-bold")
            ui.button("Status", icon="info", on_click=lambda: launch(
                lambda: A.status_args(state_dir=_state_arg()), "status", out)).props("flat size=sm")
        dialogs = ui.element("div")
        content = ui.column().classes("gap-4 w-full")
        out = ui.column().classes("w-full")
        seen = {"sig": None}

        def refresh(force: bool = False) -> None:
            sig = _signature(ctx)
            if sig == seen["sig"] and not force:
                return
            seen["sig"] = sig
            content.clear()
            with content:
                _render_campaign_state(ctx, dialogs, out)

        refresh(force=True)
        ui.timer(3.0, refresh)


def _ask(host: ui.element, title: str, body: str, fields: list[tuple[str, str, str]],
         confirm: str, on_confirm: Callable[[dict[str, str]], None]) -> None:
    """A dialog collecting a few text fields. ``fields`` is (key, label, default)."""
    with host:
        with ui.dialog() as dialog, ui.card().classes("max-w-xl"):
            ui.label(title).classes("text-lg font-semibold")
            if body:
                ui.label(body).classes("mono text-xs joblog")
            inputs = {key: ui.input(label, value=default).classes("w-full").mark(key)
                      for key, label, default in fields}

            def done() -> None:
                values = {k: str(w.value or "") for k, w in inputs.items()}
                dialog.close()
                on_confirm(values)

            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat").mark("cancel")
                ui.button(confirm, on_click=done).classes("bg-[var(--accent)] text-white").mark("confirm")
    dialog.open()


def _remember_operator(name: str) -> None:
    if name.strip():
        _user_store()["operator"] = name.strip()


def _render_campaign_state(ctx: PerfContext, dialogs: ui.element, out: ui.element) -> None:
    decisions = pending_decisions(ctx)
    manual = pending_manual(ctx)
    with _section("Waiting on you"):
        if not decisions and not manual:
            ui.label("Nothing is waiting for a decision.").classes("text-sm text-[var(--ink3)]")
        for request in decisions:
            _approval_card(request, dialogs, out)
        for step in manual:
            _manual_card(step, dialogs, out)

    running = active_runs(ctx)
    with _section("Running"):
        if not running:
            ui.label("No campaign holds a deploy-branch lock.").classes("text-sm text-[var(--ink3)]")
        for run in running:
            rid = str(run.get("run_id", ""))
            with ui.row().classes("items-center gap-2 w-full"):
                _chip("RUNNING", "n-chip-ok")
                ui.label(rid).classes("mono font-semibold")
                ui.label(f"branch {run.get('branch', '?')}").classes("text-xs text-[var(--ink3)]")
                ui.space()
                ui.button("Pause", icon="pause", on_click=lambda r=rid: _ask(
                    dialogs, f"Pause {r}",
                    "Stops at the next experiment boundary. Verified experiments are kept; "
                    "the in-flight one is re-run on resume.",
                    [("reason", "reason (optional)", "")], "Pause",
                    lambda v, r=r: launch(lambda: A.pause_args(
                        run_id=r, reason=v["reason"], state_dir=_state_arg()), f"pause {r}", out),
                )).props("outline size=sm")
                ui.button("Abort", icon="stop", on_click=lambda r=rid: _ask(
                    dialogs, f"Abort {r}",
                    "Discards the in-flight experiment at the next boundary. Verified experiments "
                    "are kept. If a change was deployed, the target is rolled back to the last "
                    "commit it confirmed running.\n\n" + _target_line(),
                    [("reason", "reason (optional)", "")], "Abort",
                    lambda v, r=r: launch(lambda: A.abort_args(
                        run_id=r, reason=v["reason"], state_dir=_state_arg()), f"abort {r}", out),
                )).props("outline size=sm color=negative").mark(f"abort-{rid}")

    requested_pause, requested_abort = pause_markers(ctx), abort_markers(ctx)
    if requested_pause or requested_abort:
        with _section("Requested, not yet reached"):
            for marker, kind, builder in (
                *[(m, "pause", A.pause_args) for m in requested_pause],
                *[(m, "abort", A.abort_args) for m in requested_abort],
            ):
                rid = marker["run_id"]
                with ui.row().classes("items-center gap-2"):
                    _chip(kind.upper(), "n-chip-warn")
                    ui.label(rid).classes("mono")
                    ui.label(str(marker.get("reason") or "")).classes("text-xs text-[var(--ink3)]")
                    ui.button("Clear", on_click=lambda r=rid, b=builder, k=kind: launch(
                        lambda: b(run_id=r, clear=True, state_dir=_state_arg()),
                        f"clear {k} {r}", out)).props("flat size=sm")

    paused = paused_runs(ctx)
    with _section("Paused (resumable)"):
        if not paused:
            ui.label("No paused campaigns.").classes("text-sm text-[var(--ink3)]")
        for item in paused:
            rid = item["run_id"]
            with ui.row().classes("items-center gap-2"):
                _chip("PAUSED", "n-chip-warn")
                ui.label(rid).classes("mono")
                ui.button("Resume", icon="play_arrow", on_click=lambda r=rid: _ask(
                    dialogs, f"Resume {r}", "Leave blank to reuse what the campaign started with.",
                    [("profile", "profile", ""), ("sla", "SLA file", ""), ("scenario", "scenario", ""),
                     ("workspace", "workspace", os.getenv("CRUCIBLE_WORKSPACE", ""))],
                    "Next",
                    lambda v, r=r: launch(lambda: A.resume_args(
                        run_id=r, state_dir=_state_arg(), profile=v["profile"], sla=v["sla"],
                        scenario=v["scenario"], workspace=v["workspace"]), f"resume {r}", out),
                )).props("size=sm")
                ui.button("Clear pause state", on_click=lambda r=rid: launch(
                    lambda: A.pause_args(run_id=r, clear=True, state_dir=_state_arg()),
                    f"clear pause {r}", out)).props("flat size=sm")

    campaign = latest_campaign(ctx)
    if campaign:
        _render_campaign_card(campaign)


def _approval_card(request: dict[str, Any], dialogs: ui.element, out: ui.element) -> None:
    rid, exp = str(request.get("run_id", "")), int(request.get("experiment", 0))
    params = request.get("params") or {}
    with ui.card().classes("w-full border border-[var(--molten)] bg-[var(--surf1)]"):
        with ui.row().classes("items-center gap-2"):
            _chip("AWAITING APPROVAL", "n-chip-warn")
            ui.label(f"{rid} · experiment {exp}").classes("mono font-semibold")
        ui.label(str(request.get("rendered", ""))).classes("mono text-xs joblog")
        binding = ", ".join(f"{k} -> {v!r}" for k, v in params.items()) or "(no parameters)"

        def answer(reject: bool) -> None:
            verb = "Reject" if reject else "Approve"
            _ask(
                dialogs, f"{verb} {rid} experiment {exp}",
                f"You are about to {verb.lower()} exactly:\n  {binding}\n\nTo try a different value, "
                "reject, and the agent proposes again.",
                [("responder", "your name (recorded on the manifest)", _operator()),
                 ("reason", "reason (optional)", "")],
                verb,
                lambda v: (_remember_operator(v["responder"]), launch(
                    lambda: A.approve_args(run_id=rid, experiment=exp, responder=v["responder"],
                                           reject=reject, reason=v["reason"], state_dir=_state_arg()),
                    f"{verb.lower()} {rid} #{exp}", out)),
            )

        with ui.row().classes("gap-2"):
            ui.button("Approve", icon="check", on_click=lambda: answer(False)).classes(
                "bg-[var(--patina)] text-white").mark(f"approve-{rid}-{exp}")
            ui.button("Reject", icon="close", on_click=lambda: answer(True)).props(
                "outline color=negative").mark(f"reject-{rid}-{exp}")


def _manual_card(step: dict[str, Any], dialogs: ui.element, out: ui.element) -> None:
    rid, exp = str(step.get("run_id", "")), int(step.get("experiment", 0))
    with ui.card().classes("w-full border border-[var(--molten)] bg-[var(--surf1)]"):
        with ui.row().classes("items-center gap-2"):
            _chip("MANUAL STEP", "n-chip-warn")
            ui.label(f"{rid} · experiment {exp}").classes("mono font-semibold")
        ui.label(str(step.get("instructions", ""))).classes("mono text-xs joblog")
        ui.button("I have done this", icon="task_alt", on_click=lambda: _ask(
            dialogs, "Confirm the manual step",
            "This reports that you did it. It approves nothing; the campaign re-checks the "
            "target before it measures.",
            [("responder", "your name", _operator()), ("reason", "note (optional)", "")],
            "Confirm",
            lambda v: (_remember_operator(v["responder"]), launch(
                lambda: A.approve_args(run_id=rid, experiment=exp, responder=v["responder"],
                                       manual_step_done=True, reason=v["reason"],
                                       state_dir=_state_arg()),
                f"manual step done {rid} #{exp}", out)),
        ))


def _render_campaign_card(campaign: dict[str, Any]) -> None:
    experiments = campaign.get("experiments") or []
    with _section(f"Latest campaign · {campaign.get('run_id', '?')}"):
        sla_info = campaign.get("sla") or {}
        with ui.row().classes("gap-4 text-xs text-[var(--ink3)]"):
            ui.label(f"SLA {sla_info.get('name', '?')}")
            ui.label(f"profile {campaign.get('profile', '?')}")
            ui.label(f"started {fmt_when(campaign.get('started_at_epoch_s'))}")
            if campaign.get("stopped_reason"):
                ui.label(f"stopped: {campaign['stopped_reason']}")
        if not experiments:
            ui.label("No experiments recorded.").classes("text-xs text-[var(--ink3)]")
        for exp in experiments:
            changes = (exp.get("proposal") or {}).get("changes") or []
            approval = exp.get("approval") or {}
            with ui.row().classes("items-center gap-2 py-1 text-xs flex-wrap"):
                ui.label(f"#{exp.get('experiment', '?')}").classes("mono w-6")
                _verdict_chip(str(exp.get("verdict", "?")))
                ui.label(str(exp.get("cause_family") or "—")).classes("text-[var(--ink3)]")
                for change in changes:
                    ui.label(f"{change.get('prop')}: {change.get('previous', '?')} → "
                             f"{change.get('value')}").classes("mono")
                if approval.get("state"):
                    ui.label(f"{approval['state']} by {approval.get('responder', '?')}").classes(
                        "text-[var(--ink3)]")
                if exp.get("verdict_reason"):
                    ui.label(str(exp["verdict_reason"])).classes("text-[var(--ink3)]")


# ---------------------------------------------------------------------------
# History: report, diff, score
# ---------------------------------------------------------------------------


@ui.page("/history")
def page_history() -> None:
    with _page_layout("History"):
        ctx = _get_ctx()
        ui.label("History").classes("text-2xl font-bold")
        campaigns, skipped = list_campaigns(ctx)
        out = ui.column().classes("w-full")
        if not campaigns:
            with _section("Campaigns"):
                ui.label("No campaign manifests in results/ yet.").classes("text-sm text-[var(--ink3)]")
        else:
            rc = claim(ctx, ctx.live_results_doc)
            if rc:
                with ui.card().classes("w-full bg-[var(--surf2)]"):
                    ui.label("Claim (docs/BENCHMARK_LIVE_RESULTS.md)").classes(
                        "text-xs text-[var(--ink3)] font-semibold")
                    ui.label(rc).classes("text-sm italic")
            with _section("Campaigns"):
                _campaign_table(list(reversed(campaigns)))
            ids = [str(c.get("run_id")) for c in reversed(campaigns) if c.get("run_id")]
            with _section("Report"):
                with ui.row().classes("gap-3 items-end"):
                    rep = ui.select(ids, value=ids[0] if ids else None, label="run")
                    ui.button("Show report", icon="description", on_click=lambda: launch(
                        lambda: A.report_args(run=rep.value or ""), f"report {rep.value}", out))
            with _section("Compare two campaigns"):
                ui.label("Scenario or load-profile differences refuse; every other difference is "
                         "listed above the figures.").classes("text-xs text-[var(--ink3)]")
                with ui.row().classes("gap-3 items-end"):
                    da = ui.select(ids, value=ids[1] if len(ids) > 1 else (ids[0] if ids else None), label="A")
                    db = ui.select(ids, value=ids[0] if ids else None, label="B")
                    ui.button("Diff", icon="compare_arrows", on_click=lambda: launch(
                        lambda: A.diff_args(run_a=da.value or "", run_b=db.value or ""),
                        f"diff {da.value} vs {db.value}", out))
        with _section("Score (reads manifests; calls no model)"):
            with ui.row().classes("gap-3 items-end flex-wrap"):
                s_journal = ui.input("journal", value=ctx.results_dir)
                s_fixtures = ui.input("fixtures dir (trap properties)", value=ctx.fixture_config_dir)
                s_truth = ui.input("ground-truth YAML (optional)")
                ui.button("Score", icon="grading", on_click=lambda: launch(
                    lambda: A.score_args(journal=s_journal.value, fixtures=s_fixtures.value,
                                         ground_truth=s_truth.value), "score", out))
        if skipped:
            ui.label(f"{len(skipped)} file(s) in results/ could not be read: "
                     + ", ".join(name for name, _ in skipped)).classes("text-xs text-[var(--err)]")


# ---------------------------------------------------------------------------
# Benchmark: replay results, bench, capture
# ---------------------------------------------------------------------------


def _replay_summary(runs: list[dict[str, Any]]) -> None:
    from ..perf.scorer import NOT_GRADED, PASS, grade_replay_case

    latest = runs[-1]
    cases = latest.get("cases") or []
    by_class: dict[str, list[str]] = {}
    for case in cases:
        grade, _ = grade_replay_case(case)
        by_class.setdefault(str(case.get("task_class", "?"))[:1].upper(), []).append(grade)
    graded = [g for grades in by_class.values() for g in grades if g != NOT_GRADED]
    passed = sum(1 for g in graded if g == PASS)
    not_graded = sum(1 for grades in by_class.values() for g in grades if g == NOT_GRADED)
    with ui.row().classes("gap-4 items-center"):
        ui.label(f"{passed}/{len(graded)} passed").classes("text-lg font-bold")
        if not_graded:
            ui.label(f"{not_graded} not graded (errors or no ground truth)").classes(
                "text-sm text-[var(--warn)]")
        ui.label(f"file {latest.get('_file', '?')}").classes("text-xs text-[var(--ink3)]")
    with ui.row().classes("gap-2 mt-2 flex-wrap"):
        for cls in sorted(by_class):
            grades = by_class[cls]
            g = [x for x in grades if x != NOT_GRADED]
            with ui.card().classes("text-center p-3"):
                ui.label(f"Class {cls}").classes("font-bold text-sm")
                ui.label(f"{sum(1 for x in g if x == PASS)}/{len(g)}" if g else "not graded").classes("text-lg")


@ui.page("/benchmark")
def page_benchmark() -> None:
    with _page_layout("Benchmark"):
        ctx = _get_ctx()
        ui.label("Benchmark").classes("text-2xl font-bold")
        out = ui.column().classes("w-full")

        with _section("Latest replay (graded as `crucible score` grades it)"):
            runs = list_replay_runs(ctx)
            if not runs:
                ui.label("No replay results yet.").classes("text-sm text-[var(--ink3)]")
            else:
                _replay_summary(runs)

        with _section("Run the replay benchmark (calls the model, no live target)"):
            with ui.row().classes("gap-3 flex-wrap items-end"):
                b_tasks = ui.input("tasks", value=ctx.tasks_dir)
                b_fixtures = ui.input("captured fixtures dir", value=ctx.results_dir)
                b_out = ui.input("output", value="results/replay.json")
                b_provider = ui.input("provider", value=os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"))
                b_model = ui.input("model", value=os.getenv("CRUCIBLE_MODEL", ""))
                b_skip = ui.checkbox("skip tasks with missing fixtures", value=True)
            ui.button("Run bench", icon="science", on_click=lambda: launch(
                lambda: A.bench_args(tasks=b_tasks.value, fixtures=b_fixtures.value, out=b_out.value,
                                     provider=b_provider.value, model=b_model.value,
                                     skip_missing_fixtures=b_skip.value), "bench", out))

        fixtures = list_fixtures(ctx)
        with _section("Capture a fixture (drives load against the live target)"):
            ids = [str(f.get("id")) for f in fixtures if f.get("id")]
            with ui.row().classes("gap-3 flex-wrap items-end"):
                f_id = ui.select(ids, value=ids[0] if ids else None, label="fixture")
                f_provider = ui.select(["actuator", "promql", "datadog"], value="actuator", label="provider")
                f_all = ui.checkbox("all configured providers from one load")
                f_promql = ui.input("PromQL URL (for all providers)")
                f_instance = ui.input("PromQL instance label")
                f_out = ui.input("output dir", value=ctx.results_dir)
                f_reval = ui.number("revalidate N runs first (0 = no)", value=0, min=0, format="%d")
            ui.label("Datadog keys come from the environment ($DATADOG_API_KEY, $DATADOG_APP_KEY), "
                     "never from this form.").classes("text-xs text-[var(--ink3)]")

            def capture(plan: bool) -> None:
                launch(lambda: A.capture_args(
                    fixture_id=f_id.value or "", provider=f_provider.value, all_providers=f_all.value,
                    promql_url=f_promql.value, promql_instance=f_instance.value, out=f_out.value,
                    revalidate=_int(f_reval) or 0, show_plan=plan,
                ), "capture plan" if plan else f"capture {f_id.value}", out)

            with ui.row().classes("gap-2"):
                ui.button("Show capture plan", icon="list", on_click=lambda: capture(True))
                ui.button("Capture", icon="camera", on_click=lambda: capture(False))

        with _section("Tasks and fixtures"):
            tasks = list_tasks(ctx)
            ui.table(
                columns=[{"name": k, "label": k, "field": k, "align": "left"}
                         for k in ("id", "class", "name", "fixtures")],
                rows=[{"id": t.get("id"), "class": t.get("task_class"), "name": t.get("name"),
                       "fixtures": ", ".join(t.get("fixtures") or [])} for t in tasks],
            ).classes("w-full text-sm")
            ui.table(
                columns=[{"name": k, "label": k, "field": k, "align": "left"}
                         for k in ("id", "cause", "providers", "validated")],
                rows=[{"id": f.get("id"),
                       "cause": f.get("cause_family") or f.get("ground_truth_cause_family") or "(healthy)",
                       "providers": ", ".join(f.get("providers") or []),
                       "validated": f.get("validated_at") or "not yet"} for f in fixtures],
            ).classes("w-full text-sm")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@ui.page("/jobs")
def page_jobs(id: str = "") -> None:  # noqa: A002 -- the query parameter's name
    with _page_layout("Jobs"):
        ctx = _get_ctx()
        ui.label("Jobs").classes("text-2xl font-bold")
        ui.label("Every action taken from this UI, run as the crucible CLI. Output is kept under "
                 f"{ctx.state / 'ui-jobs'}.").classes("text-xs text-[var(--ink3)]")
        if id:
            panel = ui.column().classes("w-full")
            if get_job(ctx.state, id) is None:
                ui.label(f"No job {id!r}.").classes("text-[var(--err)]")
            else:
                _job_panel(panel, id)
        with _section("All jobs"):
            jobs = list_jobs(ctx.state)
            if not jobs:
                ui.label("Nothing run from the UI yet.").classes("text-sm text-[var(--ink3)]")
            for job in jobs:
                _job_row(job)


# ---------------------------------------------------------------------------
# Settings: identity, token, init, export
# ---------------------------------------------------------------------------


@ui.page("/settings")
def page_settings() -> None:
    with _page_layout("Settings"):
        ctx = _get_ctx()
        ui.label("Settings").classes("text-2xl font-bold")
        out = ui.column().classes("w-full")
        sla, sla_err = load_sla(ctx)
        profile, profile_err = load_profile(ctx)

        with _section("You"):
            name = ui.input("your name (recorded on every approval)", value=_operator())
            token = ui.input("control token", password=True, password_toggle_button=True) if token_required() else None

            def save() -> None:
                store = _user_store()
                store["operator"] = str(name.value or "").strip()
                if token is not None:
                    store["token"] = str(token.value or "")
                ui.notify("saved" + ("" if _authorised() else " -- the token does not match"),
                          type="positive" if _authorised() else "negative")

            ui.button("Save", on_click=save)

        with _section("Configuration"):
            with ui.grid(columns=2).classes("gap-2 text-sm w-full"):
                for label, value, err in (
                    ("Root", str(ctx.root), ""),
                    ("SLA file", ctx.sla_path, sla_err),
                    ("Profile", ctx.profile_name, profile_err),
                    ("Target", sla.target_base_url if sla else "—", ""),
                    ("Environment", f"{sla.environment_name} ({sla.environment_kind})" if sla else "—", ""),
                    ("State directory", str(ctx.state), ""),
                    ("Collector", _collector_version(), ""),
                ):
                    ui.label(label)
                    ui.label(value + (f" -- {err}" if err else "")).classes(
                        "mono text-xs" + (" text-[var(--err)]" if err else ""))

        with _section("Workspace"):
            with ui.row().classes("gap-3 items-end flex-wrap"):
                e_profile = ui.select(_profile_options(), value=ctx.profile_name, label="profile")
                e_sla = ui.input("SLA file", value=ctx.sla_path)
                ui.button("Init state directory", on_click=lambda: launch(
                    lambda: A.init_args(state_dir=_state_arg(), sla=e_sla.value), "init", out))
                ui.button("Export collection (no credentials)", on_click=lambda: launch(
                    lambda: A.export_args(profile=e_profile.value, sla=e_sla.value), "export", out))

        with _section("Every CLI command, and where it is here"):
            for command, where in (
                ("plan, preflight, run, ceiling", "New"),
                ("status, approve, pause, abort, resume", "Campaign"),
                ("report, diff, score", "History"),
                ("bench, capture", "Benchmark"),
                ("init, export", "Settings"),
            ):
                with ui.row().classes("gap-2 text-xs"):
                    ui.label(command).classes("mono")
                    ui.label(f"→ {where}").classes("text-[var(--ink3)]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def check_host(host: str) -> None:
    """A UI that can start campaigns and approve changes is not left open on a network."""
    if host not in LOOPBACK and not token_required():
        raise SystemExit(
            f"refusing to serve the UI on {host!r} without CRUCIBLE_UI_TOKEN: anyone who can "
            "reach it could start campaigns and approve changes. Set the token, or bind to "
            "127.0.0.1 and use an SSH tunnel."
        )


def run(host: str = "127.0.0.1", port: int = 8765, *, reload: bool = False) -> None:
    check_host(host)
    ui.run(host=host, port=port, title="Crucible", favicon="🔥", reload=reload, show=False,
           storage_secret=storage_secret())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Crucible NiceGUI app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", default=".", help="Crucible workspace root")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("CRUCIBLE_ROOT", str(Path(args.root).resolve()))
    run(args.host, args.port, reload=args.reload)


if __name__ in {"__main__", "__mp_main__"}:
    main()
