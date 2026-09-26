"""The FastAPI target profile — week 3 deliverable 3. Extends GROUP 9 of
`docs/CRUCIBLE_TEST_ASSERTIONS.md` (the profile / authority boundary) to a
second runtime.

DRAFTED by Claude Code. NOT YET REVIEWED by the operator.

The point of a second profile is not the properties it lists -- it is proving
that `crucible/perf/profile.py` needed no code change to gain one, and that
the guard, the applicator and the diagnoser generalise to a runtime whose
cause families don't overlap with the JVM's. `test_perf_profile.py` already
covers spring-boot; the tests below are the FastAPI-specific half, focused on
the one requirement called out explicitly for this deliverable: the agent
must never propose a JVM GC hypothesis against a FastAPI target, because
`gil_contention` replaces `gc_pressure` for CPython.

There is no live FastAPI PerfLab target yet (see the PROVISIONAL note at the
top of `config/profiles/fastapi.yaml`), so nothing here can be checked
against a running box the way spring-boot.yaml's metric names were verified
against Box A. What IS checked is everything static: the profile loads, the
guard enforces its bounds, and the six cause families are exactly the ones
this session specified -- no `gc`, no `thread_pool`.
"""

from __future__ import annotations

import pytest

from crucible.perf.applicator import Proposal, guard_proposal
from crucible.perf.profile import DEFAULT_PROFILE_DIR, TargetProfile


@pytest.fixture
def fastapi() -> TargetProfile:
    """The real shipped profile, not a fabricated one -- same reasoning as
    the spring_boot fixture in test_perf_profile.py: a test against a
    hand-built profile would pass while the file the campaign actually loads
    was broken."""
    return TargetProfile.named("fastapi")


class TestTheProfileLoadsFromDisk:
    def test_the_shipped_profile_loads(self, fastapi):
        assert fastapi.name == "fastapi"
        assert fastapi.runtime == "cpython"
        assert fastapi.source_path is not None
        assert fastapi.source_path.parent == DEFAULT_PROFILE_DIR

    def test_config_file_is_declared_not_hardcoded(self, fastapi):
        assert fastapi.config_file == ".env"


class TestCauseFamiliesAreExactlyTheSixDeclared:
    """The explicit requirement for this deliverable: gil_contention replaces
    gc for Python runtimes, and thread_pool_saturation has no analogue here
    either -- FastAPI's concurrency unit is the ASGI worker process, not a
    pooled thread within one."""

    def test_the_six_families_are_present(self, fastapi):
        assert set(fastapi.cause_families) == {
            "pool",
            "query",
            "downstream",
            "application_code",
            "gil_contention",
            "worker_saturation",
        }

    def test_no_jvm_families_leaked_in(self, fastapi):
        """A hardcoded list would make the agent propose impossible
        hypotheses on this runtime (DESIGN.md section 5) -- gc and
        thread_pool_saturation are exactly the two the session named as
        forbidden here."""
        assert "gc" not in fastapi.cause_families
        assert "gc_pressure" not in fastapi.cause_families
        assert "thread_pool" not in fastapi.cause_families
        assert "thread_pool_saturation" not in fastapi.cause_families

    def test_a_gc_pressure_proposal_is_now_flagged_rather_than_refused(self, fastapi):
        """CHANGED 26 September 2026 (DESIGN.md 5), and this is the case that LOST
        something. Read the trade before agreeing to it.

        Previously the guard refused `gc_pressure` on a CPython profile
        categorically: a model trained mostly on Spring Boot snapshots could not
        carry the JVM hypothesis across. It now can, and the proposal is recorded
        as novel instead.

        **What is not lost.** Naming a cause never granted authority. The change
        this proposal actually makes is `DB_POOL_SIZE`, which is on the FastAPI
        allowlist and would have been permitted under any label -- so the old
        refusal was blocking the *word*, not the action. A proposal reaching for
        a real JVM knob is still refused, by the property check, which is the
        test below.

        **What is lost.** A mislabelled diagnosis on a runtime where that cause
        cannot physically exist is no longer stopped at the guard. Two things
        catch it instead, and both are weaker than a refusal: `fastapi/SKILL.md`
        tells the model in the system prompt that CPython has no stop-the-world
        collector and not to reach for `gc_pressure`, and the manifest, report and
        scorer all record that the name was novel. That is detection rather than
        prevention.

        The trade was taken because the alternative -- a closed vocabulary --
        costs every genuinely undiscovered cause, on every runtime, forever. It
        is recorded here rather than buried so that a later reader can disagree
        with it knowing what it bought.
        """
        from crucible.perf.applicator import Change, novel_cause

        proposal = Proposal(
            cause_family="gc_pressure",
            changes=(Change(prop="DB_POOL_SIZE", value=20),),
            reasoning="mistaken carry-over from a JVM snapshot",
        )

        assert guard_proposal(fastapi, proposal) is None
        # Not silent: this is what the report and the scorer read.
        assert novel_cause(fastapi, proposal) == "gc_pressure"

    def test_a_jvm_PROPERTY_is_still_refused_however_it_is_labelled(self, fastapi):
        """The half that did not weaken, and the half that was always doing the work.

        The allowlist is the authority. A proposal reaching for a JVM knob on a
        CPython target is refused whether it calls itself `gc_pressure`, calls
        itself `pool`, or calls itself something nobody has ever heard of.
        """
        from crucible.perf.applicator import Change

        for label in ("gc_pressure", "pool", "an_entirely_invented_cause"):
            proposal = Proposal(
                cause_family=label,
                changes=(Change(prop="jvm.heap.max", value="2g"),),
                reasoning="carry-over from a JVM snapshot",
            )
            refusal = guard_proposal(fastapi, proposal)
            assert refusal is not None, f"{label} let a JVM property through"
            assert "jvm.heap.max" in refusal

    def test_the_skill_still_tells_the_model_not_to_reach_for_gc(self, fastapi):
        """Now load-bearing rather than belt-and-braces.

        With the guard no longer refusing the label, the system prompt is the
        first line of defence against a Spring Boot habit on a CPython target.
        It was worth having when the guard also caught it; it is worth asserting
        now that the guard does not.
        """
        text = fastapi.skill_text()
        assert "gc_pressure" in text
        assert "do not reach for" in text.lower()


class TestAllowedProperties:
    def test_a_pool_size_change_inside_bounds_is_permitted(self, fastapi):
        from crucible.perf.applicator import Change

        proposal = Proposal(
            cause_family="pool",
            changes=(Change(prop="DB_POOL_SIZE", value=20),),
            reasoning="pending peaked at the configured size",
        )

        assert guard_proposal(fastapi, proposal) is None

    def test_a_property_outside_the_allowed_list_is_refused(self, fastapi):
        """Least privilege, same as spring-boot's 2.1: the allowed list is
        short and deliberate, and a plausible-sounding Uvicorn flag that is
        not on it is out of scope regardless of how sensible the value is."""
        from crucible.perf.applicator import Change

        proposal = Proposal(
            cause_family="worker_saturation",
            changes=(Change(prop="UVICORN_TIMEOUT_KEEP_ALIVE", value=5),),
            reasoning="plausible-sounding, but not on the allowed list",
        )

        refusal = guard_proposal(fastapi, proposal)

        assert refusal is not None
        assert "not an allowed property" in refusal

    def test_a_value_outside_bounds_is_refused(self, fastapi):
        from crucible.perf.applicator import Change

        proposal = Proposal(
            cause_family="worker_saturation",
            changes=(Change(prop="UVICORN_WORKERS", value=999),),
            reasoning="far above the declared maximum",
        )

        refusal = guard_proposal(fastapi, proposal)

        assert refusal is not None
        assert "above the maximum" in refusal


class TestSkillMdGrantsNoAuthority:
    """DESIGN.md section 5's table: SKILL.md is rendered into the prompt and
    changes how the model reasons; it never grants what fastapi.yaml does
    not already grant."""

    def test_skill_text_loads_and_discusses_gil_contention(self, fastapi):
        text = fastapi.skill_text()

        assert "gil_contention" in text
        assert "GIL" in text or "gil" in text.lower()

    def test_skill_text_explicitly_warns_against_the_gc_habit(self, fastapi):
        """The failure this deliverable exists to prevent: a model that has
        seen many more Spring Boot snapshots than FastAPI ones reaches for
        gc_pressure out of habit. The prose names that exact mistake."""
        text = fastapi.skill_text()

        assert "gc_pressure" in text
        assert "no GC pause" in text or "not a cause family" in text

    def test_a_property_named_only_in_the_skill_is_still_refused(self, fastapi):
        """Matches spring-boot's 9.2: SKILL.md can discuss a Uvicorn dial by
        name without that granting authority to change it."""
        from crucible.perf.applicator import Change

        assert "UVICORN_WORKERS" in fastapi.skill_text()
        proposal = Proposal(
            cause_family="worker_saturation",
            changes=(Change(prop="UVICORN_TIMEOUT_KEEP_ALIVE", value=5),),
            reasoning="named in SKILL.md's prose, not in allowed_properties",
        )

        assert guard_proposal(fastapi, proposal) is not None


class TestProfileCannotAuthoriseEditingItself:
    """Matches spring-boot's 9.3: config/profiles/** is protected regardless
    of which runtime's profile is being evaluated."""

    def test_config_profiles_is_a_protected_path(self, fastapi):
        assert "config/profiles/**" in fastapi.protected_paths

    def test_no_allowed_property_names_a_profile_path(self, fastapi):
        assert not any("profile" in prop.lower() for prop in fastapi.allowed_properties)
