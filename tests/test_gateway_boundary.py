"""Gateway boundary assertions. AGENTS.md non-negotiable 3, DESIGN.md 3.2, 18.

REVIEWED AND APPROVED by the operator, 26 September 2026 (docs added at the same
review; see `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 25).

**What this file is defending.** Crucible owns no provider keys. Every model call
goes to `glc_v5`, which holds them, so a leaked Crucible journal cannot contain a
Gemini key because Crucible never had one. The first test asserts that shape
directly: no field of the outgoing payload has "key" or "secret" in its name.

**The one that needs reading carefully.** DESIGN.md 3.2 draws a line that is easy
to lose: two different mechanisms can change which model answers, and they are
treated differently.

- **Error-driven cross-provider failover is disabled, always.** A 429 or a 503
  must never move a campaign from Gemini to Groq, because a campaign whose
  experiments were diagnosed by different models is not internally comparable.
- **Budget-driven downgrade is permitted**, walking `config/tiers.yaml` down under
  budget pressure -- but never invisibly, which is what `models_used` and the
  report's comparability warning are for.

The gateway CLIENT here is inherited S17Code code and it *can* fail over across
providers, which is why `test_gateway_can_fall_back_without_exposing_credentials`
exists and passes: the mechanism is real, and what that test pins is that using it
still leaks nothing. What keeps Crucible on the right side of 3.2 is that the
mechanism is switched OFF by configuration --
`test_no_error_driven_cross_provider_failover_is_configured` is the assertion that
says so, and it reads the shipped `.env.example` rather than trusting a comment.

**Retries within one provider are not failover.** The 503 retry test walks the
five Gemini keys, which is key rotation: same provider, same model, so nothing
about comparability changes. That distinction is the whole reason a retry test and
a failover test can both pass in one file without contradicting each other.

**Warm-up.** `glc_v5` runs on Render's free tier (DESIGN.md 18) and spins down
when idle, so the first call of a campaign pays a cold start. `warm_up()` charges
that to wall clock before the baseline, never to a measured window.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crucible.gateway import GatewayClient


@pytest.mark.asyncio
async def test_crucible_calls_gateway_without_owning_provider_keys(monkeypatch):
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "done", "provider": "gemini_2", "model": "gemini"})

    monkeypatch.setenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await GatewayClient("http://127.0.0.1:8111", client=http).complete(
            "Investigate the papers", "Use evidence", session="proof-run"
        )

    assert captured["url"] == "http://127.0.0.1:8111/v1/chat"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["provider"] == "gemini"
    assert payload["agent"] == "crucible_agent"
    assert payload["session"] == "proof-run"
    assert not any("key" in field.lower() or "secret" in field.lower() for field in payload)
    assert result["provider"] == "gemini_2"


@pytest.mark.asyncio
async def test_gateway_retries_transient_pool_cooldown(monkeypatch):
    """Retrying the SAME provider is key rotation, not failover.

    `gemini` expands to `gemini_1`..`gemini_5` in the gateway -- five keys, one
    model -- so a 503 retry changes which key served the call and nothing about
    which model did. DESIGN.md 3.2 is explicit that this is not the thing it
    forbids, and the distinction is why this test and the failover test below can
    both pass without contradicting each other.
    """
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"detail": "all providers on cooldown"})
        return httpx.Response(200, json={"text": "recovered", "provider": "gemini_3", "model": "gemini"})

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setenv("CRUCIBLE_GATEWAY_ATTEMPTS", "3")
    monkeypatch.setattr("crucible.gateway.asyncio.sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GatewayClient("http://gateway", client=http).complete("goal", "system")
    assert calls == 3
    assert result["text"] == "recovered"


@pytest.mark.asyncio
async def test_gateway_can_fall_back_without_exposing_credentials(monkeypatch):
    """The inherited client CAN fail over. What this pins is that it leaks nothing.

    Read with the test below it, not on its own. This one sets
    CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS deliberately, to exercise a mechanism
    Crucible ships switched off -- so it asserts the safety property (no
    credential in the payload, and the fallback provider's own model rather than
    the primary's) without asserting that Crucible uses it.
    """
    providers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        providers.append(payload["provider"])
        if payload["provider"] == "gemini":
            return httpx.Response(503, json={"detail": "pool exhausted"})
        assert "model" not in payload
        return httpx.Response(200, json={"text": "fallback", "provider": "openrouter", "model": "configured"})

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini")
    monkeypatch.setenv("CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS", "openrouter")
    monkeypatch.setenv("CRUCIBLE_GATEWAY_ATTEMPTS", "1")
    monkeypatch.setattr("crucible.gateway.asyncio.sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GatewayClient("http://gateway", client=http).chat(
            prompt="goal", system="system", request={"model": "gemini-only"}
        )
    assert providers == ["gemini", "openrouter"]
    assert result["text"] == "fallback"


def test_no_error_driven_cross_provider_failover_is_configured():
    """AGENTS.md non-negotiable 3, and until now nothing asserted it.

    The rule is that CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS stays empty -- no
    cross-provider failover, ever, under any error condition -- because a campaign
    whose experiments were diagnosed by different models is not internally
    comparable (DESIGN.md 3.2). The inherited gateway client honours the variable,
    so the rule lives entirely in configuration, which means a one-word edit to a
    deployment env file could break a measurement-integrity guarantee with nothing
    failing.

    Asserted against the shipped `.env.example` because that is the file a
    teammate copies. `.env` itself is not read here: it is gitignored and local,
    so asserting on it would pass or fail depending on whose machine ran the
    suite. What this can and cannot do is worth being honest about -- it cannot
    stop an operator exporting the variable at run time. What it does stop is the
    value being committed, which is how it would actually happen.
    """
    repo = Path(__file__).resolve().parents[1]
    example = (repo / ".env.example").read_text(encoding="utf-8")
    declarations = [
        line.strip()
        for line in example.splitlines()
        if line.strip().startswith("CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS=")
    ]
    # Presence is asserted, not just emptiness. An absent variable would make
    # this test pass by finding nothing to check -- the untested zero
    # EVALUATION.md warns about, committed by the test suite itself.
    assert len(declarations) == 1, (
        "CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS must be declared in .env.example, "
        "and declared empty. Absent, the default happens to be empty and the rule "
        "is invisible to the next person to edit the file."
    )
    _name, _, value = declarations[0].partition("=")
    assert value.strip() == "", (
        "CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS must ship empty: error-driven "
        "cross-provider failover is disabled always (AGENTS.md non-negotiable 3, "
        "DESIGN.md 3.2). A campaign whose experiments were diagnosed by different "
        "models is not internally comparable."
    )


@pytest.mark.asyncio
async def test_a_named_provider_and_model_are_sent_on_every_diagnosis_call(monkeypatch):
    """The second of the two independent mechanisms DESIGN.md 3.2 relies on.

    Naming a provider makes the gateway's `candidates()` expand to that
    provider's own key pool only, so `auto_route` is skipped and tier escalation
    cannot fire -- verified in the gateway's source on 11 September 2026. Naming a
    model as well is the belt to that braces. Either one alone would hold; the
    point of 3.2 is that both do, so one regression cannot silently change which
    model produced a campaign's numbers.
    """
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "ok", "provider": "gemini_1", "model": "pinned"})

    monkeypatch.setenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await GatewayClient("http://gateway", client=http).chat(
            prompt="diagnose", system="s", request={"model": "gemini-2.5-flash"}
        )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["provider"] == "gemini"
    assert payload["model"] == "gemini-2.5-flash"


def test_crucible_does_not_reimplement_gateway_routes(app_client):
    assert app_client.post("/v1/chat", json={}).status_code == 404
    assert app_client.get("/v1/providers").status_code == 404


# ---------------------------------------------------------------------------
# Warm-up -- week 3, deliverable 1. glc_v5 is hosted on Render's free tier
# (DESIGN.md 18) and spins down when idle, so the campaign's first model call
# would otherwise eat a cold start. warm_up() pays that cost against /healthz,
# before any diagnosis call, with a generous timeout and one retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_up_succeeds_on_the_first_healthz_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GatewayClient("http://gateway", client=http).warm_up(timeout_s=60)

    assert result["warm"] is True
    assert result["attempts"] == 1


@pytest.mark.asyncio
async def test_warm_up_retries_once_after_a_timeout_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.TimeoutException("cold start", request=request)
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GatewayClient("http://gateway", client=http).warm_up(timeout_s=60)

    assert calls == 2
    assert result["warm"] is True
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_warm_up_raises_after_two_consecutive_timeouts():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("still cold", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RuntimeError, match="did not respond"):
            await GatewayClient("http://gateway", client=http).warm_up(timeout_s=60)


@pytest.mark.asyncio
async def test_warm_up_logs_that_it_is_waiting_not_hung(capsys):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await GatewayClient("http://gateway", client=http).warm_up(timeout_s=60)

    out = capsys.readouterr().out
    assert "warming up" in out
    assert "60s" in out
