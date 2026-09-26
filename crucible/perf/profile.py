"""The target profile: what a runtime can fail from, and what may be changed.

Read from a file. Never hardcoded -- not even while only one profile exists
(``AGENTS.md`` non-negotiable 8). The moment a runtime constant lives in Python,
adding FastAPI means editing the package, and the cause families of whichever
runtime was written first leak into every other one.

``profile.yaml`` and ``SKILL.md`` are deliberately separate (``DESIGN.md`` section 5).
``SKILL.md`` is read by the model into the system prompt and describes how this
runtime fails; it grants no authority. ``profile.yaml`` is read by the guard, the
applicator and the scorer, and is the only source of allowed properties and their
bounds. :meth:`TargetProfile.skill_text` therefore returns prose for the prompt and
nothing else -- it is never consulted by :meth:`TargetProfile.check_change`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.memory.models import MemoryKind
from .deploy import DeployTarget

#: ``config/profiles/`` as shipped next to the package.
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"


def profile_dir(explicit: str | Path | None = None) -> Path:
    """Where profiles live: the argument, then ``CRUCIBLE_PROFILE_DIR``, then default."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    from_env = os.getenv("CRUCIBLE_PROFILE_DIR")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return DEFAULT_PROFILE_DIR


@dataclass(frozen=True)
class Bounds:
    """The safe range for one tunable property.

    Bounds are part of the profile, not of the model's judgement. A proposal
    outside them is refused before it is ever applied -- the agent argues for a
    value, the profile decides whether that value is sane for this runtime.
    """

    minimum: float | None = None
    maximum: float | None = None
    kind: str = "int"
    allowed_values: list[Any] | None = None

    def coerce(self, value: Any) -> Any:
        """Parse ``value`` into this bound's type, or raise ``ValueError``."""
        if self.kind == "int":
            return int(value)
        if self.kind == "float":
            return float(value)
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"true", "false"}:
                return text == "true"
            raise ValueError(f"{value!r} is not a boolean")
        return str(value)

    def violation(self, value: Any) -> str | None:
        """Why ``value`` is out of bounds, or ``None`` when it is acceptable."""
        try:
            parsed = self.coerce(value)
        except (TypeError, ValueError):
            return f"{value!r} is not a valid {self.kind}"
        if self.allowed_values is not None and parsed not in self.allowed_values:
            return f"{parsed!r} is not one of {self.allowed_values}"
        if self.kind in {"int", "float"}:
            if self.minimum is not None and parsed < self.minimum:
                return f"{parsed} is below the minimum {self.minimum}"
            if self.maximum is not None and parsed > self.maximum:
                return f"{parsed} is above the maximum {self.maximum}"
        return None


@dataclass(frozen=True)
class RestartContract:
    """How this runtime is restarted, and how we know it came back.

    ``manual`` exists because ``DESIGN.md`` section 11 requires the campaign to
    block with instructions rather than fail when automation is not possible. A
    manual restart is recorded on the manifest -- a run with human intervention is
    not comparable to a fully autonomous one.
    """

    command: list[str] = field(default_factory=list)
    manual: bool = False
    instructions: str = ""
    health_url: str = ""
    health_timeout_s: float = 60.0
    settle_s: float = 0.0


@dataclass(frozen=True)
class TargetProfile:
    """One runtime's operational contract, as loaded from ``profile.yaml``."""

    name: str
    runtime: str
    cause_families: tuple[str, ...]
    allowed_properties: dict[str, Bounds]
    protected_paths: tuple[str, ...]
    config_file: str
    restart: RestartContract
    deploy: Any
    metric_map: dict[str, str]
    gauges: dict[str, str]
    window_timers: tuple[str, ...]
    snapshot_metrics: dict[str, str]
    #: Logical metric name -> how this runtime publishes it under Prometheus.
    #: Declared rather than derived: Micrometer's Prometheus registry appends
    #: each meter's base unit to its name, so a derived name is right until the
    #: first meter whose unit differs -- and a wrong series name returns no data,
    #: which the collector honestly records as "never measured". The agent would
    #: then be told it has no evidence about a meter Prometheus is scraping fine.
    promql: dict[str, Any]
    #: Logical metric name -> how this runtime publishes it under Datadog.
    #: Declared for the same reason as `promql`: Datadog's own naming and
    #: aggregation conventions for a submitted timer are a property of the
    #: backend and the client library that fed it, not something safe to
    #: derive from the Micrometer name alone.
    datadog: dict[str, Any] = field(default_factory=dict)
    redaction_allowlist: tuple[str, ...] = ()
    skill_file: str = ""
    #: Which memory kind holds this profile's policy (the SLA, the load profile,
    #: the budget ceiling). ``AGENTS.md`` non-negotiable 4 requires the SLA to be
    #: locked TWICE -- once as a protected path above, and once as a memory kind
    #: the agent has no write permission for. This field is the second lock's
    #: declaration; :mod:`crucible.perf.policy` is what enforces it. A file guard
    #: stops working the moment config moves to a different path and nothing
    #: notices, because the guard still passes on a path nothing writes to any
    #: more. A memory permission cannot be sidestepped that way.
    policy_memory_kind: MemoryKind = MemoryKind.POLICY
    source_path: Path | None = None

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> TargetProfile:
        """Load a profile from an explicit file path."""
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"target profile not found: {resolved}")
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{resolved} must contain a mapping")
        return cls.from_mapping(data, source_path=resolved)

    @classmethod
    def named(cls, name: str, directory: str | Path | None = None) -> TargetProfile:
        """Load ``<name>.yaml`` from the profile directory."""
        return cls.load(profile_dir(directory) / f"{name}.yaml")

    @classmethod
    def from_mapping(cls, data: dict[str, Any], source_path: Path | None = None) -> TargetProfile:
        missing = [key for key in ("name", "runtime", "cause_families") if key not in data]
        if missing:
            raise ValueError(f"target profile is missing required keys: {', '.join(missing)}")

        allowed: dict[str, Bounds] = {}
        for prop, spec in (data.get("allowed_properties") or {}).items():
            spec = spec or {}
            if not isinstance(spec, dict):
                raise ValueError(f"bounds for {prop} must be a mapping")
            allowed[str(prop)] = Bounds(
                minimum=spec.get("min"),
                maximum=spec.get("max"),
                kind=str(spec.get("type", "int")),
                allowed_values=spec.get("allowed_values"),
            )

        restart_spec = data.get("restart") or {}
        restart = RestartContract(
            command=list(restart_spec.get("command") or []),
            manual=bool(restart_spec.get("manual", False)),
            instructions=str(restart_spec.get("instructions", "")),
            health_url=str(restart_spec.get("health_url", "")),
            health_timeout_s=float(restart_spec.get("health_timeout_s", 60.0)),
            settle_s=float(restart_spec.get("settle_s", 0.0)),
        )

        return cls(
            name=str(data["name"]),
            runtime=str(data["runtime"]),
            cause_families=tuple(str(c) for c in data["cause_families"]),
            allowed_properties=allowed,
            protected_paths=tuple(str(p) for p in (data.get("protected_paths") or [])),
            config_file=str(data.get("config_file", "")),
            restart=restart,
            deploy=DeployTarget.from_mapping(data.get("deploy")),
            metric_map=dict(data.get("metric_map") or {}),
            gauges={str(k): str(v) for k, v in (data.get("gauges") or {}).items()},
            window_timers=tuple(str(t) for t in (data.get("window_timers") or [])),
            snapshot_metrics={str(k): str(v) for k, v in (data.get("snapshot_metrics") or {}).items()},
            promql=dict(data.get("promql") or {}),
            datadog=dict(data.get("datadog") or {}),
            redaction_allowlist=tuple(str(k) for k in (data.get("redaction_allowlist") or [])),
            skill_file=str(data.get("skill_file", "")),
            # Not read from the file. A profile that could nominate its own policy
            # kind could nominate one the agent is allowed to write, which would
            # unlock the goalpost from inside the very file the first lock
            # protects. The kind is fixed by the code; the profile does not vote.
            policy_memory_kind=MemoryKind.POLICY,
            source_path=source_path,
        )

    # -- authority --------------------------------------------------------

    def check_change(self, prop: str, value: Any) -> str | None:
        """Why this change is refused, or ``None`` when the profile permits it.

        Two questions in a fixed order: is the property on the list at all, and is
        the value inside its bounds. An unlisted property is refused even when the
        value looks harmless -- the allowlist is the authority, not the value.
        """
        bounds = self.allowed_properties.get(prop)
        if bounds is None:
            return f"{prop} is not an allowed property for profile {self.name!r}"
        return bounds.violation(value)

    def skill_text(self) -> str:
        """The runtime's ``SKILL.md`` prose, for the system prompt and nowhere else.

        Returns ``""`` when the profile declares no skill file. This never feeds
        :meth:`check_change`: skills change how the model approaches the work, and
        grant no authority (``DESIGN.md`` section 5).
        """
        if not self.skill_file:
            return ""
        base = self.source_path.parent if self.source_path else profile_dir()
        path = (base / self.skill_file).resolve()
        if not path.exists():
            raise FileNotFoundError(f"profile {self.name!r} names a missing skill file: {path}")
        return path.read_text(encoding="utf-8")
