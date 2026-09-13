"""TargetProfile assertions — extends GROUP 2 of docs/CRUCIBLE_TEST_ASSERTIONS.md.

DRAFTED FOR REVIEW, not self-approved. These cover the profile half of the
authority boundary: what the agent may change, and the separation between
SKILL.md (prose, no authority) and profile.yaml (authority).

The guard half of Group 2 — protected paths enforced at write time, and the
Policy memory permission — is week 2 work and is not covered here. The
double-enforcement rule (AGENTS.md non-negotiable 4) is only half-tested until
both exist, which is itself worth reviewing.
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

    def test_the_eight_week_one_families_are_declared(self, spring_boot):
        """One per PerfLab endpoint. An endpoint whose family is undeclared is a
        fixture the agent cannot name correctly however good its reasoning."""
        assert set(spring_boot.cause_families) == {
            "connection_pool_exhaustion",
            "thread_pool_saturation",
            "gc_pressure",
            "inefficient_query",
            "cache_miss",
            "downstream_latency",
            "lock_contention",
            "payload_serialization",
        }


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
        file-path lock only; the Policy memory lock is asserted separately below
        and is not built yet.
        """
        assert "config/slo.yaml" in spring_boot.protected_paths
        assert any(path.startswith("locust/") for path in spring_boot.protected_paths)
        assert any(path.startswith("tests/") for path in spring_boot.protected_paths)

    @pytest.mark.xfail(
        reason="TODO(week 2): SLA is not yet a Policy memory kind. AGENTS.md "
               "non-negotiable 4 requires TWO independent locks and only the "
               "protected-path lock exists. Remove this marker when the memory "
               "permission lands -- an unexpected PASS here means it is done.",
        strict=True,
    )
    def test_the_sla_is_also_policy_memory_the_agent_cannot_write(self, spring_boot):
        """DESIGN.md 4.4, lock 2 of 2 — NOT BUILT YET. Deliberately failing.

        Why two locks and not one: a file guard is bypassed the moment config
        moves to a different path, and nobody notices, because the guard still
        passes on a path nothing writes to any more. A memory permission cannot be
        sidestepped that way.

        This is `strict=True` on purpose. While the lock is missing the suite
        reports an expected failure, so the gap is visible in every single run
        rather than buried in a comment. On the day week 2 builds it, this test
        starts passing and pytest turns that into an ERROR -- which is the prompt
        to delete the marker. It cannot be silently forgotten in either direction.
        """
        from crucible.core.memory.models import MemoryKind

        policy_backed = getattr(spring_boot, "policy_memory_kind", None)
        assert policy_backed == MemoryKind.POLICY, (
            "the SLA is protected by a file path only; the Policy memory lock "
            "required by AGENTS.md non-negotiable 4 does not exist yet"
        )

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
