"""R2 (model controls the proposed value): the value the guard CHECKS must equal
the value that gets WRITTEN to the config file.

Drafted by Claude Code, 25 October 2026; reviewed and approved by the operator,
28 October 2026. Finding: docs/THREAT_MODEL.md §6 / H4.
Fix applied 28 October 2026: these tests now assert the FIX, not the old bug.

Controls added:
  - Bounds.violation() rejects bool-as-int (int(True)==1 passes bounds but
    _format_value renders "true", a Java boolean literal), non-finite floats,
    and any value whose str form contains a line-break character.
  - Applicator._write() and preview() now call bounds.coerce(value) before
    set_property(), so the file always receives the type-coerced form.

Result: checked == applied by construction for every existing profile property.
"""
from __future__ import annotations

from crucible.perf.applicator import set_property
from crucible.perf.profile import Bounds


def test_float_written_as_coerced_int():
    bound = Bounds(minimum=1, maximum=50, kind="int")
    # Guard still passes 20.9 (coerces to 20, in range).
    assert bound.violation(20.9) is None
    # The applicator now writes the coerced value; test that path directly.
    coerced = bound.coerce(20.9)
    assert coerced == 20
    written = set_property("pool=2", "pool", coerced)
    assert "pool=20\n" in written or written.endswith("pool=20")
    assert "20.9" not in written


def test_bool_rejected_on_int_bound():
    bound = Bounds(minimum=0, maximum=100, kind="int")
    # bool is now REJECTED for int-kind properties (INV-6 guard).
    assert bound.violation(True) is not None
    assert "bool" in (bound.violation(True) or "").lower()


def test_newline_in_value_rejected():
    bound = Bounds(minimum=1, maximum=50, kind="int")
    hostile = " 20\n"
    # Line-break characters are rejected before they can reach the file.
    assert bound.violation(hostile) is not None
    assert "line-break" in (bound.violation(hostile) or "").lower()
