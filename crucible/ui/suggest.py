"""AI suggestions for the wizard's SLA and load scenarios.

A suggestion only pre-fills form fields; the operator edits and submits them, so
the SLA is still written by a human (DESIGN.md §4.4). The model's answer is
untrusted input: it is parsed as JSON and every value is bounds-checked here, and
an unusable answer is refused whole rather than half-applied.

The noise threshold is never suggested. It is a measured property of the box
(§4.7, `slo.yaml` noise), and a model's guess at it would be a number nobody
measured presented as one.
"""
from __future__ import annotations

import json
import re
from typing import Any

KINDS = ("sla", "scenarios")
MAX_SCENARIOS = 6
_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class SuggestionRefused(ValueError):
    pass


SYSTEM = """You help an operator set up a performance test for Crucible.
Use the operator's goal first: any number the goal states must be used exactly.
Otherwise ground suggestions in the facts and knowledge excerpts, and name the
file of any excerpt you relied on in the rationale. Do not invent a measurement.
Reply with ONE JSON object and nothing else."""

_SHAPES = {
    "sla": ('{"p99_ms": <number, milliseconds>, "error_rate_pct": <number, percent>, '
            '"users": <integer concurrent users the SLA must hold at>, "rationale": "<one or two sentences>"}'),
    "scenarios": ('{"scenarios": [{"name": "<lower_snake_case>", "users": <integer>, "duration_s": <integer seconds>, '
                  '"ramp_s": <integer seconds>}], "rationale": "<one or two sentences>"}  '
                  f"Give 1 to {MAX_SCENARIOS} scenarios. The first must be steady load at the SLA's user count; "
                  "add others only if they test something the first does not, such as a burst."),
}


def build_prompt(kind: str, *, goal: str, endpoint: str, mode: str, facts: str, excerpts: str) -> str:
    if kind not in KINDS:
        raise SuggestionRefused(f"unknown suggestion kind {kind!r}")
    return (f"Operator's goal: {goal or '(none given)'}\nEndpoint under test: {endpoint or '(not given)'}\n"
            f"Test mode: {mode or '(not given)'}\n\nFacts:\n{facts}\n\nKnowledge excerpts:\n{excerpts}\n\n"
            f"Suggest the {'SLA' if kind == 'sla' else 'load scenarios'}. Reply exactly in this shape:\n{_SHAPES[kind]}")


def _json_object(text: str) -> dict[str, Any]:
    fenced = _FENCE.search(text or "")
    body = fenced.group(1) if fenced else (text or "")
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise SuggestionRefused("the model did not return a JSON object")
    try:
        value = json.loads(body[start:end + 1])
    except ValueError as exc:
        raise SuggestionRefused(f"the model returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SuggestionRefused("the model did not return a JSON object")
    return value


def _number(data: dict[str, Any], key: str, low: float, high: float, *, integer: bool = False) -> float | int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuggestionRefused(f"{key} is missing or not a number")
    if integer and value != int(value):
        raise SuggestionRefused(f"{key} must be a whole number")
    if not low <= value <= high:
        raise SuggestionRefused(f"{key}={value} is outside {low:g}..{high:g}")
    return int(value) if integer else float(value)


def _rationale(data: dict[str, Any]) -> str:
    value = data.get("rationale", "")
    return str(value)[:600] if isinstance(value, (str, int, float)) else ""


def parse(kind: str, text: str) -> dict[str, Any]:
    data = _json_object(text)
    if kind == "sla":
        return {
            "p99_ms": _number(data, "p99_ms", 1, 600_000),
            "error_rate_pct": _number(data, "error_rate_pct", 0, 100),
            "users": _number(data, "users", 1, 100_000, integer=True),
            "rationale": _rationale(data),
        }
    if kind == "scenarios":
        raw = data.get("scenarios")
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SCENARIOS:
            raise SuggestionRefused(f"scenarios must be a list of 1 to {MAX_SCENARIOS}")
        scenarios, names = [], set()
        for item in raw:
            if not isinstance(item, dict):
                raise SuggestionRefused("each scenario must be an object")
            name = str(item.get("name", ""))
            if not _NAME.match(name) or name in names:
                raise SuggestionRefused(f"scenario name {name!r} must be unique lower_snake_case")
            names.add(name)
            duration = _number(item, "duration_s", 30, 86_400, integer=True)
            scenarios.append({
                "name": name,
                "users": _number(item, "users", 1, 100_000, integer=True),
                "duration_s": duration,
                "ramp_s": _number(item, "ramp_s", 0, duration, integer=True),
            })
        return {"scenarios": scenarios, "rationale": _rationale(data)}
    raise SuggestionRefused(f"unknown suggestion kind {kind!r}")
