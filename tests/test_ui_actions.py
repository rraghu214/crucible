"""The UI's argument builders: one per CLI command, and no easier path than the CLI.

DESIGN.md §15 (operator decision, 11 October 2026): the UI does everything the
CLI does. These tests hold the two halves of that:

- **coverage**: every CLI subcommand has a builder, and each builder's argv
  parses and reaches the same ``cmd_*`` function as typing it would;
- **no easier path**: approval cannot carry a value, a value cannot smuggle in
  a flag, and commands that touch the target are marked for confirmation.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from crucible.cli import build_parser, main
from crucible.perf import commands
from crucible.ui import actions as A


def _subcommands() -> set[str]:
    return set(build_parser()._subparsers._group_actions[0].choices)


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


#: (builder, kwargs, the cmd_* function the argv must reach)
SAMPLES = [
    (A.init_args, {"state_dir": "/tmp/s"}, "cmd_init"),
    (A.plan_args, {"profile": "spring-boot"}, "cmd_plan"),
    (A.preflight_args, {"apply_probe": True}, "cmd_preflight"),
    (A.run_args, {"run_id": "bench-t1", "users": 50, "stakeholder_request": "x"}, "cmd_run"),
    (A.status_args, {"run_id": "r1"}, "cmd_status"),
    (A.approve_args, {"run_id": "r1", "experiment": 1, "responder": "raghu"}, "cmd_approve"),
    (A.abort_args, {"run_id": "r1", "reason": "done"}, "cmd_abort"),
    (A.pause_args, {"run_id": "r1"}, "cmd_pause"),
    (A.resume_args, {"run_id": "r1"}, "cmd_resume"),
    (A.ceiling_args, {"max_users": 300, "step": 50}, "cmd_ceiling"),
    (A.score_args, {"journal": "results"}, "cmd_score"),
    (A.report_args, {"run": "r1"}, "cmd_report"),
    (A.diff_args, {"run_a": "r1", "run_b": "r2"}, "cmd_diff"),
    (A.capture_args, {"show_plan": True}, "cmd_capture"),
    (A.export_args, {}, "cmd_export"),
    (A.bench_args, {"tasks": "config/tasks", "fixtures": "results"}, "cmd_bench"),
]


class TestEveryCommandIsReachable:
    def test_every_cli_command_has_a_builder(self):
        """A command added to the CLI without a UI route fails here, rather than
        quietly becoming CLI-only again."""
        assert set(A.ACTIONS) == _subcommands() - {"serve", "serve-ui"}

    def test_the_samples_cover_every_builder(self):
        assert {b.__name__ for b, _, _ in SAMPLES} == {b.__name__ for b in A.ACTIONS.values()}

    @pytest.mark.parametrize("builder,kwargs,function", SAMPLES, ids=[s[2] for s in SAMPLES])
    def test_the_argv_reaches_the_same_function_as_typing_it(self, builder, kwargs, function, monkeypatch):
        reached = {}
        monkeypatch.setattr(commands, function, lambda *a, **k: reached.setdefault("hit", True) and commands.OK)
        monkeypatch.setattr(sys, "argv", ["crucible", *builder(**kwargs)])
        assert main() == commands.OK
        assert reached.get("hit"), f"{builder.__name__} built an argv that never reached {function}"


class TestNoEasierPath:
    def test_approval_has_no_parameter_for_a_value(self):
        """Assertion 13.2 carried to the UI: the operator approves what was parked.
        A value field would turn a bound approval into a blank cheque."""
        offered = set(inspect.signature(A.approve_args).parameters)
        assert offered == {"run_id", "experiment", "responder", "reject", "manual_step_done",
                           "reason", "state_dir"}

    def test_approval_needs_a_named_responder(self):
        with pytest.raises(A.ActionError, match="responder"):
            A.approve_args(run_id="r1", experiment=1, responder="  ")

    def test_a_manual_step_cannot_be_a_rejection(self):
        with pytest.raises(A.ActionError):
            A.approve_args(run_id="r1", experiment=1, responder="r", reject=True, manual_step_done=True)

    def test_a_value_that_looks_like_a_flag_stays_a_value(self):
        """Every free-text option is one --name=value token, so text typed into a
        form cannot switch the approval gate off."""
        parsed = _parse(A.run_args(stakeholder_request="--approve=preapproved", model="--approve"))
        assert parsed.approve == "file"
        assert parsed.stakeholder_request == "--approve=preapproved"
        assert parsed.model == "--approve"

    @pytest.mark.parametrize("bad", ["-r1", "--approve", "a/b", "../x", "", "a b"])
    def test_a_run_id_that_could_be_read_as_a_flag_or_path_is_refused(self, bad):
        with pytest.raises(A.ActionError):
            A.abort_args(run_id=bad)

    def test_the_approval_gate_defaults_to_asking(self):
        assert _parse(A.run_args()).approve == "file"

    def test_an_unknown_approval_mode_is_refused(self):
        with pytest.raises(A.ActionError):
            A.run_args(approve="auto")

    def test_capture_has_no_field_for_datadog_keys(self):
        """Keys reach the CLI from the environment, so they never appear in argv,
        a job record or a process listing (DESIGN.md 19.8)."""
        assert not {p for p in inspect.signature(A.capture_args).parameters if "key" in p}

    def test_a_ceiling_probe_needs_an_operator_set_maximum(self):
        """DESIGN.md 20.2: the operator sets the bounds, not just the intent."""
        with pytest.raises(A.ActionError, match="maximum"):
            A.ceiling_args(max_users=None)


class TestTouchingTheTargetIsMarked:
    @pytest.mark.parametrize(
        "args,expected",
        [
            (A.run_args(), True),
            (A.resume_args(run_id="r1"), True),
            (A.ceiling_args(max_users=100), True),
            (A.capture_args(fixture_id="perflab_pool_starved"), True),
            (A.capture_args(show_plan=True), False),
            (A.preflight_args(apply_probe=True), True),
            (A.preflight_args(), False),
            (A.plan_args(), False),
            (A.approve_args(run_id="r1", experiment=1, responder="r"), False),
            (A.report_args(), False),
        ],
    )
    def test_commands_that_load_change_or_restart_the_target_need_confirming(self, args, expected):
        assert A.touches_target(args) is expected
