"""TargetProfile assertions — extends GROUP 2 of docs/CRUCIBLE_TEST_ASSERTIONS.md.

REVIEWED 20 September 2026; the operator raised a finding here and the fix
below is AWAITING RE-CHECK, not yet approved. These cover the profile half of the
authority boundary: what the agent may change, and the separation between
SKILL.md (prose, no authority) and profile.yaml (authority).

Both halves of the double-enforcement rule (AGENTS.md non-negotiable 4) now
exist. The file-path lock is asserted here; the Policy memory lock is built in
`crucible/perf/policy.py` and asserted in `tests/test_perf_policy.py`, including
the case that proves they are INDEPENDENT — an SLA moved outside
`protected_paths` still cannot be written by an agent.

Protected paths enforced at write time are asserted in
`tests/test_perf_applicator.py`.
"""

import pathlib

import pytest

from crucible.perf.profile import DEFAULT_PROFILE_DIR, Bounds, TargetProfile


@pytest.fixture
def spring_boot() -> TargetProfile:
    """The real shipped profile, not a fabricated one.

    Deliberate: a test against a hand-built profile would pass while the file the
    campaign actually loads was broken.
    """
    return TargetProfile.named("spring-boot")


class TestProfileIsReadFromAFile:
    """AGENTS.md non-negotiable 8 — read from a file, never hardcoded."""

    def test_the_shipped_profile_loads_from_disk(self, spring_boot):
        assert spring_boot.name == "spring-boot"
        assert spring_boot.runtime == "jvm"
        assert spring_boot.source_path is not None
        assert spring_boot.source_path.parent == DEFAULT_PROFILE_DIR

    def test_no_runtime_constant_is_baked_into_the_package(self):
        """The failure this guards against: a second runtime inherits the first's list.

        If cause families were a module constant, adding FastAPI would mean the
        agent proposing `gc` on CPython, where the analogue is gil_contention.

        WIDENED 13 Sep 2026. This previously inspected profile.py alone, and
        therefore passed for weeks while runner.py held JVM_GAUGES and
        WINDOW_TIMERS one file over -- a real breach of the rule, invisible to
        the test meant to enforce it. A rule policed in one file is not policed.
        """
        import crucible.perf as package

        pkg_dir = pathlib.Path(package.__file__).parent
        needles = ("hikaricp.", "jvm.", "gc_pressure")
        offenders = []
        for source in sorted(pkg_dir.rglob("*.py")):
            for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                # A comment may name a metric to explain WHY it must not be
                # hardcoded. Only a quoted string is an actual runtime constant.
                if stripped.startswith("#"):
                    continue
                quoted = '"' in line or "'" in line
                if quoted and any(n in line for n in needles):
                    offenders.append(f"{source.relative_to(pkg_dir)}:{line_no}: {stripped[:70]}")
        assert not offenders, (
            "runtime-specific names must come from the TargetProfile, not be "
            "hardcoded in the package. Offenders:\n  " + "\n  ".join(offenders)
        )

    def test_a_missing_profile_fails_loudly(self):
        with pytest.raises(FileNotFoundError):
            TargetProfile.named("no-such-runtime")


class TestCauseFamiliesComeFromTheProfile:
    """DESIGN.md 5 — declared by the TargetProfile, not a global constant."""

    def test_the_declared_families_are_the_eight_plus_application_code(self, spring_boot):
        """One per PerfLab endpoint, plus the one nothing could name.

        An endpoint whose family is undeclared is a fixture the agent cannot name
        correctly however good its reasoning. `application_code` was added on
        26 September 2026 for exactly that: `perflab_code_latency` existed as a
        fixture and no declared family fitted it, while `fastapi.yaml` had
        declared the same concept all along.

        Declaring it grants no authority -- see the test below, which is the half
        that matters.
        """
        assert set(spring_boot.cause_families) == {
            "connection_pool_exhaustion",
            "thread_pool_saturation",
            "gc_pressure",
            "inefficient_query",
            "cache_miss",
            "downstream_latency",
            "lock_contention",
            "payload_serialization",
            "application_code",
        }

    def test_declaring_a_cause_family_grants_no_property(self, spring_boot):
        """cause_families is vocabulary; allowed_properties is authority.

        `application_code` is the sharpest case: there is deliberately NOTHING on
        the allowed list that fixes slow code, so the agent can name it and must
        then report that it cannot fix it. A family the agent can name but cannot
        act on is how the config-only boundary becomes a declared scope rather
        than a blind spot (assertion 4.5).
        """
        assert "application_code" in spring_boot.cause_families
        assert "application_code" not in spring_boot.allowed_properties
        for prop in spring_boot.allowed_properties:
            assert not prop.startswith("jvm."), f"{prop} would let a code cause be 'fixed'"


class TestAuthorityBoundary:
    """2.1 and 2.5 — the allowlist is the authority, not the value."""

    def test_an_allowed_property_within_bounds_is_permitted(self, spring_boot):
        assert spring_boot.check_change("spring.datasource.hikari.maximum-pool-size", 20) is None

    def test_a_property_outside_the_allowed_list_is_refused(self, spring_boot):
        """Refused even when the value is harmless. The list decides, not the value."""
        refusal = spring_boot.check_change("spring.datasource.password", "anything")

        assert refusal is not None
        assert "not an allowed property" in refusal

    def test_a_value_outside_safe_bounds_is_refused(self, spring_boot):
        """A pool of 5000 exhausts the database's own connection limit instead."""
        refusal = spring_boot.check_change("spring.datasource.hikari.maximum-pool-size", 5000)

        assert refusal is not None
        assert "above the maximum" in refusal

    def test_a_non_numeric_value_is_refused_rather_than_coerced(self, spring_boot):
        """Silent coercion is how 'twenty' becomes 0 and the pool collapses."""
        refusal = spring_boot.check_change("spring.datasource.hikari.maximum-pool-size", "twenty")

        assert refusal is not None
        assert "not a valid int" in refusal

    def test_the_sla_and_load_profile_are_protected_paths(self, spring_boot):
        """DESIGN.md 4.4, lock 1 of 2 — the most important boundary in the product.

        An agent that can move its own goalpost passes every time. This is the
        file-path lock only; the Policy memory lock is asserted separately below,
        and the two are independent by design -- see `tests/test_perf_policy.py`
        for the case where this lock goes quiet and the other one still holds.
        """
        assert "config/slo.yaml" in spring_boot.protected_paths
        assert any(path.startswith("locust/") for path in spring_boot.protected_paths)
        assert any(path.startswith("tests/") for path in spring_boot.protected_paths)

    def test_the_sla_is_also_policy_memory_the_agent_cannot_write(self, spring_boot):
        """DESIGN.md 4.4, lock 2 of 2 — BUILT in week 2.

        Why two locks and not one: a file guard is bypassed the moment config
        moves to a different path, and nobody notices, because the guard still
        passes on a path nothing writes to any more. A memory permission cannot be
        sidestepped that way, because it attaches to the record's KIND rather than
        to where the bytes happen to live.

        This test carried `xfail(strict=True)` from week 1 until the lock landed,
        so the gap was reported in every run rather than buried in a comment, and
        the day it started passing pytest raised an ERROR — which is what prompted
        the marker's removal. It could not be forgotten in either direction.
        """
        from crucible.core.memory.models import MemoryKind

        assert spring_boot.policy_memory_kind == MemoryKind.POLICY

    def test_the_profile_does_not_get_to_choose_its_own_policy_kind(self, tmp_path):
        """The subtle version of the same hole. If a profile could nominate the
        memory kind holding its policy, it could nominate one the agent IS allowed
        to write — unlocking the goalpost from inside the very file the first lock
        protects. The kind is fixed in code; the profile does not vote."""
        from crucible.core.memory.models import MemoryKind
        from crucible.perf.profile import TargetProfile

        sneaky = TargetProfile.from_mapping({
            "name": "sneaky", "runtime": "jvm", "cause_families": ["gc_pressure"],
            "policy_memory_kind": "fact",
        })

        assert sneaky.policy_memory_kind == MemoryKind.POLICY

    def test_the_profile_cannot_authorise_editing_itself(self, spring_boot):
        """An agent that may rewrite its own bounds has no bounds."""
        assert any("profiles" in path for path in spring_boot.protected_paths)
        assert not any("profile" in prop for prop in spring_boot.allowed_properties)


class TestSkillGrantsNoAuthority:
    """DESIGN.md 5 — skills change how the model works, never what it may do."""

    def test_the_skill_file_loads_as_prose(self, spring_boot):
        text = spring_boot.skill_text()

        assert "cause families" in text.lower() or "cause family" in text.lower()

    def test_a_property_named_only_in_the_skill_is_still_refused(self, spring_boot):
        """The trap: SKILL.md discusses jvm heap sizing, so a model may propose it.

        Prose that mentions a knob must not become permission to turn it. If this
        ever fails, authority has leaked out of profile.yaml.
        """
        skill = spring_boot.skill_text()
        assert "heap" in skill.lower()

        refusal = spring_boot.check_change("spring.jvm.heap-max", "2g")
        assert refusal is not None

    def test_bounds_are_independent_of_any_skill_text(self):
        """Bounds are arithmetic on the profile alone; no prose is consulted."""
        bounds = Bounds(minimum=1, maximum=100, kind="int")

        assert bounds.violation(50) is None
        assert bounds.violation(101) is not None
