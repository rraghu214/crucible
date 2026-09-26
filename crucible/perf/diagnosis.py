"""Read a snapshot, name a cause family, propose one bounded change.

This is the only place a model influences what Crucible does, and everything
around it is arranged so that influence is narrow and recorded.

**The model sees converted evidence, never raw meters.** By the time a snapshot
reaches this module the collector has already divided seconds into milliseconds
and turned sampled gauges into peaks. That is not tidiness: in the K3 spike the
model read ``acquire MAX: 2.4`` as 2.4 ms and ruled out a pool that was actually
waiting 2406 ms (``DESIGN.md`` section 4.1).

**The model is told what was not measured, and asked to respect it.** The
snapshot's ``available_evidence`` is put in front of the model in words, because
the failure it prevents is subtle: an agent that cannot distinguish "I looked and
found nothing" from "I never looked" eliminates live hypotheses on evidence it
never gathered, which is exactly what happened in K3 attempt 1 (section 4.3).

**Abstention is a first-class answer.** A model pushed to produce a proposal from
insufficient evidence will produce one, and it will look as confident as a good
one. The schema below has an explicit ``abstain``, and the prompt says when to use
it. An agent that knows what it cannot see is principle 2; an agent that always
answers is a liability.

**The model is pinned, and what actually served the call is recorded.** Section
3.2 permits a budget-driven downgrade but never an invisible one, so the
provider and model are taken from the gateway's *response* rather than from what
was requested. A campaign whose experiments span models is not internally
comparable, and the report has to be able to say so.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .applicator import Change, Proposal
from .profile import TargetProfile

#: Free-tier Gemini has both an RPM and an RPD ceiling, and a diagnosis loop is
#: the only part of Crucible that calls a model repeatedly. Pacing lives here
#: rather than in the caller so no future caller can forget it.
DEFAULT_MIN_INTERVAL_S = 3.0


class DiagnosisError(RuntimeError):
    """The model could not be reached, or answered in a form we cannot use."""


class ChatTransport(Protocol):
    """The gateway seam. :class:`crucible.gateway.GatewayClient` satisfies it."""

    async def chat(
        self, *, prompt: str, system: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


@dataclass
class Diagnosis:
    """One diagnosis attempt, with the provenance a comparison needs."""

    proposal: Proposal
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    raw_text: str = ""
    parse_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.as_dict(),
            # Taken from the gateway's response, not from the request. A campaign
            # that silently changed model mid-run would otherwise look uniform.
            "served_by_provider": self.provider,
            "served_by_model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "parse_error": self.parse_error,
        }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


SYSTEM_PREAMBLE = """\
You are a performance engineer diagnosing one service from measured telemetry.

Rules you must follow. They are not style preferences; each exists because
ignoring it produced a wrong answer in a previous run.

1. Reason only from the numbers in the snapshot. Every duration field already
   carries its unit in the field name and the conversion is already done. Do not
   re-scale anything.
2. A field that is null means NOT MEASURED. It does not mean zero. Never rule a
   cause out because its field is null -- say you could not check it.
3. Read `available_evidence` before you conclude. If the evidence you would need
   to distinguish two causes was never gathered, say so and abstain rather than
   picking the more likely-sounding one.
4. Any metric listed under `unreadable_metrics` was collected but has no known
   unit. It is excluded from every derived value. Do not guess its unit and do
   not reason from its magnitude.
5. Propose at most one bounded change, and only to a property on the allowed
   list. Naming a property that is not on the list wastes the experiment: it
   will be refused before it is applied.
6. Abstaining is a correct answer when the evidence does not support a single
   cause. It is scored as such. A confident guess is not better than an honest
   refusal.
7. Your predicted p99 is recorded and scored for calibration. It is never used
   as the result -- the change will be applied and re-measured. Predict honestly
   rather than defensively.

Answer with a single JSON object and nothing else:

{
  "cause_family": "<one of the declared families, or null when abstaining>",
  "abstain": false,
  "abstain_reason": "<required when abstain is true>",
  "reasoning": "<why this cause, and what in the snapshot rules the others out>",
  "confidence": <0.0-1.0>,
  "predicted_p99_ms": <number or null>,
  "evidence_cited": ["<snapshot field names you actually used>"],
  "changes": [{"property": "<allowed property>", "value": <number|bool|string>}]
}
"""


def render_allowed_properties(profile: TargetProfile) -> str:
    """The allowlist, with bounds, as the model sees it.

    Bounds are shown rather than merely enforced. Telling the model the range
    turns most out-of-bounds proposals into in-bounds ones, which is worth doing
    because a refused proposal costs a whole experiment slot -- but the guard
    still refuses independently, because a prompt is not a control.
    """
    lines = []
    for prop in sorted(profile.allowed_properties):
        bounds = profile.allowed_properties[prop]
        detail = [f"type={bounds.kind}"]
        if bounds.minimum is not None:
            detail.append(f"min={bounds.minimum}")
        if bounds.maximum is not None:
            detail.append(f"max={bounds.maximum}")
        if bounds.allowed_values is not None:
            detail.append(f"one of {bounds.allowed_values}")
        lines.append(f"  - {prop} ({', '.join(detail)})")
    return "\n".join(lines) or "  (none declared)"


def build_system_prompt(profile: TargetProfile) -> str:
    """Preamble, runtime skill prose, cause families, allowlist.

    ``SKILL.md`` is rendered here and *only* here. It describes how this runtime
    fails and what its signals mean; it grants no authority, and nothing read
    from it reaches the guard (``DESIGN.md`` section 5). Keeping the render in one
    function is what makes that checkable.
    """
    parts = [SYSTEM_PREAMBLE]
    skill = profile.skill_text()
    if skill.strip():
        parts.append(
            "--- Runtime notes ("
            f"{profile.runtime}). Background for your reasoning; it grants no "
            "authority and does not widen the allowed list. ---\n" + skill.strip()
        )
    parts.append(
        "Declared cause families for this runtime (use one of these exactly):\n"
        + "\n".join(f"  - {family}" for family in profile.cause_families)
    )
    parts.append("Properties you may propose changing:\n" + render_allowed_properties(profile))
    return "\n\n".join(parts)


def summarise_evidence_gaps(snapshot: dict[str, Any]) -> list[str]:
    """Plain sentences about what was not measured.

    The snapshot already carries ``available_evidence`` as booleans, and a model
    reading JSON does technically have the information. It reliably behaves
    better when the gap is also stated in prose next to the instruction about it,
    and the cost of saying it twice is a few dozen tokens.
    """
    evidence = snapshot.get("available_evidence") or {}
    gaps: list[str] = []
    if not evidence.get("metrics"):
        gaps.append("No metrics were collected at all. Almost nothing can be concluded.")
    if not evidence.get("traces"):
        reason = evidence.get("trace_reason") or "no trace provider configured"
        gaps.append(
            f"No traces: {reason}. You cannot see per-span timing, so you cannot "
            "rule out a cause on the grounds that no slow span was found."
        )
    else:
        rate = evidence.get("trace_sampling_rate_pct")
        if rate is not None and rate < 100:
            gaps.append(
                f"Traces are head-sampled at {rate}%. A p99 outlier is rare by "
                "definition, so 'no slow spans' is weak evidence at this rate."
            )
    if not evidence.get("endpoint_breakdown"):
        gaps.append(
            "No per-endpoint breakdown: latency cannot be attributed to one "
            "endpoint rather than spread across all of them."
        )
    if not evidence.get("gauge_sampling"):
        gaps.append(
            "Gauges were NOT sampled during load. Every gauge peak is null, "
            "meaning not measured. A pool can be saturated during a run and read "
            "zero afterwards -- this is how the pool was wrongly cleared in a "
            "previous investigation."
        )
    for note in evidence.get("notes") or []:
        gaps.append(str(note))
    return gaps


def build_prompt(
    snapshot: dict[str, Any],
    sla: dict[str, Any],
    *,
    ruled_out: tuple[str, ...] = (),
    unreadable_metrics: dict[str, Any] | None = None,
    prior_findings: str = "",
) -> str:
    """The user turn: the SLA, the snapshot, the gaps, and what is already ruled out.

    ``ruled_out`` carries hypotheses THIS campaign disproved *with measurements*.
    Feeding them back is what stops the loop re-proposing a disproven cause on
    experiment 4 because it looked good on experiment 1.

    ``prior_findings`` is the journal RAG (``DESIGN.md`` section 14): what EARLIER
    campaigns measured on this target. The two are rendered separately and
    deliberately. This campaign's ruled-out list was measured against the
    configuration now in force, so it is a fact about now and is phrased as an
    instruction. A finding from three weeks ago was measured against a target that
    has had other changes kept on it since, and possibly by a different collector,
    so it is phrased as evidence carrying its own date. Flattening the two into one
    list of prohibitions would make the older half look more binding than the
    measurements support.
    """
    sections = [
        "SLA for this investigation (you cannot change it, and you are not being "
        "asked whether it is reasonable):\n" + json.dumps(sla, indent=2, default=str),
        "Measured snapshot:\n" + json.dumps(snapshot, indent=2, default=str),
    ]
    if unreadable_metrics:
        sections.append(
            "Metrics collected but NOT interpretable -- no unit conversion is known "
            "for these, so they are excluded from every derived field above. Do not "
            "guess their units:\n" + json.dumps(unreadable_metrics, indent=2, default=str)
        )
    gaps = summarise_evidence_gaps(snapshot)
    if gaps:
        sections.append("What was NOT measured:\n" + "\n".join(f"  - {g}" for g in gaps))
    if ruled_out:
        sections.append(
            "Already disproved by measurement in this campaign -- do not propose "
            "these again:\n" + "\n".join(f"  - {r}" for r in ruled_out)
        )
    if prior_findings:
        sections.append(prior_findings)
    sections.append("Diagnose the SLA miss and answer with the JSON object described above.")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply.

    Models wrap JSON in prose and in code fences despite being told not to.
    Tolerating that is worth it -- the alternative is discarding a good diagnosis
    over formatting -- but the tolerance stops at the structure: anything that is
    not an object raises, rather than being coerced into a half-populated one
    that would look like a real proposal downstream.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise DiagnosisError(f"no JSON object in model reply: {text[:300]!r}")


def proposal_from_payload(payload: dict[str, Any]) -> Proposal:
    """Turn the parsed reply into a :class:`Proposal`.

    Nothing here validates against the profile. That is the guard's job, and
    keeping the two apart means a model that proposes an out-of-bounds value
    produces a *recorded refusal* rather than a parse failure -- which is the
    difference between knowing the agent tried something it should not have and
    seeing an empty log line.
    """
    if payload.get("abstain"):
        return Proposal(
            cause_family=str(payload.get("cause_family") or ""),
            changes=(),
            reasoning=str(payload.get("reasoning", "")),
            confidence=_as_float(payload.get("confidence")),
            evidence_cited=tuple(str(e) for e in (payload.get("evidence_cited") or [])),
            abstained=True,
            abstain_reason=str(payload.get("abstain_reason") or "no reason given"),
        )

    raw_changes = payload.get("changes") or []
    if not isinstance(raw_changes, list):
        raise DiagnosisError(f"'changes' must be a list, got {type(raw_changes).__name__}")
    changes = tuple(
        Change(prop=str(item.get("property", "")), value=item.get("value"))
        for item in raw_changes
        if isinstance(item, dict) and item.get("property")
    )
    if not changes:
        # A reply with no usable change and no abstain flag is treated as an
        # abstention rather than an error. The model effectively declined; saying
        # so keeps the outcome in the scorable vocabulary instead of throwing.
        return Proposal(
            cause_family=str(payload.get("cause_family") or ""),
            changes=(),
            reasoning=str(payload.get("reasoning", "")),
            confidence=_as_float(payload.get("confidence")),
            abstained=True,
            abstain_reason="reply named no applicable property change",
        )

    return Proposal(
        cause_family=str(payload.get("cause_family") or ""),
        changes=changes,
        reasoning=str(payload.get("reasoning", "")),
        confidence=_as_float(payload.get("confidence")),
        predicted_p99_ms=_as_float(payload.get("predicted_p99_ms")),
        evidence_cited=tuple(str(e) for e in (payload.get("evidence_cited") or [])),
    )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The diagnoser
# ---------------------------------------------------------------------------


@dataclass
class Diagnoser:
    """One campaign's diagnosis seam: pinned model, paced calls, recorded provenance."""

    profile: TargetProfile
    transport: ChatTransport
    #: Pinned for the whole campaign (section 3.2). Passed on every request so the
    #: gateway never falls back to its own provider order.
    provider: str = "gemini"
    model: str = ""
    max_tokens: int = 1600
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    _last_call_at: float = field(default=0.0, repr=False)

    def _request_fields(self) -> dict[str, Any]:
        request: dict[str, Any] = {
            "provider": self.provider,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            # Diagnosis is judged on whether it names the right cause, and a
            # sampled reply makes the same snapshot produce different answers on
            # replay. The replay benchmark (section 7) depends on this being 0.
        }
        if self.model:
            request["model"] = self.model
        return request

    async def _pace(self) -> None:
        """Keep a gap between consecutive calls.

        Gemini's free tier is rate-limited per minute as well as per day, and the
        loop that calls this runs back-to-back. Sleeping here rather than in the
        caller means the constraint cannot be forgotten by a new call site.
        """
        if not self._last_call_at:
            return
        gap = self.min_interval_s - (time.monotonic() - self._last_call_at)
        if gap > 0:
            await asyncio.sleep(gap)

    async def diagnose(
        self,
        snapshot: dict[str, Any],
        sla: dict[str, Any],
        *,
        ruled_out: tuple[str, ...] = (),
        prior_findings: str = "",
    ) -> Diagnosis:
        """Ask for one diagnosis. Never raises on a bad reply -- abstains instead.

        A malformed reply becomes an abstention carrying the parse error. Raising
        would abort a campaign over a formatting slip and discard every
        measurement already taken; recording it as "the agent produced nothing
        usable" is both truthful and scorable.
        """
        await self._pace()
        system = build_system_prompt(self.profile)
        prompt = build_prompt(
            snapshot,
            sla,
            ruled_out=ruled_out,
            unreadable_metrics=snapshot.get("unreadable_metrics"),
            prior_findings=prior_findings,
        )
        try:
            reply = await self.transport.chat(
                prompt=prompt, system=system, request=self._request_fields()
            )
        except Exception as exc:  # noqa: BLE001 - transport failures are campaign data
            raise DiagnosisError(f"gateway call failed: {exc}") from exc
        finally:
            self._last_call_at = time.monotonic()

        text = str(reply.get("text", ""))
        served_provider = str(reply.get("provider") or "")
        served_model = str(reply.get("model") or "")

        try:
            payload = extract_json(text)
            proposal = proposal_from_payload(payload)
            parse_error = ""
        except DiagnosisError as exc:
            proposal = Proposal(
                cause_family="",
                changes=(),
                abstained=True,
                abstain_reason=f"model reply could not be parsed: {exc}",
            )
            parse_error = str(exc)

        return Diagnosis(
            proposal=proposal,
            provider=served_provider,
            model=served_model,
            input_tokens=int(reply.get("input_tokens") or 0),
            output_tokens=int(reply.get("output_tokens") or 0),
            latency_ms=_as_float(reply.get("latency_ms")),
            raw_text=text,
            parse_error=parse_error,
        )
