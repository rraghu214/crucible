"""The Crucible -> gateway seam: ordinary authenticated HTTP.

Crucible holds no provider credential. It asks the gateway for a completion; the
gateway owns keys, routing, quotas and provider quirks. Session 15 adds one thing
to this client: :meth:`GatewayClient.chat` returns the *usage* the gateway
reports (tokens, cache tokens, latency), so the budget controller prices the call
it actually made instead of guessing.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx


class GatewayClient:
    """A thin chat transport. Per-call request fields come from the caller's tier."""

    #: Request fields a tier may set. Everything else stays the client's business.
    PASSTHROUGH = (
        "provider", "model", "max_tokens", "temperature", "reasoning",
        "auto_route", "cache_system", "response_format", "agent", "session",
    )

    def __init__(self, base_url: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")).rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=120)
        self._owns_client = client is None

    def _payload(self, prompt: str, system: str, request: dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "system": system,
            "max_tokens": 1600,
            "temperature": 0,
            "reasoning": "off",
            "agent": "crucible_agent",
            # "gemini" is a logical gateway provider. The gateway expands it to
            # the independently metered gemini_1..N key pool; Crucible never sees
            # keys. Defaulting here means an unset env never falls through to the
            # gateway's own provider order (which may put a heavy model first).
            "provider": os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"),
        }
        for field, value in (request or {}).items():
            if field in self.PASSTHROUGH:
                payload[field] = value
        return payload

    async def chat(
        self, *, prompt: str, system: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One chat call, returning the usage the gateway measured.

        This is the ``ChatTransport`` protocol in :mod:`crucible.economics`: on a
        budgeted run the controller is the only caller, which is what makes "no
        unmetered spend" structural rather than a convention.
        """
        attempts = max(1, int(os.getenv("CRUCIBLE_GATEWAY_ATTEMPTS", "5")))
        response = None
        payload = self._payload(prompt, system, request)
        primary = str(payload.get("provider") or "gemini")
        fallbacks = [item.strip() for item in os.getenv("CRUCIBLE_GATEWAY_FALLBACK_PROVIDERS", "").split(",")
                     if item.strip() and item.strip() != primary]
        for provider in [primary, *fallbacks]:
            provider_payload = {**payload, "provider": provider}
            # A model explicitly paired with the primary provider must not leak
            # into a different provider's request; let glc_v4 choose that
            # fallback provider's configured model.
            if provider != primary:
                provider_payload.pop("model", None)
            for attempt in range(attempts):
                response = await self._client.post(
                    f"{self.base_url}/v1/chat", json=provider_payload
                )
                if response.status_code not in {429, 502, 503}:
                    break
                if attempt < attempts - 1:
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))
            if response.status_code not in {429, 502, 503}:
                break
        assert response is not None
        if response.status_code >= 400:
            raise RuntimeError(f"gateway /v1/chat returned {response.status_code}: {response.text[:500]}")
        body = response.json()
        return {
            "text": body.get("text", ""),
            "provider": body.get("provider"),
            "model": body.get("model"),
            "input_tokens": body.get("input_tokens") or 0,
            "output_tokens": body.get("output_tokens") or 0,
            "cache_read_input_tokens": body.get("cache_read_input_tokens") or 0,
            "cache_creation_input_tokens": body.get("cache_creation_input_tokens") or 0,
            "latency_ms": body.get("latency_ms"),
            "stop_reason": body.get("stop_reason"),
        }

    async def complete(
        self,
        prompt: str,
        system: str,
        *,
        session: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The unbudgeted path, for a run created without a ceiling."""
        body: dict[str, Any] = {"session": session, **(request or {})}
        result = await self.chat(prompt=prompt, system=system, request=body)
        return {
            "text": result["text"], "provider": result["provider"], "model": result["model"],
            "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
        }

    async def health(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/healthz", timeout=timeout_s)
        response.raise_for_status()
        return response.json()

    async def warm_up(self, *, timeout_s: float = 60.0) -> dict[str, Any]:
        """Wake a cold gateway before the campaign's first model call.

        `glc_v5` is hosted on Render's free tier (`DESIGN.md` 18), which spins the
        instance down when idle; the first request after a spin-down can take tens
        of seconds to answer. Paying that cost here, against `/healthz`, means the
        campaign's first real model call -- the baseline diagnosis -- lands on an
        already-warm gateway, and the wait is visibly a cold start rather than a
        silent hang. One retry: a `glc_v5` restart can occasionally need a second
        request to fully come up even after the first one returns.
        """
        last_error: httpx.TimeoutException | None = None
        for attempt in (1, 2):
            print(
                f"[gateway] warming up {self.base_url} (attempt {attempt}/2, "
                f"timeout {timeout_s:.0f}s) -- waiting for a possible cold start, not hung"
            )
            started = time.monotonic()
            try:
                body = await self.health(timeout_s=timeout_s)
            except httpx.TimeoutException as timeout:
                last_error = timeout
                elapsed = time.monotonic() - started
                print(f"[gateway] warm-up attempt {attempt} timed out after {elapsed:.0f}s")
                continue
            elapsed = time.monotonic() - started
            print(f"[gateway] gateway is warm ({elapsed:.1f}s)")
            return {"warm": True, "attempts": attempt, "elapsed_s": elapsed, "body": body}
        raise RuntimeError(
            f"gateway at {self.base_url} did not respond within {timeout_s:.0f}s across 2 "
            "attempts; it may still be cold-starting or may be down"
        ) from last_error

    def _channel_headers(self) -> dict[str, str]:
        token = os.getenv("CRUCIBLE_CHANNEL_BRIDGE_TOKEN", "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def channels(self) -> list[dict[str, Any]]:
        """Discover the gateway catalogue; Crucible carries no channel-name list."""
        response = await self._client.get(f"{self.base_url}/v1/channels")
        response.raise_for_status()
        body = response.json()
        channels = body.get("channels", [])
        if not isinstance(channels, list):
            raise RuntimeError("gateway returned an invalid channel catalogue")
        return channels

    async def send_channel(
        self,
        *,
        channel: str,
        recipient_id: str,
        text: str,
        thread_id: str | None = None,
        voice_audio_ref: str | None = None,
    ) -> dict[str, Any]:
        """Send through one dynamically discovered GLC adapter."""
        payload = {
            "channel": channel,
            "channel_user_id": recipient_id,
            "text": text,
            "thread_id": thread_id,
            "voice_audio_ref": voice_audio_ref,
            "attachments": [],
        }
        response = await self._client.post(
            f"{self.base_url}/v1/channels/{channel}/send",
            json=payload,
            headers=self._channel_headers(),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"gateway channel send returned {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("gateway returned an invalid channel receipt")
        return body

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
