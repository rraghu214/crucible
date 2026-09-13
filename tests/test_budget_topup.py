"""Budget top-up assertions — new group, 13 September 2026.

DRAFTED FOR REVIEW, not self-approved.

The behaviour under test: when spend pressure would force a downgrade to a
cheaper model, offer the operator a bounded window to raise the ceiling instead.
A downgrade is permitted (DESIGN.md section 3.2) but it changes the model
mid-run, which makes earlier experiments non-comparable with later ones — so it
is worth one ask before accepting it.

The risky part of this feature is not the happy path. It is that a blocking ask
sits inside a loop that makes many calls, on a system whose real budget is wall
clock (DESIGN.md section 7). Most of what follows tests that it cannot stall a
run.
"""

import time

import pytest

from crucible.economics import BudgetedGateway, EconomicsConfig, RunBudget
from crucible.economics.controller import CallSite
from crucible.economics.topup import (
    CallbackTopUp,
    NoTopUp,
    TopUpCoordinator,
    TopUpOutcome,
    TopUpRequest,
)


@pytest.fixture
def config() -> EconomicsConfig:
    return EconomicsConfig.load()


#: charge() prices the call itself, so it needs the real table.
PRICING = EconomicsConfig.load().pricing


def a_request(**over) -> TopUpRequest:
    base = dict(
        run_id="r1", principal="p", current_total=1.0, spent=0.8, pressure=0.8,
        requested_tier="standard", fallback_tier="economy", timeout_s=0.3,
    )
    return TopUpRequest(**(base | over))


class TestTheDefaultNeverBlocks:
    """An unattended run must behave exactly as it did before this existed."""

    def test_the_default_channel_declines_instantly(self):
        started = time.monotonic()
        outcome = NoTopUp().ask(a_request())

        assert outcome.granted is False
        assert time.monotonic() - started < 0.1, "the default must not wait"

    def test_not_asked_is_distinct_from_declined(self):
        """A run nobody was asked about is not a run where somebody said no.

        Both end in a downgrade, but they are different facts about the campaign
        and the manifest has to be able to tell them apart — otherwise "we ran
        cheap" reads identically whether or not a human ever had the choice.
        """
        assert NoTopUp().ask(a_request()).state == "not_asked"
        assert CallbackTopUp(lambda r: None, timeout_s=0.3).ask(a_request()).state == "declined"


class TestTheWaitIsBounded:
    """Wall clock is the real budget (DESIGN.md section 7), so the deadline is
    enforced here rather than trusted to whoever answers."""

    def test_a_callback_that_never_answers_times_out(self):
        def never(_req):
            time.sleep(30)
            return 99.0

        started = time.monotonic()
        outcome = CallbackTopUp(never, timeout_s=0.3).ask(a_request())
        waited = time.monotonic() - started

        assert outcome.state == "timed_out"
        assert outcome.granted is False
        assert waited < 3.0, f"waited {waited:.1f}s against a 0.3s deadline"

    def test_a_late_answer_is_discarded_not_applied(self):
        """The campaign has already moved on by the time a late answer arrives.

        Applying it would raise the ceiling in the middle of an experiment nobody
        is watching, which is a worse outcome than the downgrade it was meant to
        avoid.
        """
        def late(_req):
            time.sleep(0.5)
            return 99.0

        outcome = CallbackTopUp(late, timeout_s=0.1).ask(a_request())
        assert outcome.state == "timed_out"
        assert outcome.new_total is None

    def test_a_broken_channel_declines_rather_than_killing_the_run(self):
        def explode(_req):
            raise RuntimeError("UI is down")

        outcome = CallbackTopUp(explode, timeout_s=0.3).ask(a_request())

        assert outcome.state == "declined"
        assert "UI is down" in outcome.reason


class TestOfferedCeilings:
    def test_a_higher_ceiling_is_granted(self):
        outcome = CallbackTopUp(lambda r: 5.0, timeout_s=0.3).ask(a_request(current_total=1.0))

        assert outcome.granted is True
        assert outcome.new_total == 5.0

    def test_a_ceiling_that_is_not_higher_is_treated_as_a_decline(self):
        """Accepting it would clear the once-per-run flag for no benefit, and a
        'raise' to the same number is almost certainly a mistake at the keyboard."""
        outcome = CallbackTopUp(lambda r: 1.0, timeout_s=0.3).ask(a_request(current_total=1.0))

        assert outcome.granted is False
        assert outcome.state == "declined"


class TestAskedAtMostOncePerRun:
    """The rule that keeps this from stalling a campaign.

    Pressure stays above the threshold once it crosses it, so every later call
    would re-trigger the ask. At five minutes each, a campaign becomes a series
    of pauses.
    """

    def test_the_second_ask_is_not_put_to_a_human(self):
        asks = []
        channel = CallbackTopUp(lambda r: asks.append(r) or None, timeout_s=0.3)
        coordinator = TopUpCoordinator(channel=channel)

        coordinator.request(a_request())
        second = coordinator.request(a_request())

        assert len(asks) == 1, "a human was asked twice in one run"
        assert second.state == "not_asked"

    def test_the_answer_is_remembered_for_the_manifest(self):
        coordinator = TopUpCoordinator(channel=CallbackTopUp(lambda r: 5.0, timeout_s=0.3))
        coordinator.request(a_request())

        entries = coordinator.as_manifest_entries()
        assert len(entries) == 1
        assert entries[0]["granted"] is True
        assert entries[0]["new_total"] == 5.0


class TestTheBudgetItself:
    def test_top_up_only_ever_raises(self):
        """Lowering mid-run would retroactively refuse calls already admitted
        under the old ceiling, and that spend has already happened."""
        budget = RunBudget(total=1.0)

        with pytest.raises(ValueError, match="only raises"):
            budget.top_up(0.5)

    def test_a_raise_is_recorded_rather_than_applied_silently(self):
        """A campaign that finished inside its budget and one that finished
        because somebody raised the budget are different results."""
        budget = RunBudget(total=1.0)
        budget.spent = 0.8
        budget.top_up(5.0, responder="raghu", reason="approved in review")

        assert budget.total == 5.0
        entry = next(r for r in budget.refusals if r.get("event") == "budget_top_up")
        assert entry["previous_total"] == 1.0
        assert entry["spent_at_top_up"] == 0.8
        assert entry["responder"] == "raghu"


class TestTheSeamInTheController:
    """The behaviour as the gateway actually applies it."""

    def test_a_granted_top_up_avoids_the_downgrade(self, config):
        """The whole point: the campaign continues on ONE model, so its
        experiments stay comparable with each other."""
        # Pressure must sit BETWEEN downgrade_at (0.5) and refuse_at (0.9):
        # below and nothing downgrades, above and the run is refused outright
        # before the downgrade branch is ever reached.
        budget = RunBudget(total=1.0, run_id="r1")
        budget.spent = 0.6

        gateway = BudgetedGateway(
            transport=None, budget=budget, policy=config.policy(),
            pricing=config.pricing, ladder=config.ladder,
            topup=TopUpCoordinator(channel=CallbackTopUp(lambda r: 100.0, timeout_s=1.0)),
        )
        decision = gateway._decide(CallSite(node_id="n1", role="default", tier="standard"))

        assert decision.action == "proceed", decision.reason
        assert decision.tier.name == "standard", "the model changed despite a granted top-up"
        assert budget.total == pytest.approx(100.0)

    def test_an_unanswered_ask_still_downgrades(self, config):
        """The fallback must survive silence -- that is what makes it safe to ask."""
        budget = RunBudget(total=1.0, run_id="r1")
        budget.spent = 0.6

        gateway = BudgetedGateway(
            transport=None, budget=budget, policy=config.policy(),
            pricing=config.pricing, ladder=config.ladder,
            topup=TopUpCoordinator(channel=CallbackTopUp(lambda r: None, timeout_s=0.2)),
        )
        decision = gateway._decide(CallSite(node_id="n1", role="default", tier="standard"))

        assert decision.action == "downgrade"
        assert budget.total == pytest.approx(1.0), "ceiling moved without a grant"

    def test_the_default_gateway_never_asks(self, config):
        """No topup argument means no behaviour change from before this existed."""
        gateway = BudgetedGateway(
            transport=None, budget=RunBudget(total=1.0), policy=config.policy(),
            pricing=config.pricing, ladder=config.ladder,
        )
        assert isinstance(gateway.topup.channel, NoTopUp)
        assert gateway.topup.already_asked is False


def test_the_request_summary_is_actionable_without_reading_code():
    """A human woken by this has to understand the trade-off from the message."""
    text = a_request().summary()

    assert "standard" in text and "economy" in text
    assert "non-comparable" in text
    assert "300" in text or "0" in text  # the deadline is stated


def test_outcome_serialises_for_the_manifest():
    outcome = TopUpOutcome(state="granted", new_total=5.0, responder="raghu", waited_s=1.2)
    d = outcome.as_dict()

    assert d["granted"] is True and d["new_total"] == 5.0 and d["responder"] == "raghu"


class TestComparabilityDisclosure:
    """DESIGN.md section 3.2's other half: a downgrade is permitted, but it must
    never happen invisibly. The ledger already recorded which model served each
    call; nothing read it back until now."""

    def _charge(self, budget, model, decision="proceed"):
        budget.charge(
            node_id="n1", role="default", tier="standard", pricing=PRICING,
            provider="gemini", model=model, input_tokens=10, output_tokens=10,
            projected_cost=0.001, decision=decision, requested_tier="standard",
        )

    def test_a_single_model_run_raises_no_warning(self):
        budget = RunBudget(total=1.0)
        self._charge(budget, "gemini-3.5-flash-lite")
        self._charge(budget, "gemini-3.5-flash-lite")

        report = budget.comparability()
        assert report["spans_multiple_models"] is False
        assert report["warning"] == ""
        assert report["models_used"] == ["gemini-3.5-flash-lite"]

    def test_a_run_that_changed_model_says_so(self):
        """The failure this prevents: experiments 1-8 diagnosed by one model and
        9-20 by another, presented in one table as though comparable."""
        budget = RunBudget(total=1.0)
        self._charge(budget, "gemini-3.5-flash-lite")
        self._charge(budget, "openai/gpt-oss-120b", decision="downgrade")

        report = budget.comparability()
        assert report["spans_multiple_models"] is True
        assert report["downgraded_calls"] == 1
        assert "not" in report["warning"] and "comparable" in report["warning"]
        assert set(report["models_used"]) == {"gemini-3.5-flash-lite", "openai/gpt-oss-120b"}

    def test_the_model_recorded_is_the_one_that_ANSWERED(self):
        """Not the one that was requested. A provider-side substitution, a
        misconfigured tier and a budget downgrade are indistinguishable from the
        request alone -- only the response knows what actually ran."""
        budget = RunBudget(total=1.0)
        budget.charge(
            node_id="n1", role="default", tier="standard", pricing=PRICING,
            provider="groq",
            model="openai/gpt-oss-120b",                     # what answered
            input_tokens=10, output_tokens=10, projected_cost=0.001,
            decision="proceed", requested_tier="standard",   # what was asked for
        )

        assert budget.comparability()["models_used"] == ["openai/gpt-oss-120b"]

    def test_the_run_snapshot_carries_it_for_the_manifest(self):
        budget = RunBudget(total=1.0)
        self._charge(budget, "gemini-3.5-flash-lite")

        assert "comparability" in budget.snapshot()


class TestItCanAskAgainAfterAGrant:
    """Review point, 13 September 2026: once-per-RUN was too strict.

    A raised ceiling gets spent too. A campaign worth funding at 80% of the first
    budget is usually worth funding at 80% of the second, and refusing to ask
    again would silently force the very downgrade the top-up exists to avoid.
    So: a grant re-arms, a decline does not.
    """

    def test_a_grant_re_arms_so_it_can_ask_again(self):
        asks = []
        # Always offer double the CURRENT ceiling, so each grant is a real raise.
        coordinator = TopUpCoordinator(
            channel=CallbackTopUp(
                lambda r: asks.append(r.attempt) or r.current_total * 2, timeout_s=0.3
            )
        )

        first = coordinator.request(a_request(current_total=1.0))
        second = coordinator.request(a_request(current_total=2.0, spent=1.8))

        assert first.granted and second.granted
        assert asks == [1, 2], "the second ask should be attempt 2, not a repeat of 1"
        assert coordinator.already_asked is False, "a grant must not close the gate"

    def test_it_keeps_asking_for_as_long_as_the_operator_keeps_granting(self):
        """Until the run completes or somebody says no."""
        coordinator = TopUpCoordinator(
            channel=CallbackTopUp(lambda r: float(r.attempt) * 10, timeout_s=0.3)
        )
        for _ in range(4):
            coordinator.request(a_request())

        assert coordinator.attempts == 4
        assert coordinator.granted_total == 40.0

    def test_a_decline_closes_the_gate_for_the_rest_of_the_run(self):
        """An operator who said no once should not be asked again for the same
        reason -- pressure stays high, so every later call would re-trigger it."""
        asks = []
        coordinator = TopUpCoordinator(
            channel=CallbackTopUp(lambda r: asks.append(r.attempt) or None, timeout_s=0.3)
        )

        coordinator.request(a_request())
        second = coordinator.request(a_request())

        assert asks == [1], "a human was asked again after declining"
        assert second.state == "not_asked"
        assert coordinator.already_asked is True

    def test_a_timeout_also_closes_the_gate(self):
        """Silence is not an invitation to ask repeatedly."""
        def never(_req):
            time.sleep(5)
            return 9.0

        coordinator = TopUpCoordinator(channel=CallbackTopUp(never, timeout_s=0.1))
        coordinator.request(a_request())

        assert coordinator.already_asked is True
        assert coordinator.request(a_request()).state == "not_asked"

    def test_a_grant_after_a_decline_is_impossible(self):
        """Ordering matters: once closed, stays closed, even if the channel would
        now say yes. Otherwise a flaky channel could reopen a decided question."""
        answers = iter([None, 99.0])
        coordinator = TopUpCoordinator(
            channel=CallbackTopUp(lambda r: next(answers), timeout_s=0.3)
        )

        coordinator.request(a_request())
        assert coordinator.request(a_request()).granted is False


class TestThePromptIsTransparentAndCorrelated:
    """Review points: show the clock, and do not mistake the next thing the
    operator says for an answer to this question."""

    def test_the_prompt_states_when_it_was_asked_and_when_it_expires(self):
        """'You have 5 minutes' without saying five minutes from WHEN is not
        transparency -- the operator may read it long after it was sent."""
        req = a_request(timeout_s=300.0)
        text = req.summary()

        assert req.asked_at.strftime("%H:%M:%S") in text
        assert req.deadline.strftime("%H:%M:%S") in text
        assert "5 minute" in text

    def test_the_deadline_is_the_ask_time_plus_the_timeout(self):
        req = a_request(timeout_s=300.0)

        assert (req.deadline - req.asked_at).total_seconds() == pytest.approx(300.0)

    def test_every_request_carries_a_correlation_id(self):
        """A channel that also carries a conversation must be able to tell an
        answer to THIS question from the next thing the operator happens to type.
        The id is quoted in the prompt so the reply can name it."""
        first, second = a_request(), a_request()

        assert first.request_id != second.request_id
        assert first.request_id in first.summary()
        assert "Reply referencing" in first.summary()

    def test_the_attempt_number_is_visible_to_the_operator(self):
        """So a second ask reads as a second ask, not as a duplicate of the first."""
        assert "attempt 2" in a_request(attempt=2).summary()
