"""The chat shows the sources its answer used, so the two RAGs can be told apart. Group 54.

DRAFTED by Claude Code, 2 November 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 54 for the questions to check.

Trial, 2 November 2026: a question about a run listed document sections under its
answer. The knowledge search returns no relevance score, so the top four hits were
always listed, whether or not the answer used them, and a run was never listed at all.
The model now ends its reply with `USED: S2,R`; the server shows only those, checked
against what the prompt offered. What is asserted:

1. an answer built from a run lists that run's manifest, not document sections;
2. an answer built from a document lists that document, not the run;
3. a mixed answer lists both;
4. a source the model names but was never offered is not shown;
5. a reply with no USED line shows no sources, rather than all of them;
6. the USED line is not shown to the operator, whatever markup wraps it;
7. the prompt numbers its sources and tells the model how to report them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.core.memory.embeddings import DeterministicEmbedder
from crucible.knowledge import KnowledgeBase
from crucible.ui import perf_ui
from crucible.ui.perf_ui import PerfContext, _split_used, _used_sources

REPO = Path(__file__).resolve().parents[1]
TOKEN = "test-chat-token"
RUN = "run-9-burst_100u"
DOC = ("# PerfLab\n\n## Startup\n\nPerfLab reads one row from Postgres on every request to /api/db, "
       "and starts within 60 seconds.\n")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CONTROL_TOKEN", TOKEN)


@pytest.fixture
def reply(monkeypatch):
    """The model's next answer, and every prompt it was sent."""
    state = {"text": "ok", "calls": []}

    async def fake_chat(self, *, prompt, system, request=None):
        state["calls"].append({"prompt": prompt, "system": system})
        return {"text": state["text"]}

    monkeypatch.setattr("crucible.gateway.GatewayClient.chat", fake_chat)
    return state


@pytest.fixture
def client(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / f"{RUN}.json").write_text(json.dumps({
        "run_id": RUN, "scenario": "s", "started_at_epoch_s": 1.0, "stopped_reason": "SLA met",
        "sla": {"name": "n", "endpoint": "/api/db", "p99_ms": 120.0, "error_rate_pct": 1.0},
        "baseline": {"load": {"p99_ms": 2500.0}, "sla_met": False}, "experiments": []}), encoding="utf-8")
    app = FastAPI()
    app.include_router(perf_ui.router)
    app.state.perf_context = PerfContext(root=REPO, results_dir=str(results), state_dir=tmp_path / "state")
    kb = KnowledgeBase.under(tmp_path, DeterministicEmbedder())
    kb.submit("PERF_LAB_PRODUCT.md", DOC, "operator")
    kb.wait_idle()
    app.state.knowledge = kb
    return TestClient(app)


def _ask(client, message="how fast is it?"):
    response = client.post("/v1/perf/chat", json={"message": message, "screen": "history"},
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()


def _names(body) -> list[str]:
    return [s["source"] for s in body["sources"]]


# -- 54.1 ---------------------------------------------------------------------


class TestSourcesAreTheOnesTheAnswerUsed:
    def test_an_answer_from_a_run_lists_the_run_not_the_documents(self, client, reply):
        reply["text"] = f"In {RUN}, p99 fell from 2500 to 69 ms.<br>USED: R"
        body = _ask(client, f"what did {RUN} measure?")
        assert body["sources"] == [{"source": f"results/{RUN}.json", "heading": "run manifest"}]

    def test_an_answer_from_a_document_lists_the_document_not_the_run(self, client, reply):
        reply["text"] = "Postgres, one row per request.<br>USED: S1"
        body = _ask(client, "what does /api/db read?")
        assert _names(body) == ["PERF_LAB_PRODUCT.md"] and body["sources"][0]["heading"]

    def test_a_mixed_answer_lists_both_in_the_order_given(self, client, reply):
        reply["text"] = "It reads Postgres, and p99 was 2500 ms.<br>USED: R,S1"
        assert _names(_ask(client)) == [f"results/{RUN}.json", "PERF_LAB_PRODUCT.md"]

    def test_the_configuration_is_its_own_source(self, client, reply):
        reply["text"] = "The SLA is 120 ms.<br>USED: F"
        body = _ask(client)
        assert body["sources"] == [{"source": "config/slo.yaml", "heading": "current SLA and target profile"}]

    def test_none_lists_nothing(self, client, reply):
        reply["text"] = "I cannot tell from what I have.<br>USED: none"
        assert _ask(client)["sources"] == []


# -- 54.2 ---------------------------------------------------------------------


class TestWhatWasNotOfferedIsNotShown:
    def test_a_source_that_does_not_exist_is_dropped(self, client, reply):
        """One document is indexed, so S1 exists and S9 does not."""
        reply["text"] = "x<br>USED: S9,S1"
        assert _names(_ask(client)) == ["PERF_LAB_PRODUCT.md"]

    def test_the_run_is_not_listed_when_no_run_is_on_disk(self, tmp_path, reply):
        app = FastAPI()
        app.include_router(perf_ui.router)
        (tmp_path / "results").mkdir()
        app.state.perf_context = PerfContext(root=REPO, results_dir=str(tmp_path / "results"), state_dir=tmp_path / "s")
        app.state.knowledge = KnowledgeBase.under(tmp_path, DeterministicEmbedder())
        reply["text"] = "x<br>USED: R"
        assert _ask(TestClient(app))["sources"] == []

    def test_a_missing_used_line_shows_no_sources_not_all_of_them(self, client, reply):
        """Listing sources nobody said were used implies a grounding that is not there."""
        reply["text"] = "Postgres, one row per request."
        assert _ask(client)["sources"] == []


# -- 54.3 ---------------------------------------------------------------------


class TestTheUsedLineIsNotShown:
    @pytest.mark.parametrize("text", [
        "Answer.<br>USED: S1",
        "Answer.\nUSED: S1",
        "<p>Answer.</p><p>USED: S1</p>",
        "Answer.<br />used: s1.",
    ])
    def test_it_is_removed_whatever_wraps_it(self, client, reply, text):
        reply["text"] = text
        body = _ask(client)
        assert "USED" not in body["reply"].upper() and "Answer." in body["reply"]
        assert _names(body) == ["PERF_LAB_PRODUCT.md"]

    def test_the_word_in_the_middle_of_an_answer_is_left_alone(self):
        text = "The pool USED: S1 earlier, then more text."
        assert _split_used(text) == (text, None)

    def test_ids_are_read_exactly_not_as_parts_of_words(self):
        assert _split_used("x<br>USED: S2, R, F")[1] == ["S2", "R", "F"]
        assert _split_used("x<br>USED: none")[1] == []
        assert _used_sources(["S1"], [], "", "config/slo.yaml") == []


# -- 54.4 ---------------------------------------------------------------------


class TestThePromptTellsTheModelHowToReport:
    def test_excerpts_are_numbered_and_the_system_prompt_defines_the_ids(self, client, reply):
        reply["text"] = "x<br>USED: none"
        _ask(client, "what does /api/db read?")
        call = reply["calls"][0]
        assert "[S1] PERF_LAB_PRODUCT.md" in call["prompt"]
        assert 'exactly "USED: "' in call["system"] and "R is the run under discussion" in call["system"]
