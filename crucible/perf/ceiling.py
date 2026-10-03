"""Ceiling discovery: stepped load ramp to find the service's capacity knee.

§20 — what is my service actually capable of?

A normal campaign answers "does it meet the SLA and why not?" This answers
"what is the ceiling above the SLA?".  It characterises the service as-is;
nothing is changed and nothing is verified.

**The intent must be declared by the operator, never inferred by the agent**
(§20.1). A ceiling probe deliberately drives a service until it breaks, so it
is started by an explicit operator choice, never by the agent concluding that
pushing harder would be informative — that would be the agent rewriting the
load profile, which §4.4 prohibits.

**A ceiling result is not a verdict on a change** (§20.6). It must never be
compared to experiment manifests as though it were one.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .campaign import Sla, check_environment
from .runner import LoadResult, Scenario

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CeilingStep:
    """One step in the load ramp."""

    users: int
    load: dict[str, Any] = field(default_factory=dict)
    sla_met: bool | None = None
    watchdog_tripped: bool = False
    watchdog_reason: str = ""
    aborted: bool = False


@dataclass
class CeilingResult:
    """The full probe: a knee reported as a pair, never as a single number."""

    run_id: str
    sla: dict[str, Any] = field(default_factory=dict)
    scenario_name: str = ""
    min_users: int = 0
    max_users: int = 0
    step_size: int = 0
    steps: list[CeilingStep] = field(default_factory=list)
    # Last load level where the SLA was met, or None if none passed.
    last_passing_users: int | None = None
    # First load level where the SLA was not met, or None if all passed.
    first_failing_users: int | None = None
    stopped_reason: str = ""
    started_at_epoch_s: float = field(default_factory=time.time)
    finished_at_epoch_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "ceiling",
            "run_id": self.run_id,
            "sla": self.sla,
            "scenario_name": self.scenario_name,
            "min_users": self.min_users,
            "max_users": self.max_users,
            "step_size": self.step_size,
            "steps": [asdict(s) for s in self.steps],
            "last_passing_users": self.last_passing_users,
            "first_failing_users": self.first_failing_users,
            "stopped_reason": self.stopped_reason,
            "started_at_epoch_s": self.started_at_epoch_s,
            "finished_at_epoch_s": self.finished_at_epoch_s,
        }

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.ceiling.json"
        path.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return path

    @property
    def knee(self) -> tuple[int | None, int | None]:
        """The knee as a pair: (last_passing_users, first_failing_users).

        Neither is a single-number answer — the true ceiling lies between them
        and the step size bounds how tightly. §20.4.
        """
        return self.last_passing_users, self.first_failing_users


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


@dataclass
class CeilingProbe:
    """A stepped load ramp that characterises a service's capacity ceiling.

    ``measure`` is the same callable injected into ``Campaign`` — it takes
    ``(scenario, run_id)`` and returns ``(LoadResult, snapshot)``. Injected
    so the probe can be exercised without a live box.

    **Never infer this from campaign results.** §20.1: the intent and bounds
    are operator-declared, not agent-derived. The caller is responsible for
    honouring that: expose the ``--ceiling`` flag through the CLI or UI and
    never invoke this class programmatically from inside the agent loop.
    """

    sla: Sla
    base_scenario: Scenario
    measure: Any
    #: The load level to begin at. Defaults to the base scenario's user count.
    min_users: int = 0
    #: The probe stops here even if the SLA is still met.
    max_users: int = 500
    #: Users added at each step. A step that is not held past warmup measures
    #: JIT, not capacity (§20.3) — hold each step long enough.
    step_size: int = 50
    run_id: str = ""
    results_dir: Path = field(default_factory=lambda: Path("results"))

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = time.strftime("ceiling-%Y%m%d-%H%M%S", time.gmtime())
        if self.min_users <= 0:
            self.min_users = self.base_scenario.users
        # §20.1: a probe on a scenario that suspends error tripwires
        # (push_beyond=True) is a different question — see §6 and §20.
        # The two probe types may look similar but answer different questions;
        # it is the caller's responsibility not to conflate them.

    def run(self) -> CeilingResult:
        """Run the stepped ramp. Blocks until the knee is found or bounds are hit.

        Raises ``CampaignRefused`` on a production target (§19.1) — ceiling
        discovery can be destructive on pre-prod; it must never run on prod.
        """
        check_environment(self.sla)

        result = CeilingResult(
            run_id=self.run_id,
            sla=self.sla.as_dict(),
            scenario_name=self.base_scenario.name,
            min_users=self.min_users,
            max_users=self.max_users,
            step_size=self.step_size,
        )

        users = self.min_users
        step_number = 0
        while users <= self.max_users:
            step_number += 1
            scenario = _at_users(self.base_scenario, users)
            run_id = f"{self.run_id}-step{step_number:02d}"
            load: LoadResult
            load, _snapshot = self.measure(scenario, run_id)

            step = CeilingStep(users=users, load=load.as_load_summary())

            if load.aborted:
                step.aborted = True
                step.watchdog_tripped = True
                step.watchdog_reason = load.abort_reason or "watchdog tripped"
                result.steps.append(step)
                result.stopped_reason = (
                    f"watchdog tripped at {users} users: {load.abort_reason or 'see step'}"
                )
                break

            sla_met = self.sla.met_by(load.p99_ms, load.error_rate_pct)
            step.sla_met = sla_met

            result.steps.append(step)

            if sla_met:
                result.last_passing_users = users
            else:
                # First level that missed — that is the answer (§20.5).
                result.first_failing_users = users
                result.stopped_reason = (
                    f"SLA missed at {users} users "
                    f"(p99 {load.p99_ms:.0f} ms > {self.sla.p99_ms:.0f} ms limit); "
                    f"last passing level was {result.last_passing_users} users"
                )
                break

            users += self.step_size
        else:
            # All steps passed within the declared bounds.
            result.stopped_reason = (
                f"all {step_number} steps passed within the declared max_users={self.max_users}; "
                f"the ceiling is above {result.last_passing_users} users"
            )

        result.finished_at_epoch_s = time.time()
        result.write(self.results_dir)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _at_users(scenario: Scenario, users: int) -> Scenario:
    """Return a copy of *scenario* with the user count overridden."""
    from dataclasses import replace

    return replace(scenario, users=users)
