"""R2 (text-only attacker): the chat reply sanitiser neutralises script in model
output before it reaches the operator's browser (where the control token lives).

Drafted by Claude Code, 25 October 2026; reviewed and approved by the operator, 28 October 2026.
Finding: docs/THREAT_MODEL.md §6.C H8 -- RE-VERIFIED 25 Oct and found PARTLY
MITIGATED since the doc was written. This file pins the control that now exists;
it is a REGRESSION GUARD, not a live exploit.

What changed since 22 Oct: perf_ui._safe_reply() now html-escapes the whole
reply and re-allows only seven attribute-free tags
(<strong><code><em><br><ul><li><p>). A document or diagnosis that tries to
inject <script> or an onerror handler through the chat is therefore rendered
inert. The matching prompt line tells the model it may use only those tags.

What is NOT covered by _safe_reply (still open under H8): the dashboard inserts
other server-provided fields straight into innerHTML -- the approval-gate
summary/cause_family (crucible-ui.html ~2719) and history run ids (~2375).
Those values originate from manifests and the model's proposal and are NOT
passed through _safe_reply. A live DOM test for those is the follow-up; this
file covers the chat path only.

Review questions for the operator:
  - Confirm the seven-tag allowlist is the intended set, and that no reply path
    bypasses _safe_reply.
  - Should the approval-gate and history innerHTML sinks also route through an
    escaper? (That is the remaining half of H8.)
"""
from __future__ import annotations

import pytest

from crucible.ui.perf_ui import _safe_reply

NEUTRALISED = [
    "<img src=x onerror=alert(1)>",
    "<script>steal(document.cookie)</script>",
    "<a href=javascript:evil() onclick=go>click</a>",
    "<iframe src=//evil></iframe>",
    "<svg/onload=alert(1)>",
]


@pytest.mark.parametrize("payload", NEUTRALISED)
def test_script_and_attributes_are_escaped(payload):
    """The dangerous markup must not survive as live HTML."""
    out = _safe_reply(payload)
    low = out.lower()
    assert "<script" not in low
    assert "onerror" not in low or "&lt;" in out  # the tag carrying it is escaped
    assert "<iframe" not in low
    assert "<svg" not in low
    assert "javascript:" not in low or "&lt;a" in out  # inside an escaped tag only


def test_the_seven_allowed_tags_survive():
    out = _safe_reply("<strong>a</strong> <code>b</code> <em>c</em><br><ul><li>d</li></ul><p>e</p>")
    for tag in ("<strong>", "<code>", "<em>", "<br>", "<ul>", "<li>", "<p>"):
        assert tag in out, f"allowed tag {tag} was stripped"


def test_an_allowed_tag_with_an_attribute_is_not_reopened():
    """Only attribute-free forms are re-allowed, so an injected attribute on an
    otherwise-allowed tag stays escaped."""
    out = _safe_reply('<p onclick="evil()">x</p>')
    assert "onclick" not in out.lower() or "&lt;p" in out
