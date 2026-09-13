"""QuotaSource assertions — new group, 13 September 2026.

DRAFTED FOR REVIEW, not self-approved.

Two places know the rate limits: the gateway, which enforces them, and
config/quota.yaml, which a human maintains. Keeping both is drift, not
redundancy — and it had already produced wrong numbers before anyone compared
them. These tests are mostly about which source wins and what happens when the
winner is unavailable.

No test here touches the network: the gateway payload is injected, so the suite
is the same whether or not glc_v5 happens to be running.
"""

import pytest

from crucible.perf.quota import (
    DeclaredQuota,
    GatewayQuota,
    QuotaConfig,
    QuotaUnavailableError,
    UnverifiedQuotaError,
    quota_source,
)

#: Shaped like the real /v1/providers response, trimmed to what we read.
GATEWAY_PAYLOAD = {
    "providers": ["gemini_1", "gemini_2", "gemini_3", "gemini_4", "gemini_5", "groq"],
    "limits": {
        "gemini": {"rpm": 15, "rpd": 1000, "tpm": 250000, "cooldown": 4},
        "groq": {"rpm": 30, "rpd": 1000, "tpm": 6000, "cooldown": 2},
    },
}


def a_gateway() -> GatewayQuota:
    return GatewayQuota(_payload=dict(GATEWAY_PAYLOAD))


class TestTheSourceIsDeclaredNotSniffed:
    """Auto-detection sounds friendlier and is worse: a gateway that is merely
    slow to start would silently demote the run to stale local numbers, and
    nothing would say so."""

    def test_the_shipped_config_declares_the_gateway(self):
        assert QuotaConfig.load().source == "gateway"

    def test_an_unreachable_gateway_refuses_rather_than_falling_back(self):
        """The bug this prevents: planning a four-week benchmark on numbers
        nobody checked, because the gateway blinked."""
        unreachable = GatewayQuota(base_url="http://127.0.0.1:9", timeout_s=0.5)

        with pytest.raises(QuotaUnavailableError, match="Refusing rather than falling back"):
            unreachable.limits()

    def test_an_unrecognised_source_is_an_error(self):
        import dataclasses

        config = dataclasses.replace(QuotaConfig.load(), source="whatever")
        with pytest.raises(ValueError, match="not recognised"):
            quota_source(config)


class TestGatewayQuota:
    def test_limits_come_back_per_provider(self):
        limits = a_gateway().limits()

        assert limits["gemini"].rpm == 15
        assert limits["gemini"].rpd == 1000

    def test_keys_in_rotation_are_COUNTED_not_declared(self):
        """The error this exists to prevent, and it is not hypothetical:
        quota.yaml said 3 keys when the gateway had 5, so every
        campaigns-per-day figure derived from it was wrong by nearly half."""
        assert a_gateway().keys_in_rotation("gemini") == 5
        assert a_gateway().keys_in_rotation("groq") == 1

    def test_a_provider_with_no_instances_counts_zero(self):
        assert a_gateway().keys_in_rotation("cerebras") == 0

    def test_the_gateways_own_cooldown_is_readable(self):
        """AGENTS.md asks for 2-3 s between calls; the gateway declares 4 for
        gemini. Reading it rather than assuming avoids two components disagreeing
        about the same rate limit."""
        assert a_gateway().cooldown_s("gemini") == 4.0
        assert a_gateway().cooldown_s("nope") is None


class TestDeclaredQuota:
    """For a deployment with no gateway to ask."""

    def test_unverified_declared_limits_are_refused(self, ):
        """This is exactly where hand-copied numbers go stale, and the only
        place nothing else will catch it."""
        config = QuotaConfig.load()
        assert config.verified is False

        with pytest.raises(UnverifiedQuotaError, match="verified: false"):
            DeclaredQuota(config).limits()

    def test_verified_declared_limits_are_returned(self):
        import dataclasses

        config = dataclasses.replace(QuotaConfig.load(), verified=True)
        limits = DeclaredQuota(config).limits()

        assert limits, "a verified declared source should yield its table"
        assert all(hasattr(v, "rpd") for v in limits.values())


def test_the_factory_returns_what_the_config_declared():
    import dataclasses

    config = QuotaConfig.load()
    assert quota_source(dataclasses.replace(config, source="gateway")).name == "gateway"
    assert quota_source(dataclasses.replace(config, source="declared")).name == "declared"
