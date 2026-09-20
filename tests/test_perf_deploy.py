"""Deploy assertions — new group, 13 September 2026. DESIGN.md §19.

REVIEWED AND APPROVED by the operator (week 1; re-confirmed 20 September 2026).

Deploy is the capability that makes the loop autonomous, and it is also the one
that reaches outside this machine. Almost every test below is about a refusal
rather than a success: what the agent cannot make it do, and what it will not do
on an assumption.

The §19.6 tests are not hypothetical. During the K1 cloud run a complete set of
measurements was taken against a target that had never been restarted — it was
caught only because a pool of 20 cannot cap active connections at 2, and the
numbers were otherwise entirely plausible.
"""

import pytest

from crucible.perf.deploy import (
    FORBIDDEN_PUSH_ARGS,
    BranchPolicyViolation,
    DeployBlocked,
    DeployError,
    DeployLog,
    DeployResult,
    DeployTarget,
    GitPushDeployer,
    ManualDeployer,
    await_commit,
    same_commit,
)
from crucible.perf.profile import TargetProfile

SHA = "a1b2c3d4e5f67890"
OLD = "0000111122223333"


def pipeline_target(**over) -> DeployTarget:
    base = dict(
        remote="perftest", branch="perftest_sandbox", mode="pipeline",
        version_url="http://target/api/version", version_field="commit",
        verify_timeout_s=1.0, poll_interval_s=0.01,
    )
    return DeployTarget(**(base | over))


class TestTheRefspecIsConfigurationNotModelOutput:
    """§19.2. The model decides WHETHER to deploy; the profile decides WHERE."""

    def test_the_command_is_built_from_the_profile(self):
        argv = GitPushDeployer(target=pipeline_target()).push_command(SHA)

        assert argv == ["git", "push", "perftest", f"{SHA}:refs/heads/perftest_sandbox"]
        assert "main" not in " ".join(argv)

    def test_an_unpinned_refspec_is_refused(self):
        """A deploy may ONLY ever reach the configured sandbox branch.

        The refusal is the point: there is no code path that falls back to the
        remote's default, or to any other branch, when the profile is incomplete.
        An incomplete profile stops the deploy rather than choosing a
        destination on the operator's behalf -- choosing one is exactly how a
        push reaches `main`.
        """
        deployer = GitPushDeployer(target=pipeline_target(branch=""))

        with pytest.raises(DeployError, match="must both be set"):
            deployer.push_command(SHA)

    def test_the_destination_is_always_the_configured_branch(self):
        """Whatever the commit, the refspec's right-hand side is the profile's
        branch and nothing else -- fully qualified, so it cannot resolve to a
        remote-side default or to a tag of the same name."""
        for commit in (SHA, OLD, "HEAD", "refs/heads/main"):
            argv = GitPushDeployer(target=pipeline_target()).push_command(commit)
            assert argv[-1].endswith(":refs/heads/perftest_sandbox")
            assert argv[-1].split(":", 1)[1] == "refs/heads/perftest_sandbox"

    def test_the_shipped_profile_does_not_target_a_protected_branch(self):
        deploy = TargetProfile.named("spring-boot").deploy

        assert deploy.branch not in {"main", "master"}
        assert deploy.branch, "a profile with no branch cannot deploy safely"


class TestForcePushIsRefusedAlways:
    """§19.3. Revert means deploying an earlier commit, never rewriting history.

    A force-push would destroy the experiment history the journal and every
    manifest depend on, making prior results unreproducible.
    """

    @pytest.mark.parametrize("flag", ["--force", "-f", "--force-with-lease", "--mirror"])
    def test_history_rewriting_flags_are_refused(self, flag):
        deployer = GitPushDeployer(target=pipeline_target(), git_binary="git")
        # Simulate a profile edited to include the flag.
        deployer.target = pipeline_target(remote=f"perftest {flag}")
        argv = ["git", "push", "perftest", flag, f"{SHA}:refs/heads/x"]

        from crucible.perf.deploy import _check_push_command

        with pytest.raises(DeployError, match="refused"):
            _check_push_command(argv)

    def test_a_forced_refspec_is_refused(self):
        from crucible.perf.deploy import _check_push_command

        with pytest.raises(DeployError, match="forced refspec"):
            _check_push_command(["git", "push", "perftest", f"+{SHA}:refs/heads/x"])

    def test_the_forbidden_list_covers_the_dangerous_flags(self):
        for flag in ("--force", "--mirror", "--delete", "--all"):
            assert flag in FORBIDDEN_PUSH_ARGS


class TestAutomationIsDeclaredNeverGuessed:
    """§19.5. Guessing wrong either stalls a working pipeline or silently skips a
    deploy that never happened — and then measures the old build."""

    def test_a_manual_profile_blocks_rather_than_failing(self):
        target = DeployTarget(mode="manual", instructions="ssh in and redeploy")
        with pytest.raises(DeployBlocked) as excinfo:
            ManualDeployer(target=target).deploy(SHA)

        assert "ssh in and redeploy" in str(excinfo.value)
        assert "paused, not failed" in str(excinfo.value)

    def test_git_push_refuses_when_the_profile_says_manual(self):
        deployer = GitPushDeployer(target=pipeline_target(mode="manual"))

        with pytest.raises(DeployBlocked, match="never guessed"):
            deployer.deploy(SHA)

    def test_the_shipped_profile_declares_pipeline_and_backs_it_up(self):
        """Flipped to `pipeline` on 21 September 2026, after the hook existed.

        The previous version of this test asserted `manual` and said: "if this
        starts failing, check a pipeline really does exist before changing it."
        It does -- `scripts/box_a_post_receive.sh` is installed as the
        post-receive hook on Box A's bare repo, and a push from Box B was
        verified end to end (`docs/DEPLOY_SETUP.md`).

        A `pipeline` declaration is only honest if it is COMPLETE, so this also
        asserts the refspec is fully pinned. Section 19.5's failure mode is an
        agent assuming automation that is not there and skipping a deploy that
        never happened; a half-configured pipeline is the same failure with extra
        steps.
        """
        deploy = TargetProfile.named("spring-boot").deploy

        assert deploy.mode == "pipeline"
        assert deploy.automated
        assert deploy.remote and deploy.branch and deploy.base_branch
        assert deploy.branch != deploy.base_branch
        assert deploy.version_url and deploy.version_field

    def test_the_deploy_hook_the_profile_depends_on_is_in_the_repo(self):
        """`mode: pipeline` is a claim about infrastructure. The script that
        implements it has to ship with the profile that assumes it, or the claim
        is unfalsifiable from inside the repo."""
        from pathlib import Path

        hook = Path("scripts/box_a_post_receive.sh")

        assert hook.exists()
        text = hook.read_text(encoding="utf-8")
        # The tunable must come from the properties file, never the command line:
        # a Spring CLI argument overrides it, and the pool size is the thing
        # under experiment. Found on Box A, 20 September 2026.
        # `maximum-pool-size=` would mean the hook is assigning the tunable on
        # the command line, where a Spring argument OVERRIDES the properties
        # file the agent edits. The hook names the flag in a comment explaining
        # why it must not be set; the `=` is what distinguishes explanation from
        # assignment.
        assert "maximum-pool-size=" not in text


class TestNoMeasurementBeforeTheTargetProvesItsVersion:
    """§19.6. This is the one that already bit us for real."""

    def test_a_target_still_running_the_old_commit_is_not_verified(self):
        calls = {"n": 0}

        def old_version(url, field_name, timeout_s=5.0):
            calls["n"] += 1
            return OLD

        import crucible.perf.deploy as mod

        original, mod.read_running_commit = mod.read_running_commit, old_version
        try:
            verified, waited, observed = await_commit(pipeline_target(), SHA)
        finally:
            mod.read_running_commit = original

        assert verified is False
        assert observed == OLD, "the last observation must survive for a human to read"
        assert calls["n"] > 1, "it should have polled rather than given up at once"

    def test_a_silent_target_is_distinguishable_from_a_stale_one(self):
        """'running the previous commit' and 'will not answer' need different
        responses from a human, so they must not both collapse to False."""
        import crucible.perf.deploy as mod

        original, mod.read_running_commit = mod.read_running_commit, lambda **kw: None
        try:
            verified, _waited, observed = await_commit(pipeline_target(), SHA)
        finally:
            mod.read_running_commit = original

        assert verified is False
        assert observed is None

    def test_a_deploy_that_cannot_be_verified_reports_why(self):
        pushed = []
        deployer = GitPushDeployer(
            target=pipeline_target(),
            _runner=lambda argv: (pushed.append(argv) or (0, "")),
        )
        import crucible.perf.deploy as mod

        original, mod.read_running_commit = mod.read_running_commit, lambda **kw: OLD
        try:
            result = deployer.deploy(SHA)
        finally:
            mod.read_running_commit = original

        assert result.deployed is True, "the push itself succeeded"
        assert result.verified is False, "but the target never confirmed the commit"
        assert "No measurement may begin" in result.reason

    def test_a_profile_with_no_version_endpoint_cannot_verify(self):
        """Silence is not consent: with nowhere to ask, §19.6 is unsatisfiable
        and the run must not proceed as though it had been checked."""
        verified, waited, observed = await_commit(DeployTarget(version_url=""), SHA)

        assert (verified, waited, observed) == (False, 0.0, None)


class TestCommitMatching:
    def test_an_abbreviated_sha_matches_in_either_direction(self):
        assert same_commit("a1b2c3d4e5f6", "a1b2c3d4") is True
        assert same_commit("a1b2c3d4", "a1b2c3d4e5f6") is True

    def test_different_commits_do_not_match(self):
        assert same_commit(SHA, OLD) is False

    def test_a_too_short_sha_requires_an_exact_match(self):
        """Matching loosely on three characters would mean measuring the wrong
        build, which is exactly what this gate exists to prevent."""
        assert same_commit("a1b", "a1b2c3d4e5f6") is False
        assert same_commit("a1b", "a1b") is True


class TestTheDeployLog:
    """§19.7 and §19.9: the last good commit is a recorded fact, because abort
    has to redeploy it rather than infer it."""

    def test_the_last_good_commit_is_the_last_VERIFIED_one(self):
        log = DeployLog()
        log.record(DeployResult(commit="good1", deployed=True, verified=True))
        log.record(DeployResult(commit="bad", deployed=True, verified=False))

        assert log.last_good_commit == "good1", "an unverified deploy is not a good commit"

    def test_a_log_with_no_verified_deploy_has_no_good_commit(self):
        log = DeployLog()
        log.record(DeployResult(commit="bad", deployed=True, verified=False))

        assert log.last_good_commit is None

    def test_a_manual_step_is_recorded_for_the_manifest(self):
        """DESIGN.md §11: a run with human intervention is not comparable to a
        fully autonomous one, so the manifest must be able to tell them apart."""
        log = DeployLog()
        log.record(DeployResult(commit="c1", deployed=True, verified=True, manual=True))

        assert log.had_manual_step is True
        assert log.as_manifest_entries()[0]["manual"] is True


class TestCrucibleWritesToOneBranchAndNoOther:
    """Operator decision, 20 September 2026. Strengthens section 19.2.

    Before this, the branch was pinned by config and nothing refused `main`. The
    agent could not change it -- `profile.yaml` is a protected path -- but a
    human editing that line by hand, or a typo, would have sent every experiment
    to a shared branch. Section 19.8 puts the real control on the remote, and
    that still holds; this catches the mistake before it gets there.

    The check lives in `DeployTarget.__post_init__`, not in `GitPushDeployer`,
    for a specific reason: a guardrail that lives in one adapter is one somebody
    reimplements without it, reading the interface and not the history. Every
    adapter takes a DeployTarget, so every adapter inherits the rule -- including
    the Jenkins one nobody has written yet.
    """

    @pytest.mark.parametrize("branch", ["main", "master", "develop", "trunk"])
    def test_a_shared_branch_is_refused(self, branch):
        # An explicit, different base_branch so this exercises the PATTERN rule
        # rather than the branch-equals-base one. Both refuse `main` under the
        # default base, and a test that could pass for either reason would not
        # notice if the pattern list were emptied.
        with pytest.raises(BranchPolicyViolation, match="protected pattern"):
            DeployTarget(remote="perftest", branch=branch, base_branch="capstone/perf-agent")

    @pytest.mark.parametrize("branch", ["release/2.1", "hotfix/urgent"])
    def test_release_and_hotfix_branches_are_refused_by_pattern(self, branch):
        with pytest.raises(BranchPolicyViolation):
            DeployTarget(remote="perftest", branch=branch)

    def test_the_operators_own_branch_is_refused_even_when_not_on_the_list(self):
        """The list cannot enumerate every branch a person works from. Whatever
        `base_branch` says is off limits by definition -- that is the branch the
        sandbox is created FROM, and writing to it is the thing this exists to
        stop."""
        with pytest.raises(BranchPolicyViolation, match="never writes to"):
            DeployTarget(remote="perftest", branch="wip", base_branch="wip")

    def test_the_dedicated_sandbox_branch_is_allowed(self):
        target = DeployTarget(
            remote="perftest", branch="perftest_sandbox", base_branch="capstone/perf-agent"
        )

        assert target.branch == "perftest_sandbox"

    def test_an_unconfigured_target_is_not_a_policy_breach(self):
        """An empty branch means no deploy is configured -- a manual target --
        which is a legitimate state, not a violation."""
        assert DeployTarget().branch == ""

    def test_the_shipped_profile_passes_its_own_rule(self):
        target = TargetProfile.named("spring-boot").deploy

        assert target.branch == "perftest_sandbox"
        assert target.base_branch != target.branch

    def test_it_fails_at_load_time_not_at_push_time(self, tmp_path):
        """A bad profile should fail during `crucible plan`, before anything has
        been measured or deployed -- not eight minutes into a campaign."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "name: x\nruntime: jvm\ncause_families: [gc_pressure]\n"
            "deploy: {remote: r, branch: main}\n",
            encoding="utf-8",
        )

        with pytest.raises(BranchPolicyViolation):
            TargetProfile.load(bad)


class TestTheSandboxBranchIsCreatedFromTheBaseBranch:
    """The other half: never write to the operator's branch, but do start from it."""

    def test_an_existing_branch_is_left_alone(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return 0, "abc123	refs/heads/perftest_sandbox"

        note = GitPushDeployer(target=pipeline_target(), _runner=runner).ensure_branch()

        assert "already exists" in note
        assert not any("push" in a for a in calls)

    def test_a_missing_branch_is_created_from_base_and_nothing_else_is_written(self):
        """The base branch is READ and never written. Without this a first
        campaign against a fresh remote fails on a missing ref -- and the obvious
        fix, "push to the branch that does exist", is exactly what must not
        happen."""
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv[1] == "ls-remote":
                return 0, ""            # absent
            if argv[1] == "rev-parse":
                return 0, "basesha1234567890"
            return 0, ""

        deployer = GitPushDeployer(
            target=pipeline_target(base_branch="capstone/perf-agent"), _runner=runner
        )
        note = deployer.ensure_branch()

        assert "created perftest_sandbox" in note
        pushes = [a for a in calls if a[1] == "push"]
        assert len(pushes) == 1
        assert pushes[0][-1] == "basesha1234567890:refs/heads/perftest_sandbox"
        # The destination is the sandbox. Nothing anywhere writes to the base.
        assert not any("refs/heads/capstone/perf-agent" in part for part in pushes[0])

    def test_creating_a_branch_is_still_a_push_and_obeys_the_same_refusals(self):
        """Branch creation is a push, so it goes through `_check_push_command`.

        Exercised with a forced refspec, because that is the refusal this path
        can actually reach: the destination is built from config, but the SOURCE
        is a sha read from git, and a `+` prefix on a refspec silently forces the
        update. Refusing it here matters for the same reason 19.3 refuses
        force-push anywhere else -- it would rewrite a branch other experiments
        depend on.
        """
        def runner(argv):
            if argv[1] == "ls-remote":
                return 0, ""
            if argv[1] == "rev-parse":
                return 0, "+deadbeef"      # a forced refspec, however it got here
            return 0, ""

        deployer = GitPushDeployer(target=pipeline_target(), _runner=runner)

        with pytest.raises(DeployError, match="forced refspec"):
            deployer.ensure_branch()

    def test_an_unresolvable_base_branch_fails_loudly(self):
        def runner(argv):
            if argv[1] == "ls-remote":
                return 0, ""
            if argv[1] == "rev-parse":
                return 128, "unknown revision"
            return 0, ""

        deployer = GitPushDeployer(target=pipeline_target(base_branch="nope"), _runner=runner)

        with pytest.raises(DeployError, match="does not resolve"):
            deployer.ensure_branch()
