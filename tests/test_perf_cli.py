"""CLI assertions — new group, week 2. DESIGN.md §9, §15, §19.4.

REVIEWED 20 September 2026; the operator raised a finding here and the fix
below is AWAITING RE-CHECK, not yet approved.

Screen 19 is the surface many engineers will only ever use, so the CLI is not a
convenience wrapper — for most of the nineteen screens it IS the product. These
tests assert the two properties that make it trustworthy:

**`plan` changes nothing.** Asserted by checksumming the workspace either side of
the call, not by reading the code. "Show me first" is worthless if it turns out
to touch something.

**`approve` cannot widen what it approves.** There is no `--value` flag, and
`write_decision` copies the parameters out of the parked request. An operator
approves what they were shown; giving them a way to type different numbers would
turn a bound approval into a blank cheque (§19.4).

`plan`'s output is asserted for content rather than formatting. What matters is
that an operator reading it can see the authority boundary — which properties may
move, which paths never can, and where a deploy would land — before agreeing to
let something edit a running service.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from crucible.cli import build_parser, main
from crucible.perf import commands


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def _subcommands(parser) -> dict:
    """Every registered subcommand, by name.

    argparse exposes no public API for this, so it reaches into
    `_subparsers._group_actions`. That is fragile, which is exactly why it lives
    in ONE helper with this note rather than being repeated at each call site --
    when a Python release moves it, one function breaks instead of several.
    """
    return dict(parser._subparsers._group_actions[0].choices)


def _flags_of(parser, command: str) -> set:
    """Every option string the named subcommand accepts."""
    sub = _subcommands(parser)[command]
    return {opt for action in sub._actions for opt in action.option_strings}


def _tree_digest(root: Path) -> str:
    """A checksum over every file under ``root``, contents included."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class TestPlanTouchesNothing:
    """§9. Terraform's plan/apply pattern, no Terraform involved."""

    def test_the_repository_is_byte_identical_after_a_plan(self, capsys):
        """Asserted on the filesystem, not on a reading of the code. A `plan`
        that turned out to write something would undermine the one guarantee the
        command exists to make."""
        watched = Path("config")
        before = _tree_digest(watched)

        assert commands.cmd_plan() == commands.OK

        assert _tree_digest(watched) == before

    def test_it_shows_which_properties_may_change_and_their_bounds(self, capsys):
        commands.cmd_plan()
        out = capsys.readouterr().out

        assert "spring.datasource.hikari.maximum-pool-size" in out
        assert "max 100" in out

    def test_it_shows_which_paths_can_never_be_written(self, capsys):
        """The authority boundary, in front of the operator before they agree to
        anything. Burying it behind a run is how you get an approval nobody
        understood."""
        commands.cmd_plan()
        out = capsys.readouterr().out

        assert "config/slo.yaml" in out
        assert "locust/**" in out

    def test_it_states_the_noise_floor_a_verdict_will_be_judged_against(self, capsys):
        commands.cmd_plan()
        out = capsys.readouterr().out

        assert "2.08%" in out
        assert "INCONCLUSIVE" in out

    def test_it_reports_the_deploy_mode_it_actually_found(self, capsys):
        """The shipped profile has declared `pipeline` since 21 September 2026,
        when the post-receive hook on Box A was installed and verified."""
        commands.cmd_plan()
        out = capsys.readouterr().out

        assert "mode        : pipeline" in out

    def test_a_manual_deploy_is_announced_as_one_that_will_block(
        self, tmp_path, monkeypatch, capsys
    ):
        """The half that matters for planning. An operator setting up an
        overnight run needs to know it will stop and wait for them -- discovering
        that at 3am is the whole reason `plan` exists.

        Asserted against a manual profile rather than the shipped one, so this
        keeps testing the behaviour after the shipped profile moved to pipeline.
        """
        source = yaml.safe_load(Path("config/profiles/spring-boot.yaml").read_text(encoding="utf-8"))
        source["deploy"]["mode"] = "manual"
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir()
        (profile_dir / "spring-boot.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
        for skill in Path("config/profiles").glob("*.SKILL.md"):
            (profile_dir / skill.name).write_text(skill.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("CRUCIBLE_PROFILE_DIR", str(profile_dir))

        commands.cmd_plan()
        out = capsys.readouterr().out

        assert "mode        : manual" in out
        assert "block with" in out

    def test_a_production_environment_is_refused_at_plan_time(self, tmp_path, capsys):
        """Before a campaign, not during one. Finding out at 3am that the target
        was labelled production would be a poor time."""
        sla = tmp_path / "prod.yaml"
        sla.write_text(
            Path("config/slo.yaml").read_text(encoding="utf-8").replace(
                "kind: pre-prod", "kind: production"
            ),
            encoding="utf-8",
        )

        assert commands.cmd_plan(sla_path=str(sla)) == commands.REFUSED
        assert "REFUSED" in capsys.readouterr().out


class TestInitDoesNotInventAnSla:
    """A generated threshold is a goalpost that arrived by accident."""

    def test_it_creates_the_state_directories(self, state):
        commands.cmd_init(state_dir=str(state))

        assert (state / "approvals").is_dir()
        assert (state / "locks").is_dir()
        assert (state / "aborts").is_dir()

    def test_a_missing_sla_is_reported_not_written(self, state, tmp_path, capsys):
        """The same rule as §4.4 reached by a friendlier route: an SLA nobody
        chose is as bad as one the agent chose."""
        missing = tmp_path / "nothing.yaml"

        commands.cmd_init(state_dir=str(state), sla_path=str(missing))

        assert not missing.exists()
        out = capsys.readouterr().out
        assert "MISSING" in out and "will not generate one" in out

    def test_an_existing_sla_is_summarised(self, state, capsys):
        commands.cmd_init(state_dir=str(state))
        out = capsys.readouterr().out

        assert "perflab-db-latency" in out
        assert "2.08%" in out


def _closed_port() -> int:
    """An ephemeral port that was just bound and released.

    Not a well-known dead port: on Windows the firewall DROPS traffic to some of
    those rather than refusing it, so a connection hangs for the full timeout and
    these tests took 4-5 seconds each. A port the OS just handed back is refused
    with an immediate RST on loopback.
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def offline_profile(tmp_path, monkeypatch):
    """The shipped profile with its URLs pointed at a closed local port.

    Preflight reaches out over the network. Left on Box A's private address these
    tests would spend the full connect timeout per check on any machine without a
    route -- and would pass or fail depending on whose laptop ran them.
    """
    port = _closed_port()
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    source = yaml.safe_load(Path("config/profiles/spring-boot.yaml").read_text(encoding="utf-8"))
    source["deploy"]["version_url"] = f"http://127.0.0.1:{port}/api/version"
    source["restart"]["health_url"] = f"http://127.0.0.1:{port}/actuator/health"
    (profile_dir / "spring-boot.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
    monkeypatch.setenv("CRUCIBLE_PROFILE_DIR", str(profile_dir))
    return profile_dir, source


@pytest.fixture
def offline_config(tmp_path, monkeypatch, offline_profile):
    """An offline profile plus an SLA it protects, at a temp path."""
    profile_dir, source = offline_profile

    sla = yaml.safe_load(Path("config/slo.yaml").read_text(encoding="utf-8"))
    sla["environment"]["target_base_url"] = source["deploy"]["version_url"].rsplit("/api", 1)[0]
    sla_path = tmp_path / "slo.yaml"
    sla_path.write_text(yaml.safe_dump(sla), encoding="utf-8")

    # The profile must protect the SLA that is actually in use, not the one at
    # the default path. Preflight checks the file it was pointed at, and an
    # operator who moves their SLA outside protected_paths has genuinely opened
    # the goalpost to the agent -- so the fixture adds it rather than the test
    # working around a check that is doing its job.
    source["protected_paths"].append(sla_path.as_posix())
    (profile_dir / "spring-boot.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")

    monkeypatch.setenv("CRUCIBLE_PROFILE_DIR", str(profile_dir))
    return str(sla_path)


class TestPreflightReportsRatherThanGuesses:
    """§9. It exercises the target; where it cannot reach, it says so."""

    def test_the_protected_paths_are_actually_verified(self, offline_config, capsys):
        """Not assumed from the profile being present. This is the boundary that
        matters most, and a profile that had drifted would otherwise only be
        discovered by an agent exploiting it."""
        commands.cmd_preflight(sla_path=offline_config, probe_timeout_s=0.25)
        out = capsys.readouterr().out

        assert "is protected: matches" in out
        assert "locust/locustfile.py is protected" in out

    def test_an_unreachable_target_warns_rather_than_failing(self, offline_config, capsys):
        """An operator may be running preflight from a machine with no route to
        the target. That is worth reporting, not worth treating as a broken
        configuration."""
        code = commands.cmd_preflight(sla_path=offline_config, probe_timeout_s=0.25)
        out = capsys.readouterr().out

        assert code == commands.OK
        assert "[warn]" in out

    def test_an_unreachable_target_is_never_reported_as_verified(self, offline_config, capsys):
        """The failure mode this whole check exists to stop: a target that cannot
        prove its commit must never read as one that did (§19.6)."""
        commands.cmd_preflight(sla_path=offline_config, probe_timeout_s=0.25)
        out = capsys.readouterr().out

        assert "[warn] target reports its commit" in out

    def test_an_sla_outside_protected_paths_fails_preflight(self, tmp_path, offline_profile, capsys):
        """Found by writing these tests: preflight checks the SLA it was POINTED
        AT, not the default one. An operator who moves their SLA somewhere the
        profile does not protect has handed the agent its own goalpost, and this
        is the check that catches it before a campaign starts."""
        _, source = offline_profile
        stray = tmp_path / "elsewhere" / "slo.yaml"
        stray.parent.mkdir()
        sla = yaml.safe_load(Path("config/slo.yaml").read_text(encoding="utf-8"))
        sla["environment"]["target_base_url"] = source["deploy"]["version_url"].rsplit("/api", 1)[0]
        stray.write_text(yaml.safe_dump(sla), encoding="utf-8")

        code = commands.cmd_preflight(sla_path=str(stray), probe_timeout_s=0.25)
        out = capsys.readouterr().out

        assert code == commands.FAILED
        assert "NOT protected by this profile" in out

    def test_the_apply_probe_is_off_by_default(self, offline_config, capsys):
        """It genuinely restarts the service. Opt-in rather than something an
        operator triggers by typing the obvious command."""
        commands.cmd_preflight(sla_path=offline_config, probe_timeout_s=0.25)
        out = capsys.readouterr().out

        assert "skipped (pass --apply-probe" in out


class TestApproveCannotWidenWhatItApproves:
    """§19.4 and the S12 binding invariant."""

    def test_there_is_no_way_to_supply_a_different_value(self):
        """Asserted on the parser, because this is a hole that would be added by
        someone being helpful. Changing the value means a new proposal, which the
        operator then sees."""
        flags = _flags_of(build_parser(), "approve")

        assert "--value" not in flags
        assert "--params" not in flags
        assert "--as" in flags

    def test_approving_copies_the_parked_parameters(self, state, capsys):
        _park(state, "run-1", 1, {"spring.datasource.hikari.maximum-pool-size": 20})

        assert commands.cmd_approve(
            "run-1", 1, responder="operator", state_dir=str(state)
        ) == commands.OK

        written = json.loads(
            (state / "approvals" / "run-1" / "001.decision.json").read_text(encoding="utf-8")
        )
        assert written["params"] == {"spring.datasource.hikari.maximum-pool-size": 20}
        assert written["responder"] == "operator"

    def test_the_responder_is_required_so_the_manifest_can_name_them(self):
        """§11. A run with human intervention is not comparable to an autonomous
        one, and 'a human approved it' is worth little without who."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["approve", "run-1", "--experiment", "1"])

    def test_rejecting_is_recorded_as_a_rejection(self, state):
        _park(state, "run-1", 1, {"p": 1})

        commands.cmd_approve(
            "run-1", 1, responder="operator", reject=True,
            reason="want the trace first", state_dir=str(state),
        )

        written = json.loads(
            (state / "approvals" / "run-1" / "001.decision.json").read_text(encoding="utf-8")
        )
        assert written["action"] == "reject"

    def test_approving_something_that_was_never_parked_fails(self, state, capsys):
        assert commands.cmd_approve(
            "run-1", 9, responder="operator", state_dir=str(state)
        ) == commands.FAILED


class TestStatusAndAbort:
    def test_status_lists_a_pending_approval_with_the_command_to_answer_it(self, state, capsys):
        _park(state, "run-1", 1, {"spring.datasource.hikari.maximum-pool-size": 20})

        commands.cmd_status(state_dir=str(state))
        out = capsys.readouterr().out

        assert "run-1 experiment 1" in out
        assert "crucible approve run-1 --experiment 1" in out

    def test_status_shows_a_held_lock_and_who_holds_it(self, state, capsys):
        from crucible.perf.campaign import BranchLock

        BranchLock(state_dir=state, branch="perftest_sandbox", run_id="run-7").acquire()

        commands.cmd_status(state_dir=str(state))

        assert "run-7" in capsys.readouterr().out

    def test_abort_explains_what_it_does_and_does_not_undo(self, state, capsys):
        """The difference is easy to get wrong and expensive to get wrong. An
        operator hitting abort must know the box gets rolled back too."""
        commands.cmd_abort("run-1", reason="unrelated outage", state_dir=str(state))
        out = capsys.readouterr().out

        assert "in-flight experiment will be discarded" in out
        assert "already verified are kept" in out
        assert "rolled back" in out

    def test_the_abort_marker_is_written_where_a_running_campaign_will_see_it(self, state):
        commands.cmd_abort("run-1", state_dir=str(state))

        assert (state / "aborts" / "run-1.abort").exists()

    def test_an_abort_can_be_cleared_so_the_run_can_start_again(self, state):
        commands.cmd_abort("run-1", state_dir=str(state))
        commands.cmd_clear_abort("run-1", state_dir=str(state))

        assert not (state / "aborts" / "run-1.abort").exists()


#: Every command, a minimal invocation of it, and the function it must reach.
#: Kept as data so adding a verb without wiring it makes this table fail rather
#: than quietly passing.
COMMAND_WIRING = [
    ("init", ["init"], "cmd_init"),
    ("plan", ["plan"], "cmd_plan"),
    ("preflight", ["preflight"], "cmd_preflight"),
    ("run", ["run"], "cmd_run"),
    ("status", ["status"], "cmd_status"),
    ("approve", ["approve", "r1", "--experiment", "1", "--as", "operator"], "cmd_approve"),
    ("abort", ["abort", "r1"], "cmd_abort"),
    # Week 3. `score` reads manifests and calls no model; `bench` replays a task
    # set against captured snapshots and needs no live target.
    ("score", ["score"], "cmd_score"),
    ("bench", ["bench", "--tasks", "t.yaml", "--fixtures", "f"], "cmd_bench"),
    # Week 3, late. Both read manifests off disk and call no model (DESIGN.md
    # 4.6). `diff` is the only verb that exits non-zero on a SUCCESSFUL run: an
    # unsafe comparison is a refusal, not a failure, and a script comparing two
    # campaigns should be able to notice that without parsing prose.
    ("report", ["report"], "cmd_report"),
    ("diff", ["diff", "--a", "r1", "--b", "r2"], "cmd_diff"),
    # Week 3, the capture half. `--plan` touches nothing, which is what makes it
    # safe to exercise here.
    ("capture", ["capture", "--plan"], "cmd_capture"),
]


class TestEveryCommandIsWiredToItsImplementation:
    """Not that argparse knows the name -- that typing it reaches the code.

    An earlier version of this asserted `command in parser.choices`, which is
    only that a subparser was registered. It would have passed for a verb that
    parsed fine and then fell through `main()` to "unhandled command", because
    registration and dispatch are two different things and only one of them is
    what an operator experiences.
    """

    @pytest.mark.parametrize("command,argv,function", COMMAND_WIRING)
    def test_typing_the_command_reaches_its_function(
        self, command, argv, function, monkeypatch
    ):
        called = {}

        def spy(*args, **kwargs):
            called["hit"] = True
            return commands.OK

        monkeypatch.setattr(commands, function, spy)
        monkeypatch.setattr(sys, "argv", ["crucible", *argv])

        assert main() == commands.OK
        assert called.get("hit"), f"{command!r} parsed but never reached {function}"

    def test_the_wiring_table_covers_every_command_the_parser_offers(self):
        """The table and the parser must not drift. A verb added to one and not
        the other is exactly the gap the test above exists to close, so the
        omission itself has to fail rather than shrink the coverage silently."""
        offered = set(_subcommands(build_parser()))
        covered = {name for name, _, _ in COMMAND_WIRING}

        # `serve` predates week 2 and starts a server rather than calling into
        # crucible.perf.commands, so it is exercised separately below.
        assert offered - covered == {"serve"}

    def test_manual_step_done_routes_away_from_the_approval_path(self, monkeypatch):
        """Same verb, different meaning, and it must not reach `cmd_approve`.

        W2-Q8 reuses the approvals directory deliberately, but a manual-step
        confirmation authorises nothing. If it landed in the approval path it
        would be recorded as an approval, and an operator reporting a restart
        would silently have consented to the change itself.
        """
        reached = []
        monkeypatch.setattr(commands, "cmd_confirm_manual_step",
                            lambda *a, **k: reached.append("manual") or commands.OK)
        monkeypatch.setattr(commands, "cmd_approve",
                            lambda *a, **k: reached.append("approve") or commands.OK)
        monkeypatch.setattr(sys, "argv", [
            "crucible", "approve", "r1", "--experiment", "1",
            "--as", "operator", "--manual-step-done",
        ])

        assert main() == commands.OK
        assert reached == ["manual"]

    def test_without_the_flag_the_same_verb_is_still_an_approval(self, monkeypatch):
        """The negative half. A flag that changed behaviour in only one direction
        would be worse than no flag."""
        reached = []
        monkeypatch.setattr(commands, "cmd_confirm_manual_step",
                            lambda *a, **k: reached.append("manual") or commands.OK)
        monkeypatch.setattr(commands, "cmd_approve",
                            lambda *a, **k: reached.append("approve") or commands.OK)
        monkeypatch.setattr(sys, "argv", [
            "crucible", "approve", "r1", "--experiment", "1", "--as", "operator",
        ])

        assert main() == commands.OK
        assert reached == ["approve"]

    def test_abort_clear_reaches_the_clear_function_not_the_abort_one(self):
        """`--clear` removes a marker; without it, abort writes one. Same verb,
        opposite effects, so the flag has to route somewhere different."""
        args = build_parser().parse_args(["abort", "r1", "--clear"])

        assert args.clear is True

    def test_serve_still_works_so_the_existing_entry_point_is_unbroken(self):
        args = build_parser().parse_args(["serve", "--port", "9999"])

        assert args.command == "serve" and args.port == 9999

    def test_run_defaults_to_the_file_gate_not_to_preapproval(self):
        """Failing closed. A default of `preapproved` would make an unattended
        campaign an unsupervised one, and the flag is easy to not notice."""
        args = build_parser().parse_args(["run"])

        assert args.approve == "file"

    def test_run_discards_a_warmup_by_default(self):
        """A cold JVM measured p99 150 ms where a warm one measured 98 — a ~50%
        gap against a 2.08% noise threshold. A zero default would swamp every
        signal the campaign is looking for."""
        args = build_parser().parse_args(["run"])

        assert args.warmup == 120.0
        assert args.measure == 300.0


def _park(state: Path, run_id: str, experiment: int, params: dict) -> None:
    run_dir = state / "approvals" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{experiment:03d}.request.json").write_text(
        json.dumps({
            "run_id": run_id, "experiment": experiment,
            "summary": "connection_pool_exhaustion: pool 10 -> 20",
            "params": params, "reasoning": "pending peaked at 43",
            "cause_family": "connection_pool_exhaustion",
        }),
        encoding="utf-8",
    )
