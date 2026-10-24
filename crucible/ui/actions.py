"""One argument builder per CLI command, so the UI can do everything the CLI does.

DESIGN.md §15 (operator decision, 11 October 2026): the UI reaches every
capability the CLI does, and gets no easier path to any of them. The UI does not
call ``crucible.perf.commands`` itself. It builds the argv a person would type and
runs the real ``crucible`` entry point (:mod:`crucible.ui.jobs`). Parsing,
defaults, guards and exit codes are therefore the CLI's own, and a builder can
only offer what the parser accepts.

Two consequences matter enough to state:

- ``approve_args`` has no parameter for a value. ``crucible approve`` copies the
  parameters from the parked request (assertion 13.2); a UI field for a value
  would be the blank cheque that rule exists to prevent.
- Every free-text option is passed as a single ``--name=value`` token, so a value
  beginning with ``-`` cannot be read as a flag. Positional run ids are checked
  against :data:`RUN_ID` for the same reason.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: A run id, fixture id or similar name. No leading dash, no path separators.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class ActionError(ValueError):
    """A form value the CLI would not accept, refused before anything runs."""


def _name(value: str, what: str) -> str:
    value = (value or "").strip()
    if not RUN_ID.match(value):
        raise ActionError(f"{what} {value!r} must be letters, digits, '.', '_' or '-' and not start with '-'")
    return value


def _text(value: Any) -> str:
    text = "" if value is None else str(value)
    if "\x00" in text:
        raise ActionError("a value contains a NUL byte")
    return text.strip()


def _opt(args: list[str], flag: str, value: Any) -> None:
    """Append ``--flag=value`` when a value was given; omit it to keep the CLI default."""
    text = _text(value)
    if text:
        args.append(f"--{flag}={text}")


def _num(args: list[str], flag: str, value: Any, kind: type = int) -> None:
    if value is None or value == "":
        return
    try:
        number = kind(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"--{flag} must be a number, got {value!r}") from exc
    args.append(f"--{flag}={number}")


def _flag(args: list[str], flag: str, on: bool) -> None:
    if on:
        args.append(f"--{flag}")


# ---------------------------------------------------------------------------
# Builders, one per command, in the CLI's own order
# ---------------------------------------------------------------------------


def init_args(*, state_dir: str = "", sla: str = "") -> list[str]:
    args = ["init"]
    _opt(args, "state-dir", state_dir)
    _opt(args, "sla", sla)
    return args


def plan_args(*, profile: str = "", sla: str = "") -> list[str]:
    args = ["plan"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    return args


def preflight_args(
    *, profile: str = "", sla: str = "", workspace: str = "", apply_probe: bool = False,
    probe_timeout: float | None = None,
) -> list[str]:
    args = ["preflight"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "workspace", workspace)
    _num(args, "probe-timeout", probe_timeout, float)
    _flag(args, "apply-probe", apply_probe)
    return args


def run_args(
    *, profile: str = "", sla: str = "", scenario: str = "", users: int | None = None,
    warmup: float | None = None, measure: float | None = None, experiments: int | None = None,
    run_id: str = "", state_dir: str = "", workspace: str = "", approve: str = "file",
    approval_timeout: float | None = None, provider: str = "", model: str = "",
    jaeger_url: str = "", jaeger_service: str = "", trace_sampling_rate: float | None = None,
    stakeholder_request: str = "", spawn_rate: float | None = None,
    cost_cap: float | None = None, wall_clock_cap: float | None = None,
) -> list[str]:
    if approve not in ("file", "preapproved"):
        raise ActionError(f"approve mode must be 'file' or 'preapproved', got {approve!r}")
    args = ["run"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "scenario", scenario)
    _num(args, "users", users)
    _num(args, "warmup", warmup, float)
    _num(args, "measure", measure, float)
    _num(args, "spawn-rate", spawn_rate, float)
    _num(args, "experiments", experiments)
    _num(args, "cost-cap", cost_cap, float)
    _num(args, "wall-clock-cap", wall_clock_cap, float)
    if _text(run_id):
        args.append(f"--run-id={_name(run_id, 'run id')}")
    _opt(args, "state-dir", state_dir)
    _opt(args, "workspace", workspace)
    args.append(f"--approve={approve}")
    _num(args, "approval-timeout", approval_timeout, float)
    _opt(args, "provider", provider)
    _opt(args, "model", model)
    _opt(args, "jaeger-url", jaeger_url)
    _opt(args, "jaeger-service", jaeger_service)
    _num(args, "trace-sampling-rate", trace_sampling_rate, float)
    _opt(args, "stakeholder-request", stakeholder_request)
    return args


def status_args(*, run_id: str = "", state_dir: str = "") -> list[str]:
    args = ["status"]
    _opt(args, "state-dir", state_dir)
    if _text(run_id):
        args.append(_name(run_id, "run id"))
    return args


def approve_args(
    *, run_id: str, experiment: int, responder: str, reject: bool = False,
    manual_step_done: bool = False, reason: str = "", state_dir: str = "",
) -> list[str]:
    """No value parameter, by design (assertion 13.2)."""
    if not _text(responder):
        raise ActionError("say who is answering: the responder is recorded on the manifest")
    if reject and manual_step_done:
        raise ActionError("a manual-step confirmation is not an approval, so it cannot be a rejection")
    args = ["approve", _name(run_id, "run id"), f"--experiment={int(experiment)}",
            f"--as={_text(responder)}"]
    _flag(args, "reject", reject)
    _flag(args, "manual-step-done", manual_step_done)
    _opt(args, "reason", reason)
    _opt(args, "state-dir", state_dir)
    return args


def abort_args(*, run_id: str, reason: str = "", clear: bool = False, state_dir: str = "") -> list[str]:
    args = ["abort", _name(run_id, "run id")]
    _opt(args, "reason", reason)
    _flag(args, "clear", clear)
    _opt(args, "state-dir", state_dir)
    return args


def pause_args(*, run_id: str, reason: str = "", clear: bool = False, state_dir: str = "") -> list[str]:
    args = ["pause", _name(run_id, "run id")]
    _opt(args, "reason", reason)
    _flag(args, "clear", clear)
    _opt(args, "state-dir", state_dir)
    return args


def resume_args(
    *, run_id: str, state_dir: str = "", profile: str = "", sla: str = "", scenario: str = "",
    workspace: str = "",
) -> list[str]:
    args = ["resume", _name(run_id, "run id")]
    _opt(args, "state-dir", state_dir)
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "scenario", scenario)
    _opt(args, "workspace", workspace)
    return args


def ceiling_args(
    *, profile: str = "", sla: str = "", scenario: str = "", users: int | None = None,
    warmup: float | None = None, measure: float | None = None, min_users: int | None = None,
    max_users: int | None = None, step: int | None = None, run_id: str = "", state_dir: str = "",
    spawn_rate: float | None = None,
) -> list[str]:
    """DESIGN.md §20.2: the operator sets the bounds, so the maximum is required."""
    if max_users in (None, ""):
        raise ActionError("a ceiling probe needs a maximum user count (DESIGN.md 20.2)")
    args = ["ceiling"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "scenario", scenario)
    _num(args, "users", users)
    _num(args, "warmup", warmup, float)
    _num(args, "measure", measure, float)
    _num(args, "min-users", min_users)
    _num(args, "max-users", max_users)
    _num(args, "step", step)
    _num(args, "spawn-rate", spawn_rate, float)
    if _text(run_id):
        args.append(f"--run-id={_name(run_id, 'run id')}")
    _opt(args, "state-dir", state_dir)
    return args


def score_args(*, journal: str = "", fixtures: str = "", ground_truth: str = "", json_out: str = "") -> list[str]:
    args = ["score"]
    _opt(args, "journal", journal)
    _opt(args, "fixtures", fixtures)
    _opt(args, "ground-truth", ground_truth)
    _opt(args, "json-out", json_out)
    return args


def report_args(*, journal: str = "", run: str = "", json_out: str = "") -> list[str]:
    args = ["report"]
    _opt(args, "journal", journal)
    if _text(run):
        args.append(f"--run={_name(run, 'run id')}")
    _opt(args, "json-out", json_out)
    return args


def diff_args(*, run_a: str, run_b: str, journal: str = "", json_out: str = "") -> list[str]:
    args = ["diff", f"--a={_name(run_a, 'run A')}", f"--b={_name(run_b, 'run B')}"]
    _opt(args, "journal", journal)
    _opt(args, "json-out", json_out)
    return args


def capture_args(
    *, fixture_id: str = "", fixture_config: str = "", out: str = "", profile: str = "",
    sla: str = "", scenario: str = "", users: int | None = None, warmup: float | None = None,
    measure: float | None = None, provider: str = "", tag: str = "", revalidate: int | None = None,
    show_plan: bool = False, all_providers: bool = False, promql_url: str = "",
    promql_instance: str = "", datadog_url: str = "",
) -> list[str]:
    """Datadog keys are never a UI field: the CLI reads them from the environment,
    so they never enter argv, a job record or a process listing (DESIGN.md 19.8)."""
    args = ["capture"]
    if _text(fixture_id):
        args.append(f"--fixture={_name(fixture_id, 'fixture id')}")
    _opt(args, "fixture-config", fixture_config)
    _opt(args, "out", out)
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "scenario", scenario)
    _num(args, "users", users)
    _num(args, "warmup", warmup, float)
    _num(args, "measure", measure, float)
    _opt(args, "provider", provider)
    _opt(args, "tag", tag)
    if revalidate:
        _num(args, "revalidate", revalidate)
    _flag(args, "plan", show_plan)
    _flag(args, "all-providers", all_providers)
    _opt(args, "promql-url", promql_url)
    _opt(args, "promql-instance", promql_instance)
    _opt(args, "datadog-url", datadog_url)
    if not show_plan and not _text(fixture_id) and not revalidate:
        raise ActionError("choose a fixture to capture, or ask for the plan")
    return args


def export_args(*, profile: str = "", sla: str = "", out: str = "") -> list[str]:
    args = ["export"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "out", out)
    return args


def bench_args(
    *, tasks: str, fixtures: str, profile: str = "", sla: str = "", out: str = "",
    provider: str = "", model: str = "", skip_missing_fixtures: bool = False,
) -> list[str]:
    if not _text(tasks) or not _text(fixtures):
        raise ActionError("bench needs a task set and a fixture directory")
    args = ["bench", f"--tasks={_text(tasks)}", f"--fixtures={_text(fixtures)}"]
    _opt(args, "profile", profile)
    _opt(args, "sla", sla)
    _opt(args, "out", out)
    _opt(args, "provider", provider)
    _opt(args, "model", model)
    _flag(args, "skip-missing-fixtures", skip_missing_fixtures)
    return args


#: Every CLI command and its builder. ``serve`` and ``serve-ui`` start servers
#: and are not actions. A test asserts this covers every other subcommand, so a
#: command added to the CLI without a UI route fails the suite.
ACTIONS: dict[str, Callable[..., list[str]]] = {
    "init": init_args,
    "plan": plan_args,
    "preflight": preflight_args,
    "run": run_args,
    "status": status_args,
    "approve": approve_args,
    "abort": abort_args,
    "pause": pause_args,
    "resume": resume_args,
    "ceiling": ceiling_args,
    "score": score_args,
    "report": report_args,
    "diff": diff_args,
    "capture": capture_args,
    "export": export_args,
    "bench": bench_args,
}

#: Commands that drive or restart the live target. The UI confirms each one,
#: naming the target and environment, before it starts (DESIGN.md §15).
TOUCHES_TARGET = frozenset({"run", "resume", "ceiling", "capture"})


def touches_target(args: list[str]) -> bool:
    """True when this argv will load, change or restart the target."""
    if not args:
        return False
    command = args[0]
    if command == "preflight":
        return "--apply-probe" in args
    if command == "capture":
        return "--plan" not in args
    return command in TOUCHES_TARGET


__all__ = [
    "ACTIONS",
    "RUN_ID",
    "TOUCHES_TARGET",
    "ActionError",
    *[f.__name__ for f in ACTIONS.values()],
    "touches_target",
]
