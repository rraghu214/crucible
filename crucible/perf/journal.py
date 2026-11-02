"""The journal RAG: what this target has already been asked, and what happened.

``DESIGN.md`` section 14 gives this component one job -- past manifests, queried
at diagnosis, so the agent does not re-propose a hypothesis a previous campaign
already disproved -- and one strong instruction about how to do it:

    Dense retrieval is weak on exact tokens, and journals are full of
    identifiers like ``hikaricp.connections.pending`` -- filter on structured
    fields first, use vectors for narrative only.

**So this is a structured query over manifests on disk, and it embeds nothing.**
That is not a shortcut taken for time. The question a diagnosis actually needs
answered is "has `spring.datasource.hikari.maximum-pool-size` been tried on this
profile and scenario, and what did it measure?", and every term in it is an exact
token living in a named field. A vector search over that returns the manifests
whose *prose* resembles the query, which is a different question with a similar
shape -- the worst kind of wrong answer, because it looks like an answer.

Reading manifests from disk is also the precedent section 4.6 already sets for
the scorer, and it buys the same property: no embedder, no Ollama on a small
cloud box, no ``embedder_id`` to go stale, and a component whose whole behaviour
is reproducible from files a human can read.

**The narrative half is declared and deliberately not built.** Where it lands, it
lands behind :func:`narrative_retrieval_refusal`, which encodes section 14's rule
rather than leaving it to be rediscovered: a corpus embedded under one model is
exactly as stale to another as a snapshot is to a changed collector, and matching
dimensionality is *not* the same vector space -- two 768-dimension models return
nearest neighbours that are noise wearing the shape of an answer.

**Prior findings are advisory; this campaign's ruled-out list is not.** The
distinction is load-bearing and the two are rendered into the prompt separately.
A hypothesis disproved twenty minutes ago in this run was measured against the
configuration now in force. A hypothesis disproved three weeks ago was measured
against a target that has since had other changes kept on it, and may have been
measured by a different collector entirely. The first is a fact about now; the
second is a fact about then, and the agent is told which it is holding, with the
date and the commit, rather than being handed a flat list of prohibitions.

The assertions doc's own review note asks whether "never re-propose a disproven
hypothesis" is always right, or whether new evidence could justify a second look.
This module takes the position that it is not always right, and that the way to
be honest about it is to supply the evidence and the date rather than to enforce
a ban the agent cannot see the reasoning behind.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .collector import COLLECTOR_VERSION
from .verdicts import ABORTED, DISPROVING_VERDICTS, IMPROVED, NOT_MEASURED

#: How many prior findings are rendered into one prompt. A cap rather than
#: everything: the journal grows without bound, the prompt does not, and a
#: hundred prior findings would crowd out the snapshot they are meant to be read
#: against. Most recent first, because a finding about the current configuration
#: is worth more than one about a configuration three changes ago.
DEFAULT_LIMIT = 8

#: Beyond this, a finding is old enough that the target may have changed under
#: it. Not a filter -- the age is *reported*, and the agent decides. Thirty days
#: is a judgement call and is flagged for review.
STALE_AFTER_DAYS = 30

class JournalError(RuntimeError):
    """The journal could not be read. Never raised for an empty journal."""


@dataclass(frozen=True)
class PriorFinding:
    """One thing a previous campaign tried on this target, and what it measured.

    Carries the provenance a reader needs to decide how much it is worth: which
    run, when, against which commit, judged against which noise floor, and by
    which collector. A finding without those is an assertion; with them it is
    evidence.
    """

    run_id: str
    experiment: int
    cause_family: str
    #: ``prop -> value`` as proposed. The exact tokens the structured filter
    #: matches on, and the reason this is not a vector search.
    changes: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    verdict_reason: str = ""
    kept: bool = False
    margin_over_noise: float | None = None
    deployed_commit: str = ""
    profile: str = ""
    scenario: str = ""
    collector_version: str = ""
    at_epoch_s: float = 0.0
    refused_by_guard: bool = False

    @property
    def properties(self) -> tuple[str, ...]:
        return tuple(sorted(self.changes))

    def age_days(self, now: float | None = None) -> float:
        if not self.at_epoch_s:
            return 0.0
        return max(0.0, ((now if now is not None else time.time()) - self.at_epoch_s) / 86400.0)

    def stale_collector(self) -> bool:
        """Whether this finding's numbers were computed by different arithmetic.

        Not a reason to hide it -- "this was tried" survives a collector change
        even when "it measured 93 ms" does not. It IS a reason to say so, which
        is what :func:`render_for_prompt` does.
        """
        return bool(self.collector_version) and self.collector_version != COLLECTOR_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        changes = ", ".join(f"{prop}={value!r}" for prop, value in sorted(self.changes.items()))
        return f"{self.cause_family}: {changes or 'no change recorded'} -> {self.verdict}"


def _finding_from_experiment(
    experiment: dict[str, Any], campaign: dict[str, Any]
) -> PriorFinding | None:
    """One manifest experiment as a finding, or ``None`` when it carries no lesson.

    An experiment with no proposal never tried anything, so there is nothing for
    a later campaign to learn from it.
    """
    proposal = experiment.get("proposal") or {}
    changes = {
        c.get("prop"): c.get("value")
        for c in (proposal.get("changes") or [])
        if c.get("prop")
    }
    if not changes and not experiment.get("cause_family"):
        return None
    return PriorFinding(
        run_id=str(experiment.get("run_id") or campaign.get("run_id") or ""),
        experiment=int(experiment.get("experiment") or 0),
        cause_family=str(experiment.get("cause_family") or ""),
        changes=changes,
        verdict=str(experiment.get("verdict") or ""),
        verdict_reason=str(experiment.get("verdict_reason") or ""),
        kept=bool(experiment.get("kept")),
        margin_over_noise=experiment.get("margin_over_noise"),
        deployed_commit=str(experiment.get("deployed_commit") or ""),
        profile=str(campaign.get("profile") or ""),
        scenario=str(campaign.get("scenario") or ""),
        collector_version=str(
            experiment.get("collector_version") or campaign.get("collector_version") or ""
        ),
        at_epoch_s=float(
            experiment.get("started_at_epoch_s") or campaign.get("started_at_epoch_s") or 0.0
        ),
        refused_by_guard=bool(experiment.get("refused_by_guard")),
    )


@dataclass
class JournalIndex:
    """Every manifest on disk, as findings that can be filtered by exact token.

    Loaded once and held. The journal is small -- one file per campaign, twenty
    experiments at most in each -- so an index is a list, and pretending
    otherwise would be building for a scale this will not reach before it has
    taught us what queries it actually needs.
    """

    findings: list[PriorFinding] = field(default_factory=list)
    #: Manifests that could not be parsed, as ``(path, reason)``. Named rather
    #: than counted, and never fatal: one corrupt file must not cost a campaign
    #: the whole of its history.
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, directory: str | Path) -> JournalIndex:
        """Read every ``*.json`` campaign manifest in a directory.

        A missing directory is an empty journal, not an error: the first campaign
        on a new target has no history, and refusing to start would be absurd.
        """
        index = cls()
        root = Path(directory)
        if not root.exists():
            return index
        for path in sorted(root.glob("*.json")):
            try:
                campaign = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                index.unreadable.append((str(path), str(exc)))
                continue
            if not isinstance(campaign, dict) or "experiments" not in campaign:
                # A replay result or a score file living in the same directory.
                # Skipped silently: it is not a malformed manifest, it is not a
                # manifest.
                continue
            for experiment in campaign.get("experiments") or []:
                finding = _finding_from_experiment(experiment, campaign)
                if finding is not None:
                    index.findings.append(finding)
        return index

    # -- the structured query --------------------------------------------

    def prior_findings(
        self,
        *,
        profile: str = "",
        scenario: str = "",
        properties: tuple[str, ...] | list[str] = (),
        cause_families: tuple[str, ...] | list[str] = (),
        exclude_run_id: str = "",
        disproving_only: bool = False,
        limit: int = DEFAULT_LIMIT,
        now: float | None = None,
    ) -> list[PriorFinding]:
        """Findings matching an exact-token filter, most recent first.

        Every argument narrows on a field, never on a similarity. ``profile`` is
        the one that matters most and is easiest to forget: a finding about a
        FastAPI target says nothing about a JVM one, and feeding it across would
        have the agent rule out a cause on evidence from a different runtime --
        the section 4.3 failure, arriving through the history rather than through
        the snapshot.

        ``exclude_run_id`` keeps the current campaign out of its own history. The
        campaign already feeds its own experiments back through ``ruled_out``, and
        counting them twice would present one measurement as two.
        """
        matched = []
        wanted_props = {str(p) for p in properties}
        wanted_causes = {str(c) for c in cause_families}
        for finding in self.findings:
            if profile and finding.profile != profile:
                continue
            if scenario and finding.scenario != scenario:
                continue
            if exclude_run_id and finding.run_id == exclude_run_id:
                continue
            if wanted_props and not (wanted_props & set(finding.properties)):
                continue
            if wanted_causes and finding.cause_family not in wanted_causes:
                continue
            if disproving_only and finding.verdict not in DISPROVING_VERDICTS:
                continue
            matched.append(finding)
        matched.sort(key=lambda f: f.at_epoch_s, reverse=True)
        return matched[: max(0, limit)]

    def properties_tried(self, *, profile: str = "", scenario: str = "") -> dict[str, list[str]]:
        """``property -> the verdicts it has produced``, for a report or a human.

        Answers "what has ever been tried here" without a model and without a
        prompt, which is the question an operator asks first when a campaign
        proposes something that feels familiar.
        """
        tried: dict[str, list[str]] = {}
        for finding in self.findings:
            if profile and finding.profile != profile:
                continue
            if scenario and finding.scenario != scenario:
                continue
            for prop in finding.properties:
                tried.setdefault(prop, []).append(finding.verdict or NOT_MEASURED)
        return {prop: verdicts for prop, verdicts in sorted(tried.items())}


def render_for_prompt(
    findings: list[PriorFinding],
    *,
    now: float | None = None,
    stale_after_days: float = STALE_AFTER_DAYS,
) -> str:
    """Prior findings as prompt text: evidence with its provenance, not orders.

    Three things travel with every line, and each exists to stop a specific
    misreading:

    - **the verdict and why**, so a finding reads as a measurement rather than as
      a prohibition somebody typed;
    - **the age**, because a change disproved three weeks ago was measured
      against a target that has had other changes kept on it since, and the agent
      is the only thing in the loop that can weigh that;
    - **a collector mismatch**, where one exists. "This was tried" survives a
      collector change; "it measured 93 ms" does not, and conflating the two
      would score the model against arithmetic that no longer exists.

    An empty list renders as an empty string rather than as "nothing was found".
    The two are not the same claim, and the caller decides how to say so: a
    campaign with no history and a campaign whose history was unreadable both
    have no findings, and only the caller knows which it is.
    """
    if not findings:
        return ""
    lines = []
    for finding in findings:
        age = finding.age_days(now)
        parts = [f"  - {finding.summary()}"]
        if finding.verdict_reason:
            parts.append(f"      measured: {finding.verdict_reason}")
        provenance = [f"run {finding.run_id or 'unknown'} experiment {finding.experiment}"]
        if age >= 1:
            provenance.append(f"{age:.0f} days ago")
        if finding.deployed_commit:
            provenance.append(f"commit {finding.deployed_commit[:12]}")
        parts.append(f"      ({', '.join(provenance)})")
        if age >= stale_after_days:
            parts.append(
                "      NOTE: older than "
                f"{stale_after_days:.0f} days. Other changes may have been kept on "
                "this target since, so this may no longer describe it."
            )
        if finding.stale_collector():
            parts.append(
                f"      NOTE: measured by collector {finding.collector_version}, this "
                f"process is {COLLECTOR_VERSION}. That this was TRIED still holds; the "
                "numbers were computed by different arithmetic and do not."
            )
        lines.append("\n".join(parts))
    return (
        "Previously measured on this target, in earlier campaigns. This is evidence, "
        "not a prohibition: weigh it, and say so in your reasoning if you propose "
        "something it speaks to.\n" + "\n".join(lines)
    )


def narrative_retrieval_refusal(corpus_embedder_id: str, query_embedder_id: str) -> str | None:
    """Why a narrative (vector) query is refused, or ``None`` when it may run.

    Section 14's rule, encoded before anything depends on it. Nothing in this
    module embeds yet -- the structured filter above answers the question the
    journal is actually for -- but the rule is the part that is expensive to
    rediscover, and it is cheap to write down now:

        Two different models at 768 dimensions produce vectors that are simply
        incomparable, and querying one corpus with the other returns nearest
        neighbours that are noise wearing the shape of an answer -- no error, no
        warning, just quietly wrong retrieval, in the component whose whole job
        is to stop the agent re-proposing a disproven hypothesis.

    So matching dimensionality is not a defence, and neither is a matching model
    id: ``outputDimensionality`` is a request parameter, so ``gemini-embedding-001``
    at 768 and at 3072 are both truthfully that model. The whole id must match, and
    a mismatch names what needs re-indexing rather than degrading quietly.
    """
    if not corpus_embedder_id or not query_embedder_id:
        return (
            "narrative retrieval needs an embedder_id on both the corpus and the "
            "query. An unlabelled vector cannot be shown to be comparable with "
            "anything, and section 14 requires that it be shown rather than assumed."
        )
    if corpus_embedder_id != query_embedder_id:
        return (
            f"the journal corpus was embedded by {corpus_embedder_id!r} and this query "
            f"by {query_embedder_id!r}. These are different vector spaces even at the "
            "same dimensionality, and querying across them returns plausible nonsense "
            f"rather than an error. Re-index the journal under {query_embedder_id!r}."
        )
    return None


def journal_summary(index: JournalIndex, *, profile: str = "") -> dict[str, Any]:
    """Counts for a report header. Pure arithmetic -- no model, no prompt."""
    findings = [f for f in index.findings if not profile or f.profile == profile]
    verdicts: dict[str, int] = {}
    for finding in findings:
        verdicts[finding.verdict or NOT_MEASURED] = verdicts.get(finding.verdict or NOT_MEASURED, 0) + 1
    return {
        "findings": len(findings),
        "campaigns": len({f.run_id for f in findings if f.run_id}),
        "verdicts": dict(sorted(verdicts.items())),
        "properties_tried": len({p for f in findings for p in f.properties}),
        "guard_refusals": sum(1 for f in findings if f.refused_by_guard),
        "stale_collector": sum(1 for f in findings if f.stale_collector()),
        "unreadable_manifests": len(index.unreadable),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "DISPROVING_VERDICTS",
    "STALE_AFTER_DAYS",
    "ABORTED",
    "IMPROVED",
    "JournalError",
    "JournalIndex",
    "PriorFinding",
    "journal_summary",
    "narrative_retrieval_refusal",
    "render_for_prompt",
]
