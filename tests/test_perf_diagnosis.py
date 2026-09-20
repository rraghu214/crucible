"""Diagnosis assertions — new group, week 2. DESIGN.md §3.2, §4.1-4.3, §5.

REVIEWED AND APPROVED by the operator, 20 September 2026.

The model is the one component here that cannot be made deterministic, so these
tests do not assert that it diagnoses correctly. They assert the things around it
that CAN be guaranteed:

- what it is told (the gaps in the evidence, in words, not just as booleans);
- what it is not told (nothing raw, no invented authority from SKILL.md);
- what happens to whatever it says (parsed defensively, never trusted into the
  guard, abstention preserved rather than coerced into a guess);
- what is recorded (the provider and model that ACTUALLY served the call, read
  off the response rather than the request).

That last one is §3.2. A budget-driven downgrade is permitted but must never be
invisible, and the only way to know which model answered is to read the reply.

The parse tests are deliberately unglamorous. A campaign that aborted because a
model wrapped its JSON in a code fence would discard every measurement already
taken, so tolerance there is worth real effort — but the tolerance stops at
structure, never at meaning.
"""

import asyncio

import pytest

from crucible.perf.diagnosis import (
    Diagnoser,
    DiagnosisError,
    build_prompt,
    build_system_prompt,
    extract_json,
    proposal_from_payload,
    render_allowed_properties,
    summarise_evidence_gaps,
)
from crucible.perf.profile import TargetProfile

SLA = {"endpoint": "/api/db", "p99_ms": 120}


def a_profile(**over) -> TargetProfile:
    base = {
        "name": "spring-boot",
        "runtime": "jvm",
        "cause_families": ["connection_pool_exhaustion", "gc_pressure"],
        "allowed_properties": {
            "spring.datasource.hikari.maximum-pool-size": {"type": "int", "min": 1, "max": 100},
            "perflab.cache.enabled": {"type": "bool"},
        },
    }
    return TargetProfile.from_mapping(base | over)


def a_snapshot(**over) -> dict:
    base = {
        "collector_version": "1.1.0",
        "hikaricp": {
            "acquire_mean_ms": 1003.4,
            "pending_peak_connections": 43,
            "active_peak_connections": 2,
        },
        "available_evidence": {
            "metrics": True,
            "traces": False,
            "trace_reason": "no trace provider configured",
            "trace_sampling_rate_pct": None,
            "endpoint_breakdown": True,
            "gauge_sampling": True,
            "notes": [],
        },
    }
    return base | over


class TestWhatTheModelIsTold:
    """The prompt is a control surface, even though it is not a guard."""

    def test_the_allowed_properties_are_shown_with_their_bounds(self):
        """Showing the range turns most out-of-bounds proposals into in-bounds
        ones, and a refused proposal costs a whole experiment slot. The guard
        still refuses independently — a prompt is not a control."""
        text = render_allowed_properties(a_profile())

        assert "spring.datasource.hikari.maximum-pool-size" in text
        assert "min=1" in text and "max=100" in text

    def test_the_cause_families_come_from_the_profile_not_a_constant(self):
        """`gc` is a JVM concept. A hardcoded list would make the agent propose
        impossible hypotheses on CPython and miss real ones (§5)."""
        system = build_system_prompt(a_profile(cause_families=["gil_contention"]))

        assert "gil_contention" in system
        assert "connection_pool_exhaustion" not in system

    def test_skill_prose_is_rendered_but_marked_as_granting_nothing(self, tmp_path):
        """§5. SKILL.md changes how the model approaches the work and grants no
        authority. The prompt says so explicitly, because a model reading runtime
        notes that mention a property could otherwise infer permission."""
        (tmp_path / "x.SKILL.md").write_text("Hikari pools fail by queueing.", encoding="utf-8")
        (tmp_path / "x.yaml").write_text(
            "name: x\nruntime: jvm\ncause_families: [connection_pool_exhaustion]\n"
            "skill_file: x.SKILL.md\n",
            encoding="utf-8",
        )

        system = build_system_prompt(TargetProfile.load(tmp_path / "x.yaml"))

        assert "Hikari pools fail by queueing." in system
        assert "grants no" in system

    def test_the_prompt_states_that_null_means_not_measured(self):
        system = build_system_prompt(a_profile())

        assert "null means NOT MEASURED" in system
        assert "does not mean zero" in system

    def test_the_prompt_says_abstaining_is_a_correct_answer(self):
        """A model pushed to produce a proposal from insufficient evidence will
        produce one, and it will look as confident as a good one."""
        system = build_system_prompt(a_profile())

        assert "Abstaining is a correct answer" in system

    def test_the_prompt_says_the_prediction_is_not_the_result(self):
        """§4.5. Told plainly so the model predicts honestly instead of
        defensively — the prediction is a calibration signal, and a hedged one is
        worth less than an accurate one."""
        # Whitespace-normalised: the preamble is hard-wrapped, and a test that
        # broke when a line was rewrapped would be testing the wrap, not the rule.
        system = " ".join(build_system_prompt(a_profile()).split())

        assert "never used as the result" in system


class TestTheAgentIsToldWhatItCouldNotSee:
    """§4.3. 'I looked and found nothing' vs 'I never looked'."""

    def test_absent_traces_are_stated_with_their_reason(self):
        gaps = summarise_evidence_gaps(a_snapshot())

        assert any("No traces" in g for g in gaps)
        assert any("cannot rule out a cause" in g for g in gaps)

    def test_head_sampling_below_100_percent_is_called_out(self):
        """Most production tracing runs at 1-10%, and a p99 outlier is rare by
        definition — so 'no slow spans' can be false on a fully instrumented
        deployment. Without this the agent treats sampled absence as proof."""
        snapshot = a_snapshot(
            available_evidence={
                "metrics": True, "traces": True, "trace_sampling_rate_pct": 5,
                "endpoint_breakdown": True, "gauge_sampling": True, "notes": [],
            }
        )

        gaps = summarise_evidence_gaps(snapshot)

        assert any("head-sampled at 5%" in g for g in gaps)

    def test_unsampled_gauges_are_flagged_with_the_failure_they_caused(self):
        """This is the K3 failure in one sentence. `pending` reads 0 once load
        drains; during the run it was 43."""
        snapshot = a_snapshot(
            available_evidence={
                "metrics": True, "traces": False, "endpoint_breakdown": True,
                "gauge_sampling": False, "notes": [],
            }
        )

        gaps = summarise_evidence_gaps(snapshot)

        assert any("NOT sampled during load" in g for g in gaps)
        assert any("null, meaning not measured" in g for g in gaps)

    def test_fully_evidenced_snapshots_produce_no_spurious_gaps(self):
        """The negative case: crying wolf about evidence that IS present would
        push the model toward abstaining on good data."""
        snapshot = a_snapshot(
            available_evidence={
                "metrics": True, "traces": True, "trace_sampling_rate_pct": 100,
                "endpoint_breakdown": True, "gauge_sampling": True, "notes": [],
            }
        )

        assert summarise_evidence_gaps(snapshot) == []

    def test_ruled_out_hypotheses_are_fed_back(self):
        """Stops the loop re-proposing on experiment 4 something disproved on
        experiment 1. Within a campaign this does the job the journal RAG does
        across campaigns (§14)."""
        prompt = build_prompt(a_snapshot(), SLA, ruled_out=("raise the pool to 20 -> WORSE",))

        assert "do not propose" in prompt
        assert "raise the pool to 20 -> WORSE" in prompt

    def test_unreadable_metrics_are_shown_and_marked_do_not_guess(self):
        """§4.8. A metric with no known unit is carried, excluded from derived
        values, and explicitly not to be guessed at — reading seconds as
        milliseconds is how a saturated pool looked healthy."""
        prompt = build_prompt(
            a_snapshot(), SLA,
            unreadable_metrics={"custom.pool.wait": {"value": 2.4, "unit": "unknown"}},
        )

        assert "NOT interpretable" in prompt
        assert "Do not" in prompt and "guess their units" in prompt

    def test_the_sla_is_shown_as_something_the_model_cannot_change(self):
        prompt = build_prompt(a_snapshot(), SLA)

        assert "you cannot change it" in prompt


class TestParsingWhatComesBack:
    """Tolerant about formatting, strict about structure."""

    def test_a_bare_json_object_parses(self):
        assert extract_json('{"cause_family": "gc_pressure"}')["cause_family"] == "gc_pressure"

    def test_a_fenced_json_block_parses(self):
        text = 'Here you go:\n```json\n{"cause_family": "gc_pressure"}\n```\nHope that helps.'

        assert extract_json(text)["cause_family"] == "gc_pressure"

    def test_json_surrounded_by_prose_parses(self):
        text = 'I think the pool is the issue. {"cause_family": "gc_pressure"} That is my answer.'

        assert extract_json(text)["cause_family"] == "gc_pressure"

    def test_a_reply_with_no_json_raises_rather_than_returning_a_shell(self):
        """A half-populated object would look like a real proposal downstream.
        Better to fail here, where it becomes a recorded abstention."""
        with pytest.raises(DiagnosisError, match="no JSON object"):
            extract_json("I would raise the pool size to 20.")

    def test_a_single_object_wrapped_in_an_array_is_unwrapped(self):
        """Tolerated deliberately: the model returned one proposal and put
        brackets round it. Refusing would discard a usable diagnosis over
        punctuation."""
        assert extract_json('[{"cause_family": "gc_pressure"}]')["cause_family"] == "gc_pressure"

    def test_two_objects_in_one_reply_raise_rather_than_silently_picking_one(self):
        """This is the case that matters. Quietly taking the first of two
        proposals would apply a change the model did not settle on, and nothing
        downstream could tell that a second one had been discarded."""
        with pytest.raises(DiagnosisError):
            extract_json('[{"cause_family": "gc_pressure"}, {"cause_family": "cache_miss"}]')

    def test_an_explicit_abstention_survives_parsing(self):
        """Abstention must not be coerced into a low-confidence guess. It is a
        distinct, scorable outcome (principle 2)."""
        proposal = proposal_from_payload(
            {"abstain": True, "abstain_reason": "gauges were never sampled"}
        )

        assert proposal.abstained
        assert "never sampled" in proposal.abstain_reason
        assert proposal.changes == ()

    def test_a_reply_with_no_usable_change_becomes_an_abstention_not_an_error(self):
        """The model effectively declined. Recording that keeps the outcome in
        the scorable vocabulary instead of throwing an exception that would abort
        a campaign mid-run."""
        proposal = proposal_from_payload({"cause_family": "gc_pressure", "changes": []})

        assert proposal.abstained
        assert "named no applicable property change" in proposal.abstain_reason

    def test_a_normal_proposal_carries_its_prediction_and_evidence(self):
        proposal = proposal_from_payload({
            "cause_family": "connection_pool_exhaustion",
            "confidence": 0.8,
            "predicted_p99_ms": 70,
            "evidence_cited": ["hikaricp.pending_peak_connections"],
            "changes": [{"property": "spring.datasource.hikari.maximum-pool-size", "value": 20}],
        })

        assert proposal.changes[0].prop == "spring.datasource.hikari.maximum-pool-size"
        assert proposal.predicted_p99_ms == 70.0
        assert proposal.evidence_cited == ("hikaricp.pending_peak_connections",)

    def test_an_out_of_bounds_value_parses_rather_than_being_rejected_here(self):
        """Validation belongs to the guard. Keeping them apart means a model that
        proposes something forbidden produces a RECORDED REFUSAL, which is
        evidence about the agent, rather than a parse failure, which is not."""
        proposal = proposal_from_payload({
            "cause_family": "connection_pool_exhaustion",
            "changes": [{"property": "server.port", "value": 999999}],
        })

        assert proposal.changes[0].prop == "server.port"

    def test_a_non_list_changes_field_is_an_error(self):
        with pytest.raises(DiagnosisError, match="must be a list"):
            proposal_from_payload({"cause_family": "x", "changes": "raise the pool"})

    def test_a_non_numeric_confidence_becomes_none_rather_than_crashing(self):
        proposal = proposal_from_payload({
            "cause_family": "x", "confidence": "high",
            "changes": [{"property": "p", "value": 1}],
        })

        assert proposal.confidence is None


class TestProvenanceAndPinning:
    """§3.2. The downgrade is permitted; the invisibility is not."""

    def test_the_provider_and_model_recorded_are_the_ones_that_answered(self):
        """Read off the RESPONSE, not the request. A campaign that silently fell
        back to a weaker model would otherwise look uniform in the report."""
        transport = _FakeTransport(
            text='{"cause_family":"connection_pool_exhaustion","changes":[{"property":"spring.datasource.hikari.maximum-pool-size","value":20}]}',
            provider="groq", model="llama-3.3-70b",
        )
        diagnoser = Diagnoser(
            profile=a_profile(), transport=transport, provider="gemini",
            model="gemini-2.5-flash", min_interval_s=0,
        )

        result = asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

        assert transport.request["provider"] == "gemini"
        assert transport.request["model"] == "gemini-2.5-flash"
        assert result.provider == "groq"
        assert result.model == "llama-3.3-70b"
        assert result.as_dict()["served_by_model"] == "llama-3.3-70b"

    def test_the_model_is_pinned_on_every_request(self):
        """Not set once and hoped for. An unset model lets the gateway apply its
        own provider order, which may put a different model first."""
        transport = _FakeTransport(text='{"abstain": true}')
        diagnoser = Diagnoser(
            profile=a_profile(), transport=transport, provider="gemini",
            model="gemini-2.5-flash", min_interval_s=0,
        )

        asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

        assert transport.request["model"] == "gemini-2.5-flash"

    def test_temperature_is_zero_so_replay_is_reproducible(self):
        """§7's replay benchmark scores the same snapshot hundreds of times. A
        sampled reply would make the same fixture produce different answers, and
        the benchmark would measure sampling noise."""
        transport = _FakeTransport(text='{"abstain": true}')
        diagnoser = Diagnoser(profile=a_profile(), transport=transport, min_interval_s=0)

        asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

        assert transport.request["temperature"] == 0

    def test_an_unparseable_reply_becomes_an_abstention_carrying_the_error(self):
        """Raising would abort the campaign over a formatting slip and discard
        every measurement already taken."""
        transport = _FakeTransport(text="I'd raise the pool size.")
        diagnoser = Diagnoser(profile=a_profile(), transport=transport, min_interval_s=0)

        result = asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

        assert result.proposal.abstained
        assert result.parse_error
        assert "could not be parsed" in result.proposal.abstain_reason

    def test_a_transport_failure_raises_rather_than_looking_like_an_abstention(self):
        """A gateway that is down and a model that declined are different facts.
        Collapsing them would let an outage be scored as good judgement."""
        diagnoser = Diagnoser(
            profile=a_profile(), transport=_ExplodingTransport(), min_interval_s=0
        )

        with pytest.raises(DiagnosisError, match="gateway call failed"):
            asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

    def test_consecutive_calls_are_paced(self):
        """Gemini's free tier is limited per minute as well as per day, and the
        loop calls this back to back. Pacing lives in the diagnoser so a new call
        site cannot forget it."""
        transport = _FakeTransport(text='{"abstain": true}')
        diagnoser = Diagnoser(profile=a_profile(), transport=transport, min_interval_s=0.05)

        async def two_calls():
            await diagnoser.diagnose(a_snapshot(), SLA)
            import time as _t
            started = _t.monotonic()
            await diagnoser.diagnose(a_snapshot(), SLA)
            return _t.monotonic() - started

        assert asyncio.run(two_calls()) >= 0.04

    def test_the_first_call_is_not_delayed(self):
        """Pacing between calls, not before the first one. A campaign should not
        pay the interval for a single diagnosis."""
        transport = _FakeTransport(text='{"abstain": true}')
        diagnoser = Diagnoser(profile=a_profile(), transport=transport, min_interval_s=30.0)

        import time as _t
        started = _t.monotonic()
        asyncio.run(diagnoser.diagnose(a_snapshot(), SLA))

        assert _t.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self, text, provider="gemini", model="gemini-2.5-flash"):
        self._text = text
        self._provider = provider
        self._model = model
        self.request = None
        self.prompt = None
        self.system = None

    async def chat(self, *, prompt, system, request=None):
        self.prompt, self.system, self.request = prompt, system, dict(request or {})
        return {
            "text": self._text,
            "provider": self._provider,
            "model": self._model,
            "input_tokens": 900,
            "output_tokens": 120,
            "latency_ms": 800,
        }


class _ExplodingTransport:
    async def chat(self, *, prompt, system, request=None):
        raise ConnectionError("gateway refused the connection")
