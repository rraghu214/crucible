"""The SLA as Policy memory: the second of the two locks on the goalpost.

``AGENTS.md`` non-negotiable 4 and ``DESIGN.md`` section 4.4 require the SLA and the
load profile to be unwritable by the agent, **enforced twice**:

1. as a **protected path** in the profile's ``protected_paths``, checked by
   :func:`crucible.perf.applicator.path_is_protected`; and
2. as the **Policy memory kind**, which the agent has no write permission for.

The first lock alone is not sufficient, and the reason is specific rather than
theoretical: a file guard stops working the moment configuration moves to a
different path, and *nothing notices*. The guard still passes — on a path nothing
writes to any more. A memory permission cannot be sidestepped that way, because it
is attached to the record's kind rather than to where the bytes happen to live.

The permission itself already exists in :class:`crucible.core.memory.store.MemoryStore`,
which refuses a ``POLICY`` write from any principal whose role is not ``operator``
or ``system``. What this module adds is the *binding*: the SLA is actually stored
as a Policy record, so that refusal is protecting something rather than being a
rule about an empty category.

Neither lock is to be weakened independently of the other. Removing one leaves a
system that still passes its own tests while the boundary it advertises is half
gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.memory.models import MemoryKind, MemoryRecord, MemoryScope, Principal, SourceRef

#: The role a campaign's own agent runs under. Named here so a test can assert on
#: it rather than restating the string, and so there is one place to look when
#: asking "what is the agent allowed to write".
AGENT_ROLE = "agent"

#: Roles the memory store accepts for a Policy write. Mirrors the store's own
#: check; duplicated deliberately so a change to either side breaks a test rather
#: than silently widening the boundary.
POLICY_WRITER_ROLES = ("operator", "system")


class SlaIsNotAgentWritable(PermissionError):
    """Raised when something with an agent principal tries to publish an SLA."""


@dataclass(frozen=True)
class SlaPolicy:
    """The SLA in the form Policy memory stores it.

    Kept as a separate small object rather than reusing :class:`~crucible.perf.campaign.Sla`
    so that the memory layer has no opinion about how an SLA is parsed, and the
    campaign has no opinion about how memory is scoped.
    """

    name: str
    endpoint: str
    p99_ms: float
    error_rate_pct: float
    noise_p99_spread_pct: float
    environment_name: str
    environment_kind: str
    source_uri: str = "file://config/slo.yaml"

    @classmethod
    def from_sla(cls, sla: Any) -> SlaPolicy:
        return cls(
            name=sla.name,
            endpoint=sla.endpoint,
            p99_ms=sla.p99_ms,
            error_rate_pct=sla.error_rate_pct,
            noise_p99_spread_pct=sla.noise_p99_spread_pct,
            environment_name=sla.environment_name,
            environment_kind=sla.environment_kind,
            source_uri=f"file://{sla.source_path}" if sla.source_path else "file://config/slo.yaml",
        )

    def as_text(self) -> str:
        """The record's text. Human-readable because a Policy record is read by
        people auditing what the agent was measured against."""
        return (
            f"SLA {self.name}: {self.endpoint} p99 <= {self.p99_ms:.0f} ms, "
            f"errors <= {self.error_rate_pct:.2f}%, judged against a measured "
            f"noise floor of {self.noise_p99_spread_pct:.2f}% on environment "
            f"{self.environment_name} [{self.environment_kind}]."
        )

    def as_metadata(self) -> dict[str, Any]:
        return {
            "sla_name": self.name,
            "endpoint": self.endpoint,
            "p99_ms": self.p99_ms,
            "error_rate_pct": self.error_rate_pct,
            "noise_p99_spread_pct": self.noise_p99_spread_pct,
            "environment_name": self.environment_name,
            "environment_kind": self.environment_kind,
        }


def sla_policy_record(policy: SlaPolicy, scope: MemoryScope, principal: Principal) -> MemoryRecord:
    """Build the Policy record for an SLA. Does not write it.

    Separated from :func:`publish_sla` so a test can assert on the record's kind
    without needing a store, and so the kind is set in exactly one place —
    ``MemoryKind.POLICY`` is what makes the store's permission check apply, and a
    record built with any other kind would be silently writable by the agent.
    """
    return MemoryRecord(
        kind=MemoryKind.POLICY,
        scope=scope,
        text=policy.as_text(),
        sources=[SourceRef(uri=policy.source_uri, author=principal.id)],
        principal=principal,
        metadata=policy.as_metadata(),
    )


def publish_sla(store: Any, policy: SlaPolicy, scope: MemoryScope, principal: Principal) -> MemoryRecord:
    """Store the SLA as Policy memory, refusing any agent principal.

    The store already refuses this, and the refusal here is *not* redundant: it
    fails with a message that names the design rule rather than a generic
    permission error, and it fails before a record is constructed. An operator
    reading a traceback should be told which boundary they hit and why it exists,
    not left to infer it from ``PermissionDenied``.
    """
    if principal.role not in POLICY_WRITER_ROLES:
        raise SlaIsNotAgentWritable(
            f"principal {principal.id!r} has role {principal.role!r}; only "
            f"{' or '.join(POLICY_WRITER_ROLES)} may write the SLA. An agent that "
            "can move its own goalpost passes every time (DESIGN.md 4.4). This is "
            "the second of two locks: the first is the protected path in the "
            "TargetProfile, and neither is to be weakened without the other."
        )
    return store.write(sla_policy_record(policy, scope, principal))


def recall_sla(store: Any, scope: MemoryScope) -> list[MemoryRecord]:
    """Every Policy record holding an SLA for this scope.

    Reading is unrestricted. The agent *should* know what it is being measured
    against — principle 3 forbids it editing the SLA, not seeing it. An agent that
    could not read its own objective would be unable to say whether it had met it.
    """
    return [
        record
        for record in store.recall("SLA", scope, kinds=[MemoryKind.POLICY])
        if record.metadata.get("sla_name")
    ]
