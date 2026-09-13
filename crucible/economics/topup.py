"""Ask a human for more budget before falling back to a cheaper model.

``DESIGN.md`` §3.2 permits a budget-driven downgrade: when spend pressure crosses
the threshold the controller drops a rung rather than halting, because on free
tier there is no cheaper rung of the *same* model to fall back to, and a campaign
that finishes on a weaker model is worth more than one that stops.

But a downgrade has a real cost: experiments diagnosed by different models are not
comparable. So before taking it, offer the operator the chance to avoid it. If
they raise the ceiling, the campaign continues on one model and stays internally
comparable. If they do not answer, the downgrade happens anyway and is disclosed.

Three rules shape this module:

**The default never blocks.** :class:`NoTopUp` declines instantly, so an
unattended run behaves exactly as it did before this existed. Blocking has to be
opted into, not inherited.

**The wait is bounded, and it is wall clock.** ``DESIGN.md`` §7 is explicit that
wall clock is the real budget, not money — a five-minute pause is a real cost, so
the deadline is enforced here rather than trusted to the responder.

**It is asked at most once per run.** A campaign makes many calls; asking on every
downgrade decision could stall it for five minutes repeatedly. One ask, one
answer, remembered for the rest of the run. :class:`TopUpCoordinator` owns that
memory so no caller has to.

Every outcome — granted, declined, timed out, never asked — is returned as a
:class:`TopUpOutcome` so it can be recorded on the manifest. "Nobody was asked"
and "somebody declined" are different facts about a campaign.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

#: Five minutes, per the operator decision recorded on 13 September 2026.
DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class TopUpRequest:
    """What the operator is being asked to decide, with the context to decide it."""

    run_id: str
    principal: str
    current_total: float
    spent: float
    pressure: float
    requested_tier: str
    fallback_tier: str
    currency: str = "USD"
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: Correlates an answer with THIS question. A channel that is also carrying a
    #: conversation must not treat the next thing the operator happens to say as
    #: the answer -- the reply has to name the request it is answering.
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    asked_at_epoch_s: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    #: Which time this is in the run: 1 for the first ask, 2 after a top-up that
    #: has since been spent, and so on.
    attempt: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def asked_at(self) -> datetime:
        return datetime.fromtimestamp(self.asked_at_epoch_s, tz=timezone.utc)

    @property
    def deadline(self) -> datetime:
        return datetime.fromtimestamp(self.asked_at_epoch_s + self.timeout_s, tz=timezone.utc)

    def summary(self) -> str:
        """What a human sees. States the clock, the deadline and the request id.

        The times are explicit because a prompt that says "you have 5 minutes"
        without saying 5 minutes from WHEN is not transparency -- the operator
        may read it long after it was sent. The request id is quoted so the
        answer can name the question it is answering.
        """
        fmt = "%H:%M:%S UTC"
        lines = [
            f"[budget-top-up {self.request_id}] attempt {self.attempt} for run "
            f"{self.run_id}: {self.spent:.4f} of {self.current_total:.4f} "
            f"{self.currency} spent ({self.pressure:.0%}).",
            f"Asked at {self.asked_at.strftime(fmt)}; you have "
            f"{self.timeout_s / 60:.0f} minute(s) to reply, until "
            f"{self.deadline.strftime(fmt)}.",
            f"Without more budget, {self.requested_tier} falls back to "
            f"{self.fallback_tier}. That changes the model mid-run, which makes "
            "earlier experiments non-comparable with later ones.",
            f"Reply referencing {self.request_id} with a new ceiling to avoid it, "
            "or do nothing and the fallback proceeds.",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class TopUpOutcome:
    """What came back. ``state`` distinguishes the four genuinely different cases."""

    #: "granted" | "declined" | "timed_out" | "not_asked"
    state: str
    new_total: float | None = None
    responder: str = ""
    waited_s: float = 0.0
    reason: str = ""

    @property
    def granted(self) -> bool:
        return self.state == "granted" and self.new_total is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"granted": self.granted}


class BudgetTopUp(Protocol):
    """Somewhere a human can be asked. The UI and the CLI both satisfy this."""

    def ask(self, request: TopUpRequest) -> TopUpOutcome: ...


class NoTopUp:
    """Decline instantly. The default, and what an unattended run uses.

    Deliberately not a timeout of zero: "nobody was asked" is a different fact
    from "somebody was asked and did not answer", and the manifest should be able
    to tell them apart.
    """

    def ask(self, request: TopUpRequest) -> TopUpOutcome:  # noqa: ARG002
        return TopUpOutcome(
            state="not_asked",
            reason="no top-up channel configured; downgrade proceeds",
        )


class CallbackTopUp:
    """Ask via a callable, and enforce the deadline here rather than trusting it.

    The callback returns the new ceiling, or ``None`` to decline. It runs on a
    daemon thread so a callback that never returns cannot wedge the run: the
    deadline expires, the downgrade proceeds, and the outcome says ``timed_out``.
    A late answer is discarded rather than applied to a campaign that has already
    moved on, which would otherwise change the budget halfway through an
    experiment nobody was watching.
    """

    def __init__(
        self,
        callback: Callable[[TopUpRequest], float | None],
        *,
        responder: str = "operator",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._callback = callback
        self._responder = responder
        self._timeout_s = float(timeout_s)

    def ask(self, request: TopUpRequest) -> TopUpOutcome:
        result: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                result["value"] = self._callback(request)
            except Exception as exc:  # a broken channel must not kill the run
                result["error"] = exc
            finally:
                done.set()

        started = time.monotonic()
        threading.Thread(target=run, name="crucible-budget-topup", daemon=True).start()
        answered = done.wait(self._timeout_s)
        waited = time.monotonic() - started

        if not answered:
            return TopUpOutcome(
                state="timed_out", responder=self._responder, waited_s=waited,
                reason=f"no answer within {self._timeout_s:.0f}s; downgrade proceeds",
            )
        if "error" in result:
            return TopUpOutcome(
                state="declined", responder=self._responder, waited_s=waited,
                reason=f"top-up channel failed: {result['error']!r}",
            )

        value = result.get("value")
        if value is None:
            return TopUpOutcome(
                state="declined", responder=self._responder, waited_s=waited,
                reason="operator declined to raise the ceiling",
            )
        new_total = float(value)
        if new_total <= request.current_total:
            return TopUpOutcome(
                state="declined", responder=self._responder, waited_s=waited,
                reason=(
                    f"offered ceiling {new_total:.4f} is not above the current "
                    f"{request.current_total:.4f}; treated as a decline"
                ),
            )
        return TopUpOutcome(
            state="granted", new_total=new_total, responder=self._responder,
            waited_s=waited, reason=f"ceiling raised to {new_total:.4f}",
        )


@dataclass
class TopUpCoordinator:
    """Asks once per *funding round*, remembers the answer, keeps the audit trail.

    Not once per run. The distinction matters and it is the whole reason this
    type exists:

    **After a decline or a timeout, it stays quiet for the rest of the run.**
    Spend pressure stays above the threshold once it crosses it, so asking per
    call would re-trigger on every subsequent decision; at five minutes each that
    turns a campaign into a series of pauses. An operator who said no once should
    not be asked again for the same reason.

    **After a GRANT, it re-arms.** A raised ceiling is eventually spent too, and a
    campaign that was worth funding at 80% of the first budget is usually worth
    funding at 80% of the second. Refusing to ask again would silently force the
    downgrade the top-up existed to avoid. So a grant resets the gate and the next
    request carries ``attempt: 2``, and so on until the run completes or the
    operator declines.
    """

    channel: BudgetTopUp = field(default_factory=NoTopUp)
    outcomes: list[TopUpOutcome] = field(default_factory=list)
    #: Set by a decline or a timeout, never by a grant. Once closed, stays closed.
    _closed: bool = False
    _attempts: int = 0

    @property
    def already_asked(self) -> bool:
        """Whether asking again would be pointless -- declined, or timed out."""
        return self._closed

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def granted_total(self) -> float | None:
        """The most recent ceiling an operator actually approved."""
        for outcome in reversed(self.outcomes):
            if outcome.granted:
                return outcome.new_total
        return None

    def request(self, req: TopUpRequest) -> TopUpOutcome:
        """Ask, unless this run has already been told no."""
        if self._closed:
            return TopUpOutcome(
                state="not_asked",
                reason=(
                    "the operator already declined or did not answer this run; "
                    "not asking again for the same reason"
                ),
            )
        self._attempts += 1
        # The attempt number travels with the question so the operator can see
        # this is the second or third time, not a duplicate of the first.
        import dataclasses

        outcome = self.channel.ask(dataclasses.replace(req, attempt=self._attempts))
        self.outcomes.append(outcome)
        if not outcome.granted:
            self._closed = True
        return outcome

    def as_manifest_entries(self) -> list[dict[str, Any]]:
        """For the manifest. A run nobody was asked about is not a run that declined."""
        return [o.as_dict() for o in self.outcomes]
