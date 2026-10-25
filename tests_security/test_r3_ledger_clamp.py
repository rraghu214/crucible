"""R3 (compromised gateway / poisoned usage numbers): the cost ledger cannot be
driven DOWN by negative token counts.

DRAFTED by Claude Code, 25 October 2026. NOT YET REVIEWED by the operator.
Finding: docs/THREAT_MODEL.md §6.B leak 10 -- RE-VERIFIED 25 Oct and found
MITIGATED, so this is a REGRESSION GUARD of the good behaviour, not a
reproduction of a live exploit.

Why it is mitigated: Pricing.cost() wraps every token count in
``max(0, int(...))`` (crucible/economics/pricing.py:86-91), so a negative
input_tokens contributes 0 to cost, and RunBudget.charge() only ever does
``self.spent += cost`` with cost >= 0. Spend cannot decrease.

What this test pins: a negative/huge token count from the gateway response
neither lowers ``spent`` nor makes cost negative. If a future refactor removes
the clamp, leak 10 reopens and this test fails.

Review question for the operator:
  - Leak 10 also asked that the gateway's RAW usage be recorded on the manifest
    (so a poisoned count is auditable, not just clamped). Is the raw usage
    captured somewhere a reader can see it? If not, that half of the finding is
    still open even though the clamp holds.
"""
from __future__ import annotations

from crucible.economics.budget import RunBudget
from crucible.economics.pricing import Pricing

PRICING = Pricing.from_mapping({
    "currency": "USD", "unit_tokens": 1_000_000,
    "default": {"input": 100.0, "output": 100.0},
    "models": {"m": {"input": 1.0, "output": 1.0}},
})


def test_negative_tokens_do_not_reduce_spend():
    budget = RunBudget(total=1.0)
    budget.charge(node_id="n1", role="r", tier="t", pricing=PRICING, model="m",
                  input_tokens=1000, output_tokens=1000)
    after_first = budget.spent
    assert after_first > 0

    # A poisoned "refund": a hostile usage report with negative counts.
    budget.charge(node_id="n2", role="r", tier="t", pricing=PRICING, model="m",
                  input_tokens=-10_000_000, output_tokens=-10_000_000)

    assert budget.spent >= after_first, "negative token counts must not lower spend (leak 10)"
    assert budget.charges[-1].cost >= 0, "a charge cost must never be negative"


def test_cost_is_never_negative():
    assert PRICING.cost("m", input_tokens=-5, output_tokens=-5) == 0.0
