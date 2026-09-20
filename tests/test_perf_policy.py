"""SLA-as-Policy-memory assertions — new group, week 2.

AGENTS.md non-negotiable 4 / DESIGN.md §4.4.
REVIEWED AND APPROVED by the operator, 20 September 2026.

This is lock 2 of 2 on the goalpost, and it exists because lock 1 has a specific
hole: a protected-path guard stops working the moment configuration moves to a
different path, and NOTHING NOTICES — the guard still passes, on a path nothing
writes to any more. The tests below therefore care about two things:

1. an agent principal cannot write the SLA, at either layer; and
2. the two locks are genuinely independent, so neither one passing implies the
   other is intact.

The second point is what the `TestTheTwoLocksAreIndependent` group is for. A test
suite that only ever exercised both locks together would go green on the day one
of them was quietly removed, which is exactly the failure this design is built to
survive.

Reading is deliberately unrestricted. Principle 3 forbids the agent EDITING the
SLA, not seeing it — an agent that could not read its own objective could not say
whether it had met one.
"""

import pytest

from crucible.core.memory.models import MemoryKind, MemoryScope, Principal
from crucible.core.memory.store import MemoryStore, PermissionDenied
from crucible.perf.applicator import path_is_protected
from crucible.perf.campaign import Sla
from crucible.perf.policy import (
    AGENT_ROLE,
    SlaIsNotAgentWritable,
    SlaPolicy,
    publish_sla,
    recall_sla,
    sla_policy_record,
)

SLA_YAML = """\
name: perflab-db-latency
environment: {name: box-a, kind: pre-prod, target_base_url: 'http://t:8080'}
objective: {endpoint: /api/db, p99_ms: 120, error_rate_pct: 1.0}
noise: {p99_spread_pct: 2.08}
"""

SCOPE = MemoryScope(tenant_id="crucible", project_id="perflab")
OPERATOR = Principal(id="operator-1", role="operator")
AGENT = Principal(id="crucible-agent", role=AGENT_ROLE)


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "slo.yaml"
    path.write_text(SLA_YAML, encoding="utf-8")
    return SlaPolicy.from_sla(Sla.load(path))


@pytest.fixture
def store():
    memory = MemoryStore(":memory:")
    yield memory
    memory.close()


class TestTheSlaIsStoredAsPolicy:
    """The binding. Without it the store's permission guards an empty category."""

    def test_the_record_is_the_policy_kind(self, policy):
        """`MemoryKind.POLICY` is what makes the store's permission check apply.
        A record built with any other kind would be silently agent-writable, and
        every other test here would still pass."""
        record = sla_policy_record(policy, SCOPE, OPERATOR)

        assert record.kind is MemoryKind.POLICY

    def test_an_operator_can_publish_it(self, store, policy):
        record = publish_sla(store, policy, SCOPE, OPERATOR)

        assert store.get(record.id) is not None

    def test_the_stored_thresholds_are_the_ones_a_verdict_will_use(self, store, policy):
        """A Policy record that recorded a different number from the file would be
        worse than no record: two sources of truth for the goalpost."""
        record = publish_sla(store, policy, SCOPE, OPERATOR)

        assert record.metadata["p99_ms"] == 120
        assert record.metadata["noise_p99_spread_pct"] == 2.08

    def test_the_text_is_readable_by_a_person_auditing_the_run(self, policy):
        """Policy records are read by humans asking what the agent was measured
        against, so the text is prose rather than a serialised blob."""
        text = sla_policy_record(policy, SCOPE, OPERATOR).text

        assert "/api/db" in text and "120 ms" in text and "2.08%" in text

    def test_it_can_be_recalled(self, store, policy):
        publish_sla(store, policy, SCOPE, OPERATOR)

        found = recall_sla(store, SCOPE)

        assert len(found) == 1
        assert found[0].metadata["sla_name"] == "perflab-db-latency"


class TestTheAgentCannotWriteIt:
    """The whole point. An agent that can move its own goalpost passes every time."""

    def test_an_agent_principal_is_refused_by_the_policy_layer(self, store, policy):
        with pytest.raises(SlaIsNotAgentWritable, match="only operator or system"):
            publish_sla(store, policy, SCOPE, AGENT)

    def test_the_refusal_names_the_rule_rather_than_just_denying(self, store, policy):
        """An operator reading the traceback should be told which boundary they hit
        and why it exists, not left to infer it from a generic PermissionError."""
        with pytest.raises(SlaIsNotAgentWritable, match="goalpost"):
            publish_sla(store, policy, SCOPE, AGENT)

    def test_nothing_is_written_when_the_agent_is_refused(self, store, policy):
        with pytest.raises(SlaIsNotAgentWritable):
            publish_sla(store, policy, SCOPE, AGENT)

        assert recall_sla(store, SCOPE) == []

    def test_the_store_refuses_it_too_even_if_the_policy_layer_is_bypassed(self, store, policy):
        """Defence in depth WITHIN lock 2. Somebody calling the store directly —
        a future code path, or a refactor that inlines publish_sla — must still be
        refused, or the enforcement would live only in the convenience wrapper."""
        record = sla_policy_record(policy, SCOPE, AGENT)

        with pytest.raises(PermissionDenied, match="only operator/system may write policy"):
            store.write(record)

    def test_a_gateway_principal_cannot_write_policy_either(self, store, policy):
        """Gateway may append audit events; that is not the same authority. Only
        an operator sets the goalpost."""
        gateway = Principal(id="glc", role="gateway")

        with pytest.raises(SlaIsNotAgentWritable):
            publish_sla(store, policy, SCOPE, gateway)

    def test_the_agent_may_still_read_the_sla(self, store, policy):
        """Principle 3 forbids the agent EDITING the SLA, not seeing it. An agent
        that could not read its own objective could not report whether it met one."""
        publish_sla(store, policy, SCOPE, OPERATOR)

        assert len(recall_sla(store, SCOPE)) == 1


class TestTheTwoLocksAreIndependent:
    """Neither lock passing may imply the other is intact."""

    def test_lock_one_is_the_protected_path(self):
        from crucible.perf.profile import TargetProfile

        profile = TargetProfile.named("spring-boot")

        assert path_is_protected("config/slo.yaml", profile.protected_paths) is not None

    def test_lock_two_holds_even_for_an_sla_that_is_not_at_a_protected_path(
        self, store, tmp_path
    ):
        """The exact hole lock 1 has. Move the SLA somewhere the profile does not
        protect and the path guard goes quiet — it still passes, on a path nothing
        writes to. The memory permission does not care where the file lives."""
        from crucible.perf.profile import TargetProfile

        moved = tmp_path / "somewhere" / "else.yaml"
        moved.parent.mkdir()
        moved.write_text(SLA_YAML, encoding="utf-8")
        profile = TargetProfile.named("spring-boot")

        assert path_is_protected(str(moved), profile.protected_paths) is None

        with pytest.raises(SlaIsNotAgentWritable):
            publish_sla(store, SlaPolicy.from_sla(Sla.load(moved)), SCOPE, AGENT)

    def test_the_load_profile_is_covered_by_the_path_lock_too(self):
        """§4.4's other half. Fewer users is not a fix, and changing the profile
        destroys comparability with every earlier campaign."""
        from crucible.perf.profile import TargetProfile

        profile = TargetProfile.named("spring-boot")

        assert path_is_protected("locust/locustfile.py", profile.protected_paths) is not None
