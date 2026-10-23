"""AI suggestions for the wizard's SLA and scenarios. Group 30.

DRAFTED by Claude Code, 23 October 2026. REVIEWED AND APPROVED by the operator, 24 October 2026 (see docs/CRUCIBLE_TEST_ASSERTIONS.md).
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 30 for the questions to check.

"Suggest SLA" and "Suggest scenarios" used to be canned replies on a timer. They
now call the model. What is asserted:

1. the model's answer is untrusted: it is bounds-checked, and an unusable answer
   is refused whole, never half-applied;
2. a suggestion writes nothing: the SLA file is unchanged after one;
3. it needs the control token, because it spends a model call;
4. the operator's goal reaches the prompt, and the noise floor is never asked for;
5. the rationale cannot carry markup into the page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.core.memory.embeddings import DeterministicEmbedder
from crucible.ui import perf_ui
from crucible.ui.knowledge import KnowledgeBase
from crucible.ui.perf_ui import PerfContext
from crucible.ui.suggest import SuggestionRefused, build_prompt, parse

REPO = Path(__file__).resolve().parents[1]
TOKEN = "test-control-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLA = {"p99_ms": 150, "error_rate_pct": 1.0, "users": 50, "rationale": "Goal says 150 ms."}
SCENARIOS = {"scenarios": [{"name": "steady_50u", "users": 50, "duration_s": 300, "ramp_s": 30},
                           {"name": "burst_100u", "users": 100, "duration_s": 180, "ramp_s": 15}],
             "rationale": "Steady load plus a 2x burst."}


class TestTheAnswerIsUntrusted:
    def test_a_good_sla_is_parsed(self):
        assert parse("sla", json.dumps(SLA)) == {"p99_ms": 150.0, "error_rate_pct": 1.0, "users": 50,
                                                  "rationale": "Goal says 150 ms."}

    def test_json_inside_a_fence_and_prose_is_found(self):
        text = "Here you go:\n```json\n" + json.dumps(SCENARIOS) + "\n```"
        assert [s["name"] for s in parse("scenarios", text)["scenarios"]] == ["steady_50u", "burst_100u"]

    @pytest.mark.parametrize("bad", [
        "no json at all",
        json.dumps({**SLA, "p99_ms": -5}),
        json.dumps({**SLA, "p99_ms": "150"}),
        json.dumps({**SLA, "error_rate_pct": 120}),
        json.dumps({**SLA, "users": 12.5}),
        json.dumps({**SLA, "users": True}),
        json.dumps({k: v for k, v in SLA.items() if k != "p99_ms"}),
    ])
    def test_an_unusable_sla_is_refused(self, bad):
        with pytest.raises(SuggestionRefused):
            parse("sla", bad)

    @pytest.mark.parametrize("scenarios", [
        [],
        [SCENARIOS["scenarios"][0]] * 2,
        [{**SCENARIOS["scenarios"][0], "name": "Steady Load"}],
        [{**SCENARIOS["scenarios"][0], "ramp_s": 400}],
        [{**SCENARIOS["scenarios"][0], "duration_s": 5}],
        [{**SCENARIOS["scenarios"][0], "name": f"s{i}"} for i in range(7)],
    ])
    def test_an_unusable_scenario_list_is_refused_whole(self, scenarios):
        with pytest.raises(SuggestionRefused):
            parse("scenarios", json.dumps({"scenarios": scenarios}))


class TestThePrompt:
    def test_the_goal_reaches_the_prompt_verbatim(self):
        goal = "keep /api/db under 150 ms at 80 users"
        prompt = build_prompt("sla", goal=goal, endpoint="/api/db", mode="Advisory", facts="-", excerpts="-")
        assert goal in prompt

    @pytest.mark.parametrize("kind", ["sla", "scenarios"])
    def test_the_noise_floor_is_never_asked_for(self, kind):
        prompt = build_prompt(kind, goal="g", endpoint="/e", mode="m", facts="", excerpts="")
        assert "noise" not in prompt.lower()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", TOKEN)
    sla = tmp_path / "slo.yaml"
    sla.write_bytes((REPO / "config" / "slo.yaml").read_bytes())
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, sla_path=str(sla), results_dir=str(tmp_path / "r"),
                                         state_dir=tmp_path / "state")
    app.state.knowledge = KnowledgeBase.under(tmp_path / "state", DeterministicEmbedder())
    reply = {"text": json.dumps(SLA), "model": "gemini-x", "provider": "gemini"}
    calls: list[str] = []

    async def fake_chat(self, *, prompt, system, request=None):
        calls.append(prompt)
        return dict(reply)

    monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
    return TestClient(app), sla, reply, calls


def _suggest(client, kind="sla", headers=AUTH):
    return client.post("/v1/perf/suggest", json={"kind": kind, "goal": "p99 under 150 ms", "endpoint": "/api/db",
                                                  "mode": "Advisory"}, headers=headers)


class TestTheRoute:
    def test_a_suggestion_is_returned_with_the_model_that_made_it(self, app):
        client, *_ = app
        body = _suggest(client).json()
        assert body["suggestion"]["p99_ms"] == 150.0
        assert body["model"] == "gemini-x"

    def test_a_suggestion_writes_nothing(self, app):
        client, sla, *_ = app
        before = sla.read_bytes()
        _suggest(client)
        assert sla.read_bytes() == before

    def test_an_unusable_answer_is_a_502_with_the_reason(self, app):
        client, _, reply, _ = app
        reply["text"] = json.dumps({**SLA, "p99_ms": -1})
        response = _suggest(client)
        assert response.status_code == 502
        assert "p99_ms" in response.json()["detail"]

    def test_markup_in_the_rationale_is_escaped(self, app):
        client, _, reply, _ = app
        reply["text"] = json.dumps({**SLA, "rationale": "<img src=x onerror=alert(1)>"})
        assert "<img" not in _suggest(client).json()["suggestion"]["rationale"]

    def test_no_token_configured_fails_closed_and_calls_no_model(self, app, monkeypatch):
        client, _, _, calls = app
        monkeypatch.delenv("CRUCIBLE_CONTROL_TOKEN")
        assert _suggest(client).status_code == 503
        assert calls == []

    def test_a_wrong_token_is_refused_and_calls_no_model(self, app):
        client, _, _, calls = app
        assert _suggest(client, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert calls == []

    def test_an_unknown_kind_is_refused(self, app):
        client, *_ = app
        assert _suggest(client, kind="noise").status_code == 422
