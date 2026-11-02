"""The verdict vocabulary, in one place because more than one module needs it.

Extracted from :mod:`crucible.perf.campaign` on 26 September 2026, when the
journal RAG needed to ask "did this change work?" and importing the campaign to
find out created a cycle -- the campaign reads the journal at diagnosis, and the
journal reads the campaign's vocabulary to interpret a manifest.

The alternative was for the journal to declare its own copies of five short
strings. That is the failure ``AGENTS.md`` records for a duplicated assertions
doc, in its cheapest and most tempting form: the strings would agree on the day
they were written and drift the first time a verdict is added, and nothing would
fail -- a journal comparing against a verdict the campaign no longer emits simply
finds nothing, and reports an empty history rather than an error.

**Strings rather than an enum**, which is the choice the campaign made
originally and is kept deliberately: a manifest read years from now should be
legible without importing the package that wrote it. The scorer, the report and
a human with ``jq`` all read these out of JSON.

:mod:`crucible.perf.campaign` re-exports every name here, so existing imports
keep working and there is one obvious place to look for either.
"""

from __future__ import annotations

#: The change beat the environment's measured noise floor, downward.
IMPROVED = "IMPROVED"

#: The change beat the noise floor, upward. Reverted.
WORSE = "WORSE"

#: The change moved p99 by less than run-to-run variation. Not a result: on the
#: Oracle box three identical runs spread 2.08%, so a 1.5% "improvement" is
#: variation wearing a result's clothes (``DESIGN.md`` section 4.7).
INCONCLUSIVE = "INCONCLUSIVE"

#: One side of the comparison was never measured. Distinct from every verdict
#: above because principle 1 says verified and unverified must never look
#: identical in a results table.
NOT_MEASURED = "NOT_MEASURED"

#: The watchdog stopped the run mid-window (section 6). Distinct from
#: :data:`NOT_MEASURED`, which means nothing was ever measured: here a
#: measurement was under way and was invalidated, and the tripwire that
#: invalidated it is part of the record. The scorer treats both as
#: ``UNREACHABLE`` -- neither is a failure the agent can be graded on -- but
#: they are different facts and the manifest says which one happened.
ABORTED = "ABORTED"

#: Every verdict a manifest may carry. A reader iterating this rather than
#: hardcoding a list gets the new one for free when one is added.
ALL_VERDICTS = (IMPROVED, WORSE, INCONCLUSIVE, NOT_MEASURED, ABORTED)

#: Verdicts meaning "this was measured and it did not work". The journal feeds
#: these back to diagnosis: each one cost an experiment to learn, and learning
#: it twice is the most expensive avoidable thing a campaign can do.
#:
#: :data:`NOT_MEASURED` and :data:`ABORTED` are deliberately NOT here. Neither
#: says the change was wrong -- they say nobody found out -- and treating
#: "we never measured it" as "it was disproved" would have the agent rule out a
#: live hypothesis on evidence it never gathered, which is exactly the K3
#: attempt-1 failure (``DESIGN.md`` section 4.3).
DISPROVING_VERDICTS = (WORSE, INCONCLUSIVE)

__all__ = [
    "ABORTED",
    "ALL_VERDICTS",
    "DISPROVING_VERDICTS",
    "IMPROVED",
    "INCONCLUSIVE",
    "NOT_MEASURED",
    "WORSE",
]
