"""Applicator assertions — new group, week 2. DESIGN.md §4.4, §5, §7, §11.

REVIEWED AND APPROVED by the operator, 20 September 2026.

This is the only module that changes the target's behaviour, so nearly every
test below asserts a refusal or an undo rather than a success. Three themes:

1. **The profile is the authority, not the value.** An unlisted property is
   refused even when the number is sensible, because the alternative is an
   allowlist that only works while the model is behaving.
2. **The SLA and the load profile cannot be written.** Tested by path as well as
   by property name, because §4.4 enforces this twice on purpose and a test that
   only checked one half would let the other rot.
3. **A change that does not come back healthy is undone.** Including the case
   where the undo also fails, which must stop the campaign rather than try a
   third thing.

The `previous`-value tests are the subtle ones. The revert restores what the
applicator READ FROM DISK, never what the proposal claimed was there. A model
that misremembers the old value would otherwise make the revert write a value
that was never in force, and the manifest would then describe an experiment
that did not happen.
"""

import pytest

from crucible.perf.applicator import (
    Applicator,
    ApplyError,
    Change,
    CommandRestarter,
    ManualRestarter,
    Proposal,
    RestartBlocked,
    guard_proposal,
    path_is_protected,
    poll_health,
    read_property,
    set_property,
)
from crucible.perf.profile import RestartContract, TargetProfile

PROPERTIES = """\
# PerfLab baseline
spring.application.name=perf-lab
server.port=8080

# --- HikariCP ---
spring.datasource.hikari.maximum-pool-size=10
spring.datasource.hikari.minimum-idle=2
perflab.cache.enabled=true
"""


def a_profile(**over) -> TargetProfile:
    base = {
        "name": "spring-boot",
        "runtime": "jvm",
        "cause_families": ["connection_pool_exhaustion", "cache_miss"],
        "config_file": "application.properties",
        "allowed_properties": {
            "spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100},
            "spring.datasource.hikari.minimum-idle": {"type": "int", "min": 0, "max": 100},
            "perflab.cache.enabled": {"type": "bool"},
        },
        "protected_paths": ["config/slo.yaml", "config/profiles/**", "locust/**", "tests/**"],
    }
    return TargetProfile.from_mapping(base | over)


def a_proposal(prop="spring.datasource.hikari.maximum-pool-size", value=20, **over) -> Proposal:
    base = {
        "cause_family": "connection_pool_exhaustion",
        "changes": (Change(prop=prop, value=value),),
        "reasoning": "pending peaked at 43 against a pool of 10",
    }
    return Proposal(**(base | over))


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    return tmp_path


class TestTheProfileIsTheAuthority:
    """§5. The agent argues for a value; the profile decides whether it is sane."""

    def test_an_in_bounds_change_to_a_listed_property_is_permitted(self):
        assert guard_proposal(a_profile(), a_proposal(value=20)) is None

    def test_an_unlisted_property_is_refused_even_when_the_value_is_harmless(self):
        """The allowlist is the authority, not the plausibility of the number.

        `server.port=8081` is a perfectly reasonable value. It is refused because
        the property is not on the list at all -- if refusal depended on the value
        looking wrong, the guard would only stop a model that was being obviously
        stupid, which is not the threat.
        """
        refusal = guard_proposal(a_profile(), a_proposal(prop="server.port", value=8081))

        assert refusal is not None
        assert "server.port" in refusal
        assert "not an allowed property" in refusal

    def test_a_value_outside_bounds_is_refused(self):
        refusal = guard_proposal(a_profile(), a_proposal(value=5000))

        assert refusal is not None
        assert "above the maximum 100" in refusal

    def test_an_undeclared_cause_family_is_refused(self):
        """Naming a family the runtime does not have means the diagnosis is not
        about this runtime. `gc` is a JVM concept; a profile that does not declare
        it must not accept a proposal claiming it."""
        refusal = guard_proposal(a_profile(), a_proposal(cause_family="gil_contention"))

        assert refusal is not None
        assert "gil_contention" in refusal
        assert "not a cause family" in refusal

    def test_the_first_refusal_reason_is_the_one_reported(self):
        """'You may not touch that property at all' and 'that value is out of
        range' are different refusals, and an operator reading the journal should
        see the categorical one. This asserts the ORDER of the checks, which is
        the thing that would silently regress under a refactor."""
        refusal = guard_proposal(
            a_profile(), a_proposal(prop="server.port", value=999999)
        )

        assert "not an allowed property" in refusal
        assert "maximum" not in refusal

    def test_the_same_property_twice_in_one_proposal_is_refused(self):
        """Two values for one property is not a change, it is an ambiguity: which
        one ends up in force depends on iteration order, and the manifest would
        record both."""
        proposal = Proposal(
            cause_family="connection_pool_exhaustion",
            changes=(
                Change(prop="spring.datasource.hikari.maximum-pool-size", value=20),
                Change(prop="spring.datasource.hikari.maximum-pool-size", value=30),
            ),
        )

        assert "appears twice" in guard_proposal(a_profile(), proposal)

    def test_an_abstention_has_nothing_to_apply(self):
        proposal = Proposal(cause_family="", changes=(), abstained=True, abstain_reason="no gauges")

        assert "abstained" in guard_proposal(a_profile(), proposal)


class TestTheSlaAndLoadProfileCannotBeWritten:
    """§4.4. The most important boundary in the product, enforced twice."""

    @pytest.mark.parametrize(
        "candidate",
        [
            "config/slo.yaml",
            "config/profiles/spring-boot.yaml",
            "config/profiles/fastapi.yaml",
            "locust/locustfile.py",
            "locust/nested/deeper.py",
            "tests/test_perf_campaign.py",
        ],
    )
    def test_the_protected_paths_actually_match(self, candidate):
        """The guard is worth exactly as much as its glob matching.

        `fnmatch` is used rather than PurePath.match precisely so `locust/**`
        crosses a separator and catches `locust/nested/deeper.py`. If that ever
        changed, the guard would still LOOK correct while quietly permitting
        every nested path.
        """
        assert path_is_protected(candidate, a_profile().protected_paths) is not None

    def test_a_windows_separator_is_still_matched(self):
        """The repo is developed on Windows and runs on Linux. A guard that
        stopped matching when the separator changed would be the worst possible
        bug in this file, and it would only show up on one platform."""
        assert path_is_protected(r"locust\locustfile.py", a_profile().protected_paths) is not None

    def test_the_protected_directory_itself_is_covered_not_just_its_contents(self):
        assert path_is_protected("config/profiles", a_profile().protected_paths) is not None

    def test_an_unrelated_path_is_not_protected(self):
        """The negative case matters too: over-matching would make the applicator
        unable to write the config file it is supposed to write."""
        assert path_is_protected("src/main/resources/application.properties",
                                 a_profile().protected_paths) is None

    def test_a_profile_whose_own_config_file_is_protected_is_refused(self):
        """An internally inconsistent profile must stop the applicator rather
        than have it pick one rule over the other."""
        profile = a_profile(config_file="config/slo.yaml")

        refusal = guard_proposal(profile, a_proposal())

        assert "inconsistent" in refusal


class TestEditingThePropertiesFile:
    """The write is line-oriented so a human can read the diff."""

    def test_the_value_changes_and_nothing_else_does(self):
        after = set_property(PROPERTIES, "spring.datasource.hikari.maximum-pool-size", 20)

        assert "spring.datasource.hikari.maximum-pool-size=20" in after
        assert "# PerfLab baseline" in after
        assert "spring.datasource.hikari.minimum-idle=2" in after
        assert len(after.splitlines()) == len(PROPERTIES.splitlines())

    def test_a_boolean_is_written_the_way_java_reads_it(self):
        """Python's str(False) is 'False'; Spring wants 'false'. A capital F
        binds as... nothing, and the property silently keeps its default."""
        after = set_property(PROPERTIES, "perflab.cache.enabled", False)

        assert "perflab.cache.enabled=false" in after
        assert "False" not in after

    def test_reading_back_the_last_assignment_wins(self):
        """Spring resolves the last assignment when a key appears twice. Reading
        the first would make the revert restore a value the app was never
        running."""
        text = PROPERTIES + "spring.datasource.hikari.maximum-pool-size=99\n"

        assert read_property(text, "spring.datasource.hikari.maximum-pool-size") == "99"

    def test_a_commented_out_property_is_not_read_as_set(self):
        text = "# spring.datasource.hikari.maximum-pool-size=2\n"

        assert read_property(text, "spring.datasource.hikari.maximum-pool-size") is None

    def test_a_property_the_file_does_not_set_is_appended_and_marked(self):
        after = set_property(PROPERTIES, "server.tomcat.threads.max", 400)

        assert "# added by crucible" in after
        assert after.rstrip().endswith("server.tomcat.threads.max=400")


class TestPreviousValuesComeFromDiskNotFromTheProposal:
    """The revert has to restore what was actually in force."""

    def test_the_applicator_overwrites_whatever_previous_the_proposal_claimed(self, workspace):
        """A model that reported the old pool size from memory would make the
        revert write a value that was never in force. The applicator therefore
        ignores `previous` on the way in and fills it from the file it read."""
        lying = Proposal(
            cause_family="connection_pool_exhaustion",
            changes=(
                Change(prop="spring.datasource.hikari.maximum-pool-size", value=20, previous=999),
            ),
        )

        result = Applicator(profile=a_profile(), workspace=workspace).apply(lying)

        assert result.changes[0].previous == "10"
        assert result.changes[0].value == 20

    def test_a_refused_proposal_writes_nothing(self, workspace):
        before = (workspace / "application.properties").read_text(encoding="utf-8")

        result = Applicator(profile=a_profile(), workspace=workspace).apply(
            a_proposal(prop="server.port", value=9999)
        )

        assert result.refused and not result.applied
        assert (workspace / "application.properties").read_text(encoding="utf-8") == before


class TestAutoRevert:
    """§7 and §11. A change that does not come back healthy is undone."""

    def test_a_healthy_restart_keeps_the_change(self, workspace):
        restarter = _FakeRestarter(outcomes=[(True, "healthy after 3.0s")])

        result = Applicator(
            profile=a_profile(), workspace=workspace, restarter=restarter
        ).apply(a_proposal(value=20))

        assert result.applied and result.healthy and not result.reverted
        assert "maximum-pool-size=20" in (workspace / "application.properties").read_text(encoding="utf-8")

    def test_an_unhealthy_restart_reverts_the_file_and_restarts_again(self, workspace):
        """The revert is not advisory. Leaving the change in place would let the
        NEXT experiment measure a service that never started, and attribute the
        result to whatever it tried next."""
        restarter = _FakeRestarter(outcomes=[(False, "never became healthy"), (True, "healthy")])

        result = Applicator(
            profile=a_profile(), workspace=workspace, restarter=restarter
        ).apply(a_proposal(value=20))

        assert result.reverted and result.revert_healthy is True
        assert not result.needs_human
        assert "maximum-pool-size=10" in (workspace / "application.properties").read_text(encoding="utf-8")
        assert restarter.calls == 2

    def test_a_failed_revert_stops_and_asks_for_a_human(self, workspace):
        """Two failed restarts is no longer a configuration problem. A third
        automatic action against a target nobody understands the state of would
        be guessing, and the campaign has to stop instead."""
        restarter = _FakeRestarter(outcomes=[(False, "did not start"), (False, "still down")])

        result = Applicator(
            profile=a_profile(), workspace=workspace, restarter=restarter
        ).apply(a_proposal(value=20))

        assert result.reverted and result.revert_healthy is False
        assert result.needs_human
        assert "no manifest describes" in result.reason

    def test_the_file_is_restored_byte_for_byte_on_revert(self, workspace):
        """Not 'the property is back to 10' -- the whole file. A revert that
        rewrote comments or reordered lines would make every later diff unreadable
        and hide what an experiment actually changed."""
        before = (workspace / "application.properties").read_text(encoding="utf-8")
        restarter = _FakeRestarter(outcomes=[(False, "down"), (True, "healthy")])

        Applicator(profile=a_profile(), workspace=workspace, restarter=restarter).apply(
            a_proposal(value=20)
        )

        assert (workspace / "application.properties").read_text(encoding="utf-8") == before

    def test_no_restarter_means_no_health_is_claimed(self, workspace):
        """On the cloud setup the restart happens on the target box, not here.
        The result must not report a health it never observed."""
        result = Applicator(profile=a_profile(), workspace=workspace).apply(a_proposal(value=20))

        assert result.applied
        assert result.healthy is False and result.restarted is False
        assert "no local restarter" in result.reason


class TestManualRestartBlocksRatherThanFails:
    """§11. The campaign blocks with instructions; it does not fail."""

    def test_a_manual_restarter_raises_restart_blocked(self):
        contract = RestartContract(manual=True, instructions="stop the JVM, then start it")

        with pytest.raises(RestartBlocked, match="stop the JVM"):
            ManualRestarter(contract).restart()

    def test_a_blocked_restart_is_recorded_as_a_manual_step(self, workspace):
        """A run with human intervention is not comparable to a fully autonomous
        one, so the manifest has to be able to tell them apart."""
        result = Applicator(
            profile=a_profile(),
            workspace=workspace,
            restarter=ManualRestarter(RestartContract(manual=True, instructions="do it by hand")),
        ).apply(a_proposal(value=20))

        assert result.applied and result.manual_step
        assert "do it by hand" in result.reason

    def test_manual_true_with_a_command_restarter_still_blocks(self):
        """Automation is declared, never guessed. A contract that says manual but
        happens to carry a command must not have the command run: the operator
        said no automation, and the presence of a command is not consent."""
        contract = RestartContract(manual=True, command=["./mvnw", "spring-boot:run"])

        with pytest.raises(RestartBlocked):
            CommandRestarter(contract).restart()

    def test_manual_false_with_no_command_is_an_error_not_a_silent_pass(self):
        """There is nothing to run and nothing to tell an operator. Passing
        silently would report a restart that never happened."""
        with pytest.raises(ApplyError, match="names no restart command"):
            CommandRestarter(RestartContract(manual=False)).restart()


class TestHealthPolling:
    """Wall clock is charged here, never to the measured window."""

    def test_it_returns_as_soon_as_the_target_is_healthy(self):
        clock = _Clock()
        answers = iter([False, False, True])

        healthy, waited = poll_health(
            "http://target/health", timeout_s=60,
            now=clock.now, sleep=clock.sleep, probe=lambda _: next(answers),
        )

        assert healthy and waited == pytest.approx(4.0)

    def test_it_gives_up_at_the_deadline_rather_than_polling_forever(self):
        clock = _Clock()

        healthy, waited = poll_health(
            "http://target/health", timeout_s=5,
            now=clock.now, sleep=clock.sleep, probe=lambda _: False,
        )

        assert not healthy and waited >= 5

    def test_no_health_url_is_treated_as_healthy_without_waiting(self):
        """A profile that declares no health endpoint cannot be polled. Blocking
        forever would be worse than proceeding, and this is visible in the profile
        rather than hidden here."""
        assert poll_health("", timeout_s=60) == (True, 0.0)


class TestTheCommit:
    """§19.7. The deployed sha is a recorded fact, not something inferred later."""

    def test_the_commit_sha_is_returned_and_recorded(self, workspace):
        git = _FakeGit(head="abc123def456")

        result = Applicator(
            profile=a_profile(), workspace=workspace, _git=git
        ).apply(a_proposal(value=20), commit_message="experiment 1")

        assert result.commit == "abc123def456"
        assert ["git", "add", "--", "application.properties"] in git.calls

    def test_only_the_config_file_is_staged(self, workspace):
        """A `git add -A` would sweep in whatever else was in the workspace, and
        the experiment's commit would then contain changes nobody proposed."""
        git = _FakeGit(head="abc123def456")

        Applicator(profile=a_profile(), workspace=workspace, _git=git).apply(
            a_proposal(value=20), commit_message="experiment 1"
        )

        for argv in git.calls:
            assert "-A" not in argv and "--all" not in argv


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRestarter:
    name = "fake"

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def restart(self):
        self.calls += 1
        return self._outcomes.pop(0) if self._outcomes else (True, "healthy")


class _FakeGit:
    def __init__(self, head):
        self.head = head
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[1] == "rev-parse":
            return 0, self.head
        return 0, ""


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
