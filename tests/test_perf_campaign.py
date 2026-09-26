"""Campaign assertions — new group, week 2. DESIGN.md §4.4-4.7, §7, §8, §19.

REVIEWED AND APPROVED by the operator, 20 September 2026.

The loop is where the integrity rules become behaviour, so most of these tests
drive a whole campaign with fake collaborators and then assert on the MANIFEST
rather than on a return value. That is deliberate: the manifest is what a scorer
and a human read months later, and a rule that holds in memory but is not
recorded may as well not hold.

Four groups carry the weight:

- **Refusals before anything runs.** Production, an undeclared environment kind,
  a missing noise floor, a locked deploy branch.
- **The verdict arithmetic.** A move smaller than the environment's measured
  noise is INCONCLUSIVE, not a win. On the Oracle box three identical runs spread
  2.08%, so a 1.5% "improvement" is run-to-run variation wearing a result's
  clothes.
- **Never measuring the wrong build.** §19.6 — an unverified deploy stops the
  campaign rather than producing a plausible number attributed to the wrong
  configuration.
- **Abort semantics.** The in-flight experiment is discarded, verified ones are
  kept, and a deployed change is rolled back on the box as well as in the
  workspace (§19.9).

The `test_the_verdict_uses_the_measurement_not_the_prediction` case is the K3
lesson in test form: the agent predicted 140 ms and measured 93 ms. If the
prediction were ever used as the "after" figure, a wrong prediction would grade
itself correct.
"""

import asyncio
import json
import time

import pytest

from crucible.perf.applicator import Applicator, Change, Proposal
from crucible.perf.approval import (
    DenyingGate,
    ManualStep,
    PreapprovedGate,
    confirm_manual_step,
    pending_manual_steps,
)
from crucible.perf.campaign import (
    IMPROVED,
    INCONCLUSIVE,
    MAX_CONSECUTIVE_REFUSALS,
    NOT_MEASURED,
    WORSE,
    BranchLock,
    Campaign,
    CampaignRefused,
    Sla,
    abort_requested,
    calibration_error_pct,
    check_environment,
    margin_over_noise,
    request_abort,
    verdict_for,
)
from crucible.perf.deploy import DeployBlocked, DeployResult, DeployTarget
from crucible.perf.diagnosis import Diagnosis
from crucible.perf.profile import TargetProfile
from crucible.perf.runner import LoadResult, Scenario

PROPERTIES = "spring.datasource.hikari.maximum-pool-size=10\nperflab.cache.enabled=true\n"

SLA_YAML = """\
name: perflab-db-latency
environment:
  name: box-a
  kind: pre-prod
  target_base_url: http://10.0.0.79:8080
objective:
  endpoint: /api/db
  p99_ms: 120
  error_rate_pct: 1.0
noise:
  p99_spread_pct: 2.08
  measured_on: 2026-09-12
host_contention:
  cpu_steal_abort_pct: 10.0
"""


@pytest.fixture
def sla(tmp_path):
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML, encoding="utf-8")
    return Sla.load(path)


@pytest.fixture
def profile():
    return TargetProfile.from_mapping({
        "name": "spring-boot",
        "runtime": "jvm",
        "cause_families": ["connection_pool_exhaustion"],
        "config_file": "application.properties",
        "allowed_properties": {
            "spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100},
        },
        "protected_paths": ["config/slo.yaml", "locust/**"],
        "deploy": {"branch": "perftest_sandbox"},
    })


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "application.properties").write_text(PROPERTIES, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestTheCampaignRefusesBeforeItStarts:
    """§8 and §19.1. Every later guardrail assumes a target that can be broken."""

    def test_production_is_refused(self, tmp_path):
        """Not a warning, not a flag. Crucible edits configuration on a running
        service and restarts it; the entire safety argument in §19 assumes
        pre-prod, so this refusal is what makes the rest of it true."""
        path = tmp_path / "prod.yaml"
        path.write_text(SLA_YAML.replace("kind: pre-prod", "kind: production"), encoding="utf-8")

        with pytest.raises(CampaignRefused, match="never runs against production"):
            check_environment(Sla.load(path))

    def test_an_environment_with_no_declared_kind_is_refused(self, tmp_path):
        """The assumption must be STATED by the environment, not held as a
        convention in somebody's head. An unlabelled environment is refused
        rather than assumed safe."""
        path = tmp_path / "blank.yaml"
        path.write_text(SLA_YAML.replace("kind: pre-prod", "kind: ''"), encoding="utf-8")

        with pytest.raises(CampaignRefused, match="declares no kind"):
            check_environment(Sla.load(path))

    def test_an_unrecognised_kind_is_refused_rather_than_waved_through(self, tmp_path):
        """The list is a whitelist on purpose. A kind nobody has thought about
        should stop a campaign; treating unknown as safe is how 'prod-canary'
        gets measured one day."""
        path = tmp_path / "odd.yaml"
        path.write_text(SLA_YAML.replace("kind: pre-prod", "kind: prod-canary"), encoding="utf-8")

        with pytest.raises(CampaignRefused, match="not one Crucible recognises"):
            check_environment(Sla.load(path))

    def test_an_sla_with_no_measured_noise_floor_is_refused(self, tmp_path):
        """Without it every improvement looks real, including the ones that are
        run-to-run variation. The number is a property of the environment and
        cannot be defaulted."""
        path = tmp_path / "nonoise.yaml"
        path.write_text(SLA_YAML.replace("  p99_spread_pct: 2.08\n", ""), encoding="utf-8")

        with pytest.raises(CampaignRefused, match="no noise.p99_spread_pct"):
            Sla.load(path)

    def test_a_missing_sla_is_refused_rather_than_defaulted(self, tmp_path):
        """Inventing a threshold would be the agent grading itself by a friendlier
        route than editing one."""
        with pytest.raises(CampaignRefused, match="agent grading itself"):
            Sla.load(tmp_path / "nope.yaml")

    def test_the_sla_object_exposes_no_way_to_change_the_objective(self, sla):
        """§4.4. The absence of a setter is the point: an agent that can move its
        own goalpost passes every time."""
        with pytest.raises(Exception):
            sla.p99_ms = 5000  # type: ignore[misc]


class TestTheSlaIsThreeValued:
    """Principle 1. Verified and unverified must never look identical."""

    def test_a_measurement_inside_the_objective_passes(self, sla):
        assert sla.met_by(90.0, 0.0) is True

    def test_a_measurement_outside_the_objective_fails(self, sla):
        assert sla.met_by(200.0, 0.0) is False

    def test_a_high_error_rate_fails_even_when_latency_passes(self, sla):
        """A fast service that is returning errors has not met its objective.
        Latency measured over mostly-500s is not the latency anybody cares about."""
        assert sla.met_by(90.0, 8.0) is False

    def test_an_unmeasured_p99_is_neither_pass_nor_fail(self, sla):
        """Returning False here would collapse 'we did not measure' into 'it
        failed', and a results table could no longer tell them apart."""
        assert sla.met_by(None, None) is None


# ---------------------------------------------------------------------------
# Verdict arithmetic
# ---------------------------------------------------------------------------


class TestTheVerdictIsJudgedAgainstMeasuredNoise:
    """§4.7 by arithmetic. Being right by luck is not being right."""

    def test_a_clear_improvement_is_improved(self):
        verdict, why = verdict_for(98.0, 58.0, 2.08)

        assert verdict == IMPROVED
        assert "40" in why or "fell" in why

    def test_a_clear_regression_is_worse(self):
        assert verdict_for(58.0, 98.0, 2.08)[0] == WORSE

    def test_a_move_smaller_than_the_noise_floor_is_inconclusive_not_a_win(self):
        """The Oracle box spread 2.08% across three identical runs, so 98 -> 97
        is not an improvement. Reporting it as one is how an agent accumulates a
        record of successes it did not earn."""
        verdict, why = verdict_for(98.0, 97.0, 2.08)

        assert verdict == INCONCLUSIVE
        assert "noise floor" in why

    def test_a_small_regression_inside_the_noise_floor_is_also_inconclusive(self):
        """Symmetry matters. A rule that only absorbed improvements into noise
        would make the agent look conservative and its regressions look real."""
        assert verdict_for(98.0, 99.0, 2.08)[0] == INCONCLUSIVE

    def test_the_noise_floor_is_taken_from_the_sla_not_hardcoded(self):
        """The local Windows machine measured 14.3%; the Oracle box 2.08%. The
        same measured pair is a different verdict on a different box, and that is
        correct."""
        assert verdict_for(98.0, 90.0, 2.08)[0] == IMPROVED
        assert verdict_for(98.0, 90.0, 14.3)[0] == INCONCLUSIVE

    def test_an_unmeasured_side_is_not_measured_rather_than_a_comparison(self):
        verdict, why = verdict_for(98.0, None, 2.08)

        assert verdict == NOT_MEASURED
        assert "never measured" in why


class TestTheMarginOverNoiseIsRecorded:
    """W2-Q2. A verdict alone cannot tell a bare win from a decisive one."""

    def test_a_move_at_exactly_the_floor_is_one_margin(self):
        assert margin_over_noise(100.0, 97.92, 2.08) == pytest.approx(1.0, abs=0.01)

    def test_a_marginal_win_lands_between_one_and_two(self):
        """The band where repeats earn their wall clock. On this environment it is
        98 ms -> 96 ms at one floor and 98 -> 94 at two: the whole marginal range
        is two milliseconds, and one of the three baseline runs that PRODUCED the
        floor already read 96 with nothing changed."""
        margin = margin_over_noise(98.0, 95.0, 2.08)

        assert 1.0 < margin < 2.0

    def test_a_decisive_win_is_well_above_two(self):
        assert margin_over_noise(1300.0, 95.0, 2.08) > 40

    def test_a_move_inside_the_floor_is_below_one(self):
        """Which is the same thing the verdict says as INCONCLUSIVE -- the two
        must never disagree, because a reader seeing IMPROVED with a margin of
        0.8 would not know which to believe."""
        margin = margin_over_noise(98.0, 97.0, 2.08)
        verdict, _ = verdict_for(98.0, 97.0, 2.08)

        assert margin < 1.0
        assert verdict == INCONCLUSIVE

    def test_it_is_none_when_either_side_was_not_measured(self):
        """Zero would read as "no improvement" by an experiment that was never
        measured at all."""
        assert margin_over_noise(98.0, None, 2.08) is None
        assert margin_over_noise(None, 95.0, 2.08) is None

    def test_the_margin_reaches_the_manifest(self, profile, sla, workspace):
        """The scorer reads manifests from disk, so a value held only in memory
        is a value the scorer does not have."""
        result = asyncio.run(_campaign(profile, sla, workspace, p99s=[300.0, 58.0]).run())

        assert result.experiments[0].margin_over_noise > 2.0

    def test_it_is_direction_agnostic(self):
        """A regression four floors deep is as far from noise as an improvement
        four floors clear. The sign lives in the verdict; this is distance."""
        assert margin_over_noise(100.0, 90.0, 2.08) == pytest.approx(
            margin_over_noise(100.0, 110.0, 2.08), abs=0.01
        )


class TestCalibrationIsTrackedNeverActedOn:
    """§4.5. The prediction is a signal, not a decision input."""

    def test_the_k3_numbers_produce_a_conservative_calibration_error(self):
        """Predicted 140 ms, measured 93 ms: the agent was conservative by about
        1.5x. That is worth recording and scoring, and worth nothing as a result."""
        assert calibration_error_pct(140.0, 93.0) == pytest.approx(50.5, abs=0.5)

    def test_a_missing_prediction_is_none_rather_than_zero(self):
        """Zero would read as perfect calibration by an agent that never
        predicted anything."""
        assert calibration_error_pct(None, 93.0) is None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class TestTheLoopEndToEnd:
    """Driven with fakes, asserted on the manifest."""

    def test_a_baseline_that_already_meets_the_sla_stops_without_experimenting(
        self, profile, sla, workspace
    ):
        """Nothing to fix. Finding out what headroom exists means raising the
        load, which changes the load profile — and §4.4 says the agent may never
        do that, so it is an operator-flagged ceiling probe instead (§20)."""
        campaign = _campaign(profile, sla, workspace, p99s=[80.0])

        result = asyncio.run(campaign.run())

        assert result.experiments == []
        assert "already meets the SLA" in result.stopped_reason
        assert "ceiling probe" in result.stopped_reason

    def test_an_improving_change_is_kept_and_the_run_stops_when_the_sla_is_met(
        self, profile, sla, workspace
    ):
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0])

        result = asyncio.run(campaign.run())

        assert len(result.experiments) == 1
        manifest = result.experiments[0]
        assert manifest.verdict == IMPROVED and manifest.kept
        assert manifest.sla_met_after is True
        assert "maximum-pool-size=20" in (workspace / "application.properties").read_text(encoding="utf-8")

    def test_a_change_inside_the_noise_floor_is_reverted(self, profile, sla, workspace):
        """An unproven change left in place becomes the NEXT experiment's
        baseline, so the campaign would then be measuring against something
        nobody verified."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 299.0, 299.0])

        result = asyncio.run(campaign.run())

        assert result.experiments[0].verdict == INCONCLUSIVE
        assert not result.experiments[0].kept
        assert "maximum-pool-size=10" in (workspace / "application.properties").read_text(encoding="utf-8")

    def test_a_regression_is_reverted_and_fed_back_as_ruled_out(self, profile, sla, workspace):
        """The next diagnosis must not re-propose it. Within a campaign the
        ruled-out list does the job the journal RAG does across campaigns."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 500.0, 500.0])

        result = asyncio.run(campaign.run())

        assert result.experiments[0].verdict == WORSE
        assert not result.experiments[0].kept
        assert any("WORSE" in r for r in result.ruled_out)

    def test_the_verdict_uses_the_measurement_not_the_prediction(self, profile, sla, workspace):
        """The K3 lesson. The agent predicts 70; the re-measurement says 299. The
        verdict must follow the measurement, or a confident wrong prediction
        would grade itself correct."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 299.0, 299.0], predicted=70.0)

        result = asyncio.run(campaign.run())
        manifest = result.experiments[0]

        assert manifest.predicted_p99_ms == 70.0
        assert manifest.after["load"]["p99_ms"] == 299.0
        assert manifest.verdict == INCONCLUSIVE

    def test_the_noise_floor_the_verdict_was_judged_against_is_recorded(
        self, profile, sla, workspace
    ):
        """A later reader must know which number a verdict was judged against —
        the threshold is a property of the box and boxes change."""
        result = asyncio.run(_campaign(profile, sla, workspace, p99s=[300.0, 58.0]).run())

        assert result.experiments[0].noise_floor_pct == 2.08

    def test_an_abstention_stops_the_campaign_without_marking_it_a_failure(
        self, profile, sla, workspace
    ):
        """Abstention on insufficient evidence is a correct outcome, not a
        failure (principle 2). It must not read as a crash in the report."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0],
            proposal=Proposal(cause_family="", changes=(), abstained=True,
                              abstain_reason="gauges were never sampled"),
        )

        result = asyncio.run(campaign.run())

        assert "declined to propose" in result.stopped_reason
        assert "correct outcome" in result.stopped_reason

    def test_a_guard_refusal_does_not_consume_an_experiment_slot(self, profile, sla, workspace):
        """W2-Q4. A model proposing something forbidden is evidence about the
        agent and is recorded -- but nothing was applied and nothing measured, so
        the operator who asked for N MEASUREMENTS still gets N."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0] * 8,
            proposal=Proposal(
                cause_family="connection_pool_exhaustion",
                changes=(Change(prop="server.port", value=9999),),
            ),
            max_experiments=5,
        )

        result = asyncio.run(campaign.run())

        assert all(m.refused_by_guard for m in result.experiments)
        assert "could not produce a permitted proposal" in result.stopped_reason
        assert "not an exhausted campaign" in result.stopped_reason

    def test_three_refusals_in_a_row_stop_the_campaign(self, profile, sla, workspace):
        """Refusals being free needs something else to stop a model looping on
        forbidden proposals. Three is the cap, and it fails with the real reason."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0] * 8,
            proposal=Proposal(
                cause_family="connection_pool_exhaustion",
                changes=(Change(prop="server.port", value=9999),),
            ),
            max_experiments=5,
        )

        result = asyncio.run(campaign.run())

        assert len(result.experiments) == MAX_CONSECUTIVE_REFUSALS

    def test_refused_experiments_still_get_distinct_numbers(self, profile, sla, workspace):
        """Found while implementing W2-Q4. The first version refunded the slot by
        decrementing the experiment NUMBER, so the next proposal reused it -- and
        approval files are named by that number, so two different proposals would
        have parked at `001.request.json` and one operator decision could have
        answered the other."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0] * 8,
            proposal=Proposal(
                cause_family="connection_pool_exhaustion",
                changes=(Change(prop="server.port", value=9999),),
            ),
            max_experiments=5,
        )

        result = asyncio.run(campaign.run())
        numbers = [m.experiment for m in result.experiments]

        assert numbers == sorted(set(numbers))

    def test_the_refusal_is_fed_back_so_it_is_not_proposed_again(
        self, profile, sla, workspace
    ):
        """It must reach the next diagnosis, or the model spends every remaining
        attempt on the same refused idea."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0] * 8,
            proposal=Proposal(
                cause_family="connection_pool_exhaustion",
                changes=(Change(prop="server.port", value=9999),),
            ),
            max_experiments=5,
        )

        result = asyncio.run(campaign.run())

        assert any("refused by guard" in r for r in result.ruled_out)

    def test_a_declined_approval_stops_the_campaign_and_applies_nothing(
        self, profile, sla, workspace
    ):
        campaign = _campaign(profile, sla, workspace, p99s=[300.0], gate=DenyingGate())

        result = asyncio.run(campaign.run())

        assert "declined" in result.stopped_reason
        assert "maximum-pool-size=10" in (workspace / "application.properties").read_text(encoding="utf-8")

    def test_the_manifest_is_written_even_when_the_campaign_stops_early(
        self, profile, sla, workspace, tmp_path
    ):
        """Everything measured before the stop still happened, and a scorer reads
        manifests from disk (§4.6)."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0], gate=DenyingGate())
        campaign.results_dir = tmp_path / "results"

        result = asyncio.run(campaign.run())
        written = json.loads((tmp_path / "results" / f"{result.run_id}.json").read_text(encoding="utf-8"))

        assert written["run_id"] == result.run_id
        assert written["sla"]["p99_ms"] == 120


class TestTheApprovalCardShowsWhatIsChangingFrom:
    """Found during the local end-to-end rehearsal, not by reasoning."""

    def test_the_card_names_the_current_value_not_none(self, profile, sla, workspace):
        """The model does not supply `previous`, and the applicator resolves it at
        apply time -- which is AFTER the operator has decided. Without this the
        card read "maximum-pool-size None -> 20", and approving a change without
        being shown what it changes FROM is most of the way to approving it
        blind."""
        gate = _RecordingGate()
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0], gate=gate)

        asyncio.run(campaign.run())

        assert gate.requests[0].summary.endswith("'10' -> 20")

    def test_the_binding_surface_is_unaffected(self, profile, sla, workspace):
        """`previous` is display only. The params the approval is bound to are
        property/value pairs, and adding the old value must not change them --
        otherwise every existing decision file would stop matching."""
        gate = _RecordingGate()
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0], gate=gate)

        asyncio.run(campaign.run())

        assert gate.requests[0].params == {"spring.datasource.hikari.maximum-pool-size": 20}

    def test_an_unreadable_config_file_degrades_rather_than_raising(
        self, profile, sla, workspace
    ):
        """A display concern must not become a campaign failure.

        Asserted on the helper rather than through a whole campaign: a missing
        config file legitimately fails the APPLY a moment later, so a campaign-level
        test would pass for the wrong reason. What is being checked here is only
        that resolving the current value does not raise.
        """
        campaign = _campaign(profile, sla, workspace, p99s=[300.0])
        (workspace / "application.properties").unlink()
        proposal = Proposal(
            cause_family="connection_pool_exhaustion",
            changes=(Change(prop="spring.datasource.hikari.maximum-pool-size", value=20),),
        )

        resolved = campaign._with_current_values(proposal)

        assert resolved.changes[0].value == 20
        assert resolved.changes[0].previous is None

    def test_the_applicator_still_re_reads_previous_at_apply_time(
        self, profile, sla, workspace
    ):
        """The card's value could be stale by the time the change is applied, and
        the REVERT must use what was actually in force. The two reads are
        deliberately separate."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0])

        result = asyncio.run(campaign.run())

        assert result.experiments[0].apply_result["changes"][0]["previous"] == "10"


class TestComparabilityAcrossModels:
    """§3.2. A downgrade is permitted; an invisible one is not."""

    def test_a_campaign_on_one_model_is_not_flagged(self, profile, sla, workspace):
        result = asyncio.run(_campaign(profile, sla, workspace, p99s=[300.0, 58.0]).run())

        assert not result.spans_multiple_models
        assert result.as_dict()["comparability_warning"] == ""

    def test_a_campaign_spanning_two_models_is_flagged_in_the_manifest(
        self, profile, sla, workspace
    ):
        """Experiments diagnosed by different models are not comparable with each
        other. The report must say so rather than averaging across them."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0, 299.0, 299.0, 299.0, 299.0],
            models=["gemini-2.5-flash", "llama-3.3-70b"], max_experiments=2,
        )

        result = asyncio.run(campaign.run())

        assert result.spans_multiple_models
        assert "not directly comparable" in result.as_dict()["comparability_warning"]


class TestNoMeasurementBeforeTheCommitIsProven:
    """§19.6. The failure that produces a perfectly plausible wrong number."""

    def test_an_unverified_deploy_stops_the_campaign_before_measuring(
        self, profile, sla, workspace
    ):
        """During the K1 cloud run a full set of measurements was taken against a
        target that had never been restarted. Nothing downstream catches that —
        the numbers look fine — so it has to be caught here."""
        deployer = _FakeDeployer(results=[DeployResult(commit="abc", deployed=True, verified=False,
                                                       reason="target never reported abc")])
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0], deployer=deployer)

        result = asyncio.run(campaign.run())

        assert "never reported abc" in result.stopped_reason
        assert result.experiments[0].verdict == NOT_MEASURED
        # The second measurement must never have happened.
        assert result.experiments[0].after == {}

    def test_a_verified_deploy_records_the_commit_on_the_experiment(
        self, profile, sla, workspace
    ):
        """§19.7. 'Which change produced this number' is answered from the
        journal rather than reconstructed by inference."""
        deployer = _FakeDeployer(
            results=[DeployResult(commit="abc123", deployed=True, verified=True)]
        )
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0], deployer=deployer)

        result = asyncio.run(campaign.run())

        assert result.experiments[0].deployed_commit == "abc123"

    def test_a_manual_deploy_blocks_and_is_recorded_as_a_manual_step(
        self, profile, sla, workspace
    ):
        """§11. A run with human intervention is not comparable to a fully
        autonomous one, so the manifest has to be able to tell them apart."""
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0, 58.0], deployer=_BlockingDeployer()
        )

        result = asyncio.run(campaign.run())

        assert result.experiments[0].manual_steps
        assert "manual deploy" in result.stopped_reason


class TestAManualStepPausesBeforeMeasuring:
    """W2-Q8, and the bug found by attempting the cloud run.

    Before the fix the loop recorded the manual step and measured anyway --
    attributing the target's OLD numbers to a change that had never been put in
    force. The local rehearsal used an automated restarter and never reached the
    branch; cross-box, Crucible has no credential to restart Box A, so the
    restart is genuinely manual.
    """

    def _manual(self, profile, sla, workspace, **over):
        from crucible.perf.applicator import ManualRestarter
        from crucible.perf.profile import RestartContract

        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 58.0], **over)
        campaign.applicator.restarter = ManualRestarter(
            RestartContract(manual=True, instructions="restart the JVM on Box A by hand")
        )
        return campaign

    def test_it_never_measures_before_the_change_is_in_force(self, profile, sla, workspace):
        """The whole point. Measuring now would attribute the PREVIOUS
        configuration's numbers to this change -- the same silent error as
        measuring before a deploy lands (19.6), and just as plausible-looking."""
        campaign = self._manual(profile, sla, workspace, wait=False)

        result = asyncio.run(campaign.run())

        assert result.experiments[0].after == {}
        assert result.experiments[0].verdict == NOT_MEASURED

    def test_it_pauses_and_resumes_rather_than_forcing_a_new_run(
        self, profile, sla, workspace
    ):
        """Section 11 says the campaign BLOCKS rather than failing, and section
        7's pause holds without discarding. Aborting would make the operator redo
        the baseline because somebody had to bounce a JVM."""
        campaign = self._manual(profile, sla, workspace)
        campaign.manual_step_timeout_s = 5.0

        # Confirm from "another terminal" while the campaign waits.
        import threading

        def confirm():
            for _ in range(100):
                if pending_manual_steps(campaign.state_dir, campaign.run_id):
                    confirm_manual_step(
                        campaign.state_dir, campaign.run_id, 1,
                        responder="operator", note="restarted by hand",
                    )
                    return
                time.sleep(0.02)

        thread = threading.Thread(target=confirm)
        thread.start()
        result = asyncio.run(campaign.run())
        thread.join()

        assert result.experiments[0].after != {}
        assert result.experiments[0].verdict == IMPROVED

    def test_the_human_intervention_is_recorded_on_the_manifest(
        self, profile, sla, workspace
    ):
        """Section 11: a run with human intervention is not comparable with a
        fully autonomous one, so a later reader must be able to tell them apart
        rather than inferring it."""
        campaign = self._manual(profile, sla, workspace, wait=False)

        result = asyncio.run(campaign.run())

        assert result.experiments[0].manual_steps
        assert "Box A by hand" in result.experiments[0].manual_steps[0]

    def test_a_confirmation_carries_no_parameters_and_is_not_an_approval(
        self, tmp_path
    ):
        """The binding check belongs to approvals and to nothing else. "I
        finished the restart" authorises nothing -- it reports -- so it is a
        different file with a different action, exempt from binding by
        construction rather than by exception."""
        step = ManualStep(run_id="r1", experiment=1, instructions="do it", state_dir=tmp_path)
        step.park()

        confirm_manual_step(tmp_path, "r1", 1, responder="operator")
        written = json.loads(step.confirmation_path().read_text(encoding="utf-8"))

        assert written["action"] == "manual_step_done"
        assert "params" not in written

    def test_the_manual_files_do_not_collide_with_approval_files(self, tmp_path):
        """One experiment can wait TWICE -- once for authorisation, later for a
        manual step. A shared filename would let the answer to one satisfy the
        other."""
        step = ManualStep(run_id="r1", experiment=1, instructions="do it", state_dir=tmp_path)

        assert step.request_path().name == "001.manual.request.json"
        assert step.confirmation_path().name == "001.manual.json"

    def test_confirming_a_step_that_was_never_parked_fails(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no manual step is pending"):
            confirm_manual_step(tmp_path, "r1", 7, responder="operator")

    def test_a_timeout_leaves_the_campaign_paused_not_applied(
        self, profile, sla, workspace
    ):
        """Proceeding on a deadline would measure a target whose change was never
        put in force, which is the exact failure the pause exists to prevent."""
        campaign = self._manual(profile, sla, workspace)
        campaign.manual_step_timeout_s = 0.0

        result = asyncio.run(campaign.run())

        assert "paused, not failed" in result.stopped_reason
        assert result.experiments[0].after == {}

    def test_the_change_stays_applied_so_the_human_restarts_into_it(
        self, profile, sla, workspace
    ):
        """Pausing is not reverting. The operator is being asked to restart INTO
        the new configuration, so undoing the edit first would make the
        instruction meaningless."""
        campaign = self._manual(profile, sla, workspace, wait=False)

        asyncio.run(campaign.run())

        text = (workspace / "application.properties").read_text(encoding="utf-8")
        assert "maximum-pool-size=20" in text


class TestAbort:
    """§7 and §19.9. Discard the in-flight one; keep what was verified."""

    def test_an_abort_marker_stops_the_loop_at_the_next_boundary(
        self, profile, sla, workspace, tmp_path
    ):
        """A boundary, not an interrupt. Tearing down mid-apply would leave the
        target in a state no manifest describes, which is worse than not
        aborting."""
        campaign = _campaign(profile, sla, workspace, p99s=[300.0] * 6, max_experiments=3)
        campaign.state_dir = tmp_path / "state"
        request_abort(campaign.state_dir, campaign.run_id, "unrelated outage")

        result = asyncio.run(campaign.run())

        assert result.experiments == []
        assert "unrelated outage" in result.stopped_reason

    def test_abort_rolls_the_target_back_to_the_last_verified_commit(
        self, profile, sla, workspace, tmp_path
    ):
        """The workspace revert leaves HEAD at the last good experiment, but the
        box is still running the aborted one. An abort that stopped locally would
        leave the environment in a state no manifest describes."""
        deployer = _FakeDeployer(results=[
            DeployResult(commit="good1", deployed=True, verified=True),
            DeployResult(commit="bad2", deployed=True, verified=False, reason="never landed"),
            DeployResult(commit="good1", deployed=True, verified=True),
        ])
        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0, 299.0, 299.0, 299.0],
            deployer=deployer, max_experiments=3,
        )
        campaign.state_dir = tmp_path / "state"

        result = asyncio.run(campaign.run())

        assert deployer.commits[-1] == "good1"
        assert "rolled the target back to good1" in result.stopped_reason

    def test_abort_with_nothing_verified_says_a_human_is_needed(
        self, profile, sla, workspace, tmp_path
    ):
        """There is no last-good commit to roll back to. Saying so is better than
        silently leaving the box on whatever the aborted experiment deployed."""
        deployer = _FakeDeployer(results=[
            DeployResult(commit="bad1", deployed=True, verified=False, reason="never landed"),
        ])
        campaign = _campaign(profile, sla, workspace, p99s=[300.0, 299.0], deployer=deployer)
        campaign.state_dir = tmp_path / "state"

        result = asyncio.run(campaign.run())

        assert "needs a human" in result.stopped_reason

    def test_the_abort_marker_is_readable_by_another_process(self, tmp_path):
        request_abort(tmp_path, "run-x", "operator changed their mind")

        assert abort_requested(tmp_path, "run-x") == "operator changed their mind"

    def test_no_marker_means_no_abort(self, tmp_path):
        assert abort_requested(tmp_path, "run-x") is None


class TestOneCampaignPerDeployBranch:
    """§19.10. Two campaigns on one branch interleave commits and void both."""

    def test_a_second_campaign_on_the_same_branch_is_refused(self, tmp_path):
        first = BranchLock(state_dir=tmp_path, branch="perftest_sandbox", run_id="run-1")
        first.acquire()

        with pytest.raises(CampaignRefused, match="already locked"):
            BranchLock(state_dir=tmp_path, branch="perftest_sandbox", run_id="run-2").acquire()

        first.release()

    def test_the_lock_names_its_holder_so_a_stale_one_can_be_identified(self, tmp_path):
        """An operator hitting the refusal needs to know whether it is a live run
        or a crashed one."""
        BranchLock(state_dir=tmp_path, branch="perftest_sandbox", run_id="run-1").acquire()

        with pytest.raises(CampaignRefused, match="run-1"):
            BranchLock(state_dir=tmp_path, branch="perftest_sandbox", run_id="run-2").acquire()

    def test_a_released_lock_can_be_taken_again(self, tmp_path):
        with BranchLock(state_dir=tmp_path, branch="b", run_id="run-1"):
            pass

        with BranchLock(state_dir=tmp_path, branch="b", run_id="run-2"):
            pass

    def test_different_branches_do_not_block_each_other(self, tmp_path):
        with BranchLock(state_dir=tmp_path, branch="one", run_id="run-1"):
            with BranchLock(state_dir=tmp_path, branch="two", run_id="run-2"):
                pass


class TestGatewayWarmUp:
    """Week 3, deliverable 1. `glc_v5` is hosted on Render's free tier
    (`DESIGN.md` 18) and spins down when idle, so an uncontrolled first model
    call pays a cold start inside the timed diagnosis step. The campaign warms
    the gateway itself, before anything that reaches the model."""

    def test_the_gateway_is_warmed_before_the_first_diagnosis_call(
        self, profile, sla, workspace
    ):
        order = []

        async def fake_warm_up():
            order.append("warm_up")

        campaign = _campaign(
            profile, sla, workspace, p99s=[300.0, 58.0], warm_up_gateway=fake_warm_up
        )
        real_diagnose = campaign.diagnoser.diagnose

        async def recording_diagnose(*args, **kwargs):
            order.append("diagnose")
            return await real_diagnose(*args, **kwargs)

        campaign.diagnoser.diagnose = recording_diagnose

        asyncio.run(campaign.run())

        assert order == ["warm_up", "diagnose"]

    def test_a_campaign_with_no_warm_up_configured_runs_unaffected(
        self, profile, sla, workspace
    ):
        """A scripted test campaign never sets this collaborator; it must not be
        required, only used when present."""
        campaign = _campaign(profile, sla, workspace, p99s=[80.0])

        result = asyncio.run(campaign.run())

        assert "already meets the SLA" in result.stopped_reason


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _campaign(
    profile,
    sla,
    workspace,
    *,
    p99s,
    proposal=None,
    predicted=None,
    gate=None,
    deployer=None,
    max_experiments=1,
    models=None,
    wait=True,
    warm_up_gateway=None,
):
    """A campaign whose measurement and diagnosis are scripted.

    ``p99s`` is consumed one per measurement: the first is the baseline, then one
    per experiment. Scripting the numbers is what lets the verdict rules be
    asserted exactly rather than approximately.
    """
    import tempfile
    from pathlib import Path

    state = Path(tempfile.mkdtemp())
    measurements = iter(p99s)

    def measure(scenario, run_id):
        p99 = next(measurements)
        load = LoadResult(
            run_id=run_id, scenario=scenario.name, p50_ms=p99 * 0.6, p99_ms=p99,
            rps=190.0, request_count=10000, failure_count=0, error_rate_pct=0.0,
        )
        return load, {"collector_version": "1.1.0", "available_evidence": {"metrics": True}}

    default_proposal = Proposal(
        cause_family="connection_pool_exhaustion",
        changes=(Change(prop="spring.datasource.hikari.maximum-pool-size", value=20),),
        reasoning="pending peaked at 43",
        confidence=0.8,
        predicted_p99_ms=predicted,
    )

    return Campaign(
        profile=profile,
        sla=sla,
        scenario=Scenario(name="db", host="http://target", warmup_s=0, measure_s=0),
        applicator=Applicator(profile=profile, workspace=workspace, _git=_FakeGit()),
        diagnoser=_ScriptedDiagnoser(proposal or default_proposal, models or ["gemini-2.5-flash"]),
        measure=measure,
        approval_gate=gate or PreapprovedGate(),
        deployer=deployer,
        max_experiments=max_experiments,
        state_dir=state,
        results_dir=state / "results",
        wait_for_manual_steps=wait,
        warm_up_gateway=warm_up_gateway,
    )


class _ScriptedDiagnoser:
    """Returns a fixed proposal, cycling through the model names given."""

    def __init__(self, proposal, models):
        self._proposal = proposal
        self._models = list(models)
        self._n = 0
        self.prior_findings_seen: list[str] = []

    async def diagnose(self, snapshot, sla, *, ruled_out=(), prior_findings=""):
        # Captured, not ignored: what history a diagnosis was shown is part of
        # what produced its answer (DESIGN.md section 14).
        self.prior_findings_seen.append(prior_findings)
        model = self._models[self._n % len(self._models)]
        self._n += 1
        return Diagnosis(proposal=self._proposal, provider="gemini", model=model)


class _RecordingGate:
    """Approves everything, keeping the request so the card can be asserted on."""

    name = "recording"

    def __init__(self):
        self.requests = []

    def request(self, request):
        from crucible.perf.approval import ApprovalDecision

        self.requests.append(request)
        return ApprovalDecision(
            state="approved", responder="operator", params=dict(request.params)
        )


class _FakeGit:
    """A git that always succeeds and hands back a fresh sha per commit.

    Injected so the loop tests need no repository. The alternative -- `git init`
    in every tmp_path -- would make these tests depend on the developer's git
    config (hooks, signing, user.email), which has nothing to do with what they
    are asserting.
    """

    def __init__(self):
        self.n = 0
        self.messages = []

    def __call__(self, argv):
        if argv[1] == "commit":
            self.n += 1
            self.messages.append(argv[argv.index("-m") + 1])
        if argv[1] == "rev-parse":
            return 0, f"sha{self.n:04d}"
        return 0, ""


class _FakeDeployer:
    name = "fake"

    def __init__(self, results):
        self._results = list(results)
        self.commits = []

    def deploy(self, commit):
        self.commits.append(commit)
        if self._results:
            result = self._results.pop(0)
            # Keep the recorded commit honest: the campaign asked for this one.
            if result.verified:
                return DeployResult(commit=result.commit, deployed=True, verified=True)
            return result
        return DeployResult(commit=commit, deployed=True, verified=True)


class _BlockingDeployer:
    name = "manual"

    def deploy(self, commit):
        raise DeployBlocked("deploy by hand on Box A, then the campaign resumes")


def _target(**over) -> DeployTarget:
    return DeployTarget(**({"remote": "perftest", "branch": "perftest_sandbox"} | over))
