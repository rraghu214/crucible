"""Does the campaign fit in the quota? Arithmetic, not optimism.

Week 1 of the roadmap opens with "Gemini quota arithmetic" for a reason: the free
tier is the only thing that can make week 4's benchmark impossible, and it would
do so on the last day, after everything else worked. This module answers three
questions before that happens:

* how many campaigns fit in one day
* how many days the replay benchmark needs
* whether the 3-second inter-call delay, not the quota, is the real ceiling

The limits themselves live in ``config/quota.yaml`` and are marked unverified
until read off AI Studio. Google no longer publishes per-model free-tier numbers,
and the third-party trackers disagree, so a hardcoded constant here would be a
claim nobody measured -- which is the one thing this project is not allowed to do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

DEFAULT_QUOTA_FILE = Path(__file__).resolve().parents[2] / "config" / "quota.yaml"


class UnverifiedQuotaError(RuntimeError):
    """The limits have not been checked against AI Studio."""


@dataclass(frozen=True)
class ModelLimits:
    """One model's free-tier ceiling."""

    name: str
    rpm: int
    rpd: int
    tpm: int


@dataclass(frozen=True)
class QuotaPlan:
    """What the quota permits, per model, for a campaign of a given shape."""

    model: str
    calls_per_campaign: int
    tokens_per_campaign: int
    effective_rpm: float
    campaigns_per_day: float
    campaigns_per_day_all_keys: float
    benchmark_calls: int
    benchmark_days: float
    min_campaign_minutes: float
    rpm_bound_by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls_per_campaign": self.calls_per_campaign,
            "tokens_per_campaign": self.tokens_per_campaign,
            "effective_rpm": round(self.effective_rpm, 2),
            "rpm_bound_by": self.rpm_bound_by,
            "campaigns_per_day": round(self.campaigns_per_day, 2),
            "campaigns_per_day_all_keys": round(self.campaigns_per_day_all_keys, 2),
            "benchmark_calls": self.benchmark_calls,
            "benchmark_days": round(self.benchmark_days, 2),
            "min_campaign_minutes": round(self.min_campaign_minutes, 1),
        }


@dataclass(frozen=True)
class QuotaConfig:
    """``config/quota.yaml``, loaded."""

    verified: bool
    checked_on: str | None
    keys_in_rotation: int
    limits: dict[str, ModelLimits]
    min_seconds_between_calls: float
    campaign: dict[str, int]
    benchmark: dict[str, int]
    #: Which source is authoritative: "gateway" or "declared". Declared, never
    #: sniffed -- a gateway that is merely slow to start must not silently demote
    #: the run to stale local numbers.
    source: str = "declared"
    gateway_url: str = "http://127.0.0.1:8111"
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> QuotaConfig:
        resolved = Path(path).expanduser().resolve() if path else DEFAULT_QUOTA_FILE
        if not resolved.exists():
            raise FileNotFoundError(f"quota config not found: {resolved}")
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        limits = {
            str(name): ModelLimits(
                name=str(name),
                rpm=int(spec["rpm"]),
                rpd=int(spec["rpd"]),
                tpm=int(spec["tpm"]),
            )
            for name, spec in (data.get("limits") or {}).items()
        }
        return cls(
            verified=bool(data.get("verified", False)),
            checked_on=data.get("checked_on"),
            keys_in_rotation=int(data.get("keys_in_rotation", 1)),
            limits=limits,
            min_seconds_between_calls=float(data.get("min_seconds_between_calls", 3.0)),
            campaign=dict(data.get("campaign") or {}),
            benchmark=dict(data.get("benchmark") or {}),
            source=str(data.get("source", "declared")),
            gateway_url=str(data.get("gateway_url", "http://127.0.0.1:8111")),
            source_path=resolved,
        )

    # -- arithmetic -------------------------------------------------------

    def calls_per_campaign(self) -> int:
        """Model calls a campaign costs IF IT RUNS TO THE CAP.

        This is a worst case for planning, not an expectation. A campaign that
        meets the SLA at experiment 7 stops there and costs proportionally less.
        """
        c = self.campaign
        experiments = int(c.get("max_experiments", 0))
        per_experiment = (
            int(c.get("diagnosis_calls_per_experiment", 0))
            + int(c.get("proposal_calls_per_experiment", 0))
            + int(c.get("verdict_calls_per_experiment", 0))
        )
        return (
            experiments * per_experiment
            + int(c.get("watchdog_calls_per_campaign", 0))
            + int(c.get("summary_calls_per_campaign", 0))
        )

    def plan(self, model: str, *, allow_unverified: bool = False) -> QuotaPlan:
        """What ``model``'s quota permits. Refuses unverified limits by default."""
        if not self.verified and not allow_unverified:
            raise UnverifiedQuotaError(
                f"{self.source_path} is marked verified: false. Read the real limits from "
                "https://aistudio.google.com/rate-limit, update the file and set "
                "verified: true. Pass allow_unverified=True only to explore a "
                "hypothetical -- never to plan real work."
            )
        limits = self.limits.get(model)
        if limits is None:
            known = ", ".join(sorted(self.limits)) or "(none)"
            raise KeyError(f"no limits for {model!r} in {self.source_path}. Known: {known}")

        calls = self.calls_per_campaign()
        tokens = calls * int(self.campaign.get("tokens_per_call", 0))

        # Two ceilings, and the lower one wins. The 3 s delay AGENTS.md requires is
        # frequently the binding constraint, which matters: raising the quota would
        # then change nothing at all.
        delay_rpm = 60.0 / self.min_seconds_between_calls if self.min_seconds_between_calls else float("inf")
        effective_rpm = min(float(limits.rpm), delay_rpm)
        bound_by = "inter-call delay" if delay_rpm < limits.rpm else "provider RPM"

        campaigns_per_day = (limits.rpd / calls) if calls else float("inf")

        bench = self.benchmark
        benchmark_calls = int(bench.get("snapshots", 0)) * int(bench.get("calls_per_snapshot", 1))
        # Keys rotate, so the daily ceiling multiplies; RPM does not, because the
        # loop is sequential and the delay applies to the loop, not to the key.
        daily_capacity_all_keys = limits.rpd * max(1, self.keys_in_rotation)
        benchmark_days = (benchmark_calls / daily_capacity_all_keys) if daily_capacity_all_keys else float("inf")

        return QuotaPlan(
            model=model,
            calls_per_campaign=calls,
            tokens_per_campaign=tokens,
            effective_rpm=effective_rpm,
            campaigns_per_day=campaigns_per_day,
            campaigns_per_day_all_keys=campaigns_per_day * max(1, self.keys_in_rotation),
            benchmark_calls=benchmark_calls,
            benchmark_days=benchmark_days,
            min_campaign_minutes=(calls / effective_rpm) if effective_rpm else float("inf"),
            rpm_bound_by=bound_by,
        )


def format_report(config: QuotaConfig, *, allow_unverified: bool = False) -> str:
    """A human-readable table for every model in the config."""
    lines: list[str] = []
    if not config.verified:
        lines.append(
            "WARNING: limits are UNVERIFIED. Read them from https://aistudio.google.com/rate-limit "
            f"and set verified: true in {config.source_path}.\n"
        )
    calls = config.calls_per_campaign()
    lines.append(
        f"One campaign at the cap = {calls} model calls "
        f"({config.campaign.get('max_experiments')} experiments max; a campaign that meets "
        "its SLA earlier costs less)."
    )
    lines.append(f"Keys in rotation: {config.keys_in_rotation}. "
                 f"Minimum {config.min_seconds_between_calls}s between calls.\n")
    header = f"{'model':<24}{'campaigns/day':>15}{'all keys':>10}{'bench days':>12}{'RPM bound by':>18}"
    lines.append(header)
    lines.append("-" * len(header))
    for name in sorted(config.limits):
        plan = config.plan(name, allow_unverified=allow_unverified or not config.verified)
        lines.append(
            f"{name:<24}{plan.campaigns_per_day:>15.1f}{plan.campaigns_per_day_all_keys:>10.1f}"
            f"{plan.benchmark_days:>12.2f}{plan.rpm_bound_by:>18}"
        )
    lines.append("")
    lines.append(
        "Reminder: wall clock is the real budget, not money (DESIGN.md section 7). "
        "A campaign that fits the quota can still take eight hours."
    )
    return "\n".join(lines)


def fits_in_a_day(config: QuotaConfig, model: str, campaigns: int = 1) -> bool:
    """Whether ``campaigns`` campaigns fit inside one day's quota across all keys."""
    plan = config.plan(model, allow_unverified=not config.verified)
    return campaigns <= math.floor(plan.campaigns_per_day_all_keys)


__all__ = [
    "DEFAULT_QUOTA_FILE",
    "ModelLimits",
    "QuotaConfig",
    "QuotaPlan",
    "UnverifiedQuotaError",
    "fits_in_a_day",
    "format_report",
]


# ---------------------------------------------------------------------------
# Where the limits come from
# ---------------------------------------------------------------------------
#
# Two places know the rate limits: the gateway, which enforces them, and
# config/quota.yaml, which is hand-maintained. Keeping both is not redundancy,
# it is drift -- and it already bit us. quota.yaml said 10 rpm / 500 rpd across
# 3 keys; the running gateway reported 15 / 1000 across 5. Every number in the
# file was wrong, and the campaign arithmetic built on it was wrong with it.
#
# So the gateway is authoritative WHERE IT EXISTS. It will not always exist: an
# enterprise calling a provider directly has no glc_v5, and for them the declared
# file is the only source there is. Hence a switch.
#
# The switch is DECLARED, never sniffed. Auto-detection sounds friendlier and is
# worse: a gateway that is merely slow to start would silently demote the run to
# stale local numbers, and nothing would say so. An unreachable declared source
# is an error, not a cue to guess.


class QuotaUnavailableError(RuntimeError):
    """The declared source of truth could not be reached."""


class QuotaSource(Protocol):
    """Somewhere the per-model rate limits can be read from."""

    name: str

    def limits(self) -> dict[str, ModelLimits]: ...


@dataclass
class DeclaredQuota:
    """Limits as written in ``config/quota.yaml``.

    For deployments with no gateway to ask. The ``verified`` flag matters most
    here: this is precisely the case where numbers are copied by hand and go
    stale, and where nothing else will catch it.
    """

    config: QuotaConfig
    name: str = "declared"

    def limits(self) -> dict[str, ModelLimits]:
        if not self.config.verified:
            raise UnverifiedQuotaError(
                f"{self.config.source_path} is marked verified: false. Declared limits "
                "are only as good as the last time a human checked them; read the real "
                "values and set verified: true."
            )
        return dict(self.config.limits)


@dataclass
class GatewayQuota:
    """Limits read live from glc_v5's ``/v1/providers``.

    Authoritative because it is the thing that enforces them. Also reports how
    many keys are actually in rotation, derived by counting the provider
    instances (``gemini_1``..``gemini_5``) rather than trusting a hand-written
    count -- the error that made our own campaign arithmetic wrong by 3x.
    """

    base_url: str = "http://127.0.0.1:8111"
    timeout_s: float = 8.0
    name: str = "gateway"
    _payload: dict[str, Any] | None = None

    def _fetch(self) -> dict[str, Any]:
        if self._payload is not None:
            return self._payload
        import json
        import urllib.error
        import urllib.request

        url = f"{self.base_url.rstrip('/')}/v1/providers"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as response:
                self._payload = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise QuotaUnavailableError(
                f"the gateway is the declared quota source but {url} could not be read "
                f"({exc!r}). Refusing rather than falling back to config/quota.yaml, "
                "which would plan the campaign on numbers nobody checked."
            ) from exc
        return self._payload

    def limits(self) -> dict[str, ModelLimits]:
        payload = self._fetch()
        raw = payload.get("limits") or {}
        return {
            str(name): ModelLimits(
                name=str(name),
                rpm=int(spec.get("rpm", 0)),
                rpd=int(spec.get("rpd", 0)),
                tpm=int(spec.get("tpm", 0)),
            )
            for name, spec in raw.items()
        }

    def keys_in_rotation(self, base: str) -> int:
        """How many instances of ``base`` the gateway actually has loaded.

        ``gemini`` becomes ``gemini_1``..``gemini_5``, and each carries its own
        daily allowance, so this multiplies the campaigns-per-day figure. Counted
        rather than declared, because the declared count was wrong.
        """
        providers = self._fetch().get("providers") or []
        instances = [p for p in providers if str(p).startswith(f"{base}_")]
        return len(instances) or (1 if base in providers else 0)

    def cooldown_s(self, provider: str) -> float | None:
        """The gateway's own inter-call cooldown, if it declares one."""
        spec = (self._fetch().get("limits") or {}).get(provider) or {}
        value = spec.get("cooldown")
        return float(value) if value is not None else None


def quota_source(
    config: QuotaConfig,
    *,
    gateway_url: str | None = None,
) -> QuotaSource:
    """Build the source ``config`` declares. Never guesses which one to use."""
    declared = (config.source or "declared").strip().lower()
    if declared == "gateway":
        return GatewayQuota(base_url=gateway_url or config.gateway_url)
    if declared == "declared":
        return DeclaredQuota(config)
    raise ValueError(
        f"quota source {declared!r} is not recognised; expected 'gateway' or 'declared'"
    )
