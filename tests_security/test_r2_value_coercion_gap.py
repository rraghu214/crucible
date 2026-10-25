"""R2 (model controls the proposed value): the value the guard CHECKS is not the
value that gets WRITTEN to the config file.

DRAFTED by Claude Code, 25 October 2026. NOT YET REVIEWED by the operator.
Finding: docs/THREAT_MODEL.md §6 / revision-1 H4 -- RE-VERIFIED 25 Oct, STILL
LIVE. This is a reproduction: it asserts the gap exists today.

Mechanism (no network, no workspace needed -- the gap is in two pure functions):
  - Bounds.violation(v) coerces v to the bound's type and checks the COERCED
    value against min/max (crucible/perf/profile.py:72-88). ``20.9`` coerces to
    ``int(20.9) == 20`` and passes an int bound of 1..50.
  - Applicator._write then calls set_property(text, prop, change.value) with the
    RAW value (crucible/perf/applicator.py:552), and set_property renders it with
    _format_value = str(value) (applicator.py:271-275). So ``20.9`` is written
    verbatim, and ``True`` is written as ``true`` on an int property.

Consequence: the operator approves/guards one value and the file receives a
different one -- a type mismatch (restart/parse failure = DoS) at least, and a
whitespace/newline carrier at worst. INV-6: the approved parameters are not the
applied parameters.

Proposed fix (for when this flips to a refusal test): write ``bounds.coerce(
value)``, reject non-finite floats and bool-for-int, and reject any value whose
string form contains ``\\r \\n \\x0b \\x0c \\x85``.

Review question for the operator:
  - Both shipped profiles use only int/bool properties, so today the worst case
    is a failed restart (DoS), not key injection. Do you want the applicator to
    write the coerced value regardless, so "checked == applied" holds by
    construction for any future string-typed property too?
"""
from __future__ import annotations

from crucible.perf.applicator import set_property
from crucible.perf.profile import Bounds


def test_float_passes_an_int_bound_but_is_written_unrounded():
    bound = Bounds(minimum=1, maximum=50, kind="int")
    # The guard coerces 20.9 -> int(20.9) == 20, which is in range: it passes.
    assert bound.violation(20.9) is None
    # But the file receives "20.9", not the "20" the guard reasoned about.
    written = set_property("pool=2", "pool", 20.9)
    assert "pool=20.9" in written, "if the applicator now writes the coerced value, H4 is fixed"
    assert "pool=20" not in written.replace("pool=20.9", "")


def test_bool_passes_an_int_bound_and_is_written_as_a_word():
    bound = Bounds(minimum=0, maximum=100, kind="int")
    # int(True) == 1, in range -> passes.
    assert bound.violation(True) is None
    # Written as the Java boolean literal on a numeric property.
    written = set_property("pool=2", "pool", True)
    assert "pool=true" in written


def test_whitespace_wrapped_value_passes_and_is_written_raw():
    bound = Bounds(minimum=1, maximum=50, kind="int")
    hostile = " 20\n"
    # int(" 20\n") == 20 -> passes the bound.
    assert bound.violation(hostile) is None
    written = set_property("pool=2", "pool", hostile)
    # The newline the guard never saw is carried into the properties file.
    assert "pool= 20\n" in written or "\n" in written.split("pool=", 1)[1][:6]
