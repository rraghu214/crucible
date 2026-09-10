"""Quota arithmetic assertions — new group, week 1.

DRAFTED FOR REVIEW, not self-approved.

The point of these is narrow but real: the free-tier ceiling is the one thing
that can make week 4's benchmark impossible, and it would do so on the last day.
The arithmetic must therefore refuse to answer confidently from numbers nobody
checked — which is the same rule the agent itself lives under.
"""

import pytest

from crucible.perf.quota import QuotaConfig, UnverifiedQuotaError, format_report


@pytest.fixture
def config() -> QuotaConfig:
    """The shipped config, not a fabricated one."""
    return QuotaConfig.load()


class TestUnverifiedLimitsAreRefused:
    """Principle 1, applied to our own planning: nothing claimed that was not measured."""

    def test_planning_against_unverified_limits_raises(self, config):
        """Google stopped publishing free-tier numbers and the trackers disagree.

        A silent default here would put a made-up number into a four-week plan and
        nobody would find out until the benchmark did not fit.
        """
        assert config.verified is False

        with pytest.raises(UnverifiedQuotaError):
            config.plan("gemini-2.5-flash")

    def test_a_hypothetical_can_be_explored_explicitly(self, config):
        """Refusing outright would make the file useless before it is filled in.

        The escape hatch is opt-in and named, so an unverified answer cannot be
        obtained by accident.
        """
        plan = config.plan("gemini-2.5-flash", allow_unverified=True)

        assert plan.calls_per_campaign > 0

    def test_the_report_says_loudly_that_the_numbers_are_unverified(self, config):
        assert "UNVERIFIED" in format_report(config)


class TestCampaignArithmetic:
    def test_calls_per_campaign_follows_the_declared_loop_shape(self, config):
        """max 20 experiments x (diagnose + propose + verdict) + 2 watchdog + 1 summary.

        This is the cost of a campaign that runs all the way to the cap. One that
        meets its SLA at experiment 7 stops there and costs proportionally less --
        the cap is a planning worst case, never a target.
        """
        assert config.calls_per_campaign() == 20 * 3 + 2 + 1

    def test_an_unknown_model_names_the_ones_it_knows(self, config):
        with pytest.raises(KeyError, match="gemini-2.5-flash"):
            config.plan("gpt-4o", allow_unverified=True)


class TestTheRealCeiling:
    """AGENTS.md requires 2-3 s between calls. That is often the binding limit."""

    def test_the_inter_call_delay_can_bind_before_the_provider_rpm_does(self, config):
        """If the delay binds, raising the quota changes nothing — worth knowing
        before anyone spends a day trying to raise the quota."""
        import dataclasses

        slow = dataclasses.replace(config, min_seconds_between_calls=10.0)
        plan = slow.plan("gemini-2.5-flash", allow_unverified=True)

        assert plan.effective_rpm == pytest.approx(6.0)
        assert plan.rpm_bound_by == "inter-call delay"

    def test_key_rotation_multiplies_the_daily_ceiling_but_not_the_rate(self, config):
        """Keys raise RPD because the quota is per key; they do not raise RPM,
        because the loop is sequential and the delay applies to the loop."""
        plan = config.plan("gemini-2.5-flash", allow_unverified=True)

        assert plan.campaigns_per_day_all_keys == pytest.approx(
            plan.campaigns_per_day * config.keys_in_rotation
        )
        assert plan.effective_rpm <= config.limits["gemini-2.5-flash"].rpm
