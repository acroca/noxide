"""GitHub Copilot authentication and chat completions.

Auth chain:
1. Device flow (one-time) → persists OAuth token to state_dir/oauth_token
2. Copilot token exchange → short-lived bearer, cached in memory
3. Chat completions via https://api.githubcopilot.com/chat/completions
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# GitHub App client ID of the Copilot plugin (copilot.vim/JetBrains).
# Unlike VS Code's OAuth app ID, this authorizes only Copilot itself —
# no repo/workflow/email scopes appear on the consent screen.
_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_CHAT_URL = "https://api.githubcopilot.com/chat/completions"
_MODELS_URL = "https://api.githubcopilot.com/models"

OAUTH_TOKEN_FILENAME = "oauth_token"

# Transient failures (5xx, network errors) are retried with exponential backoff
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0

_COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.4",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GitHubCopilotChat/0.22.4",
}


async def send_with_retries(send, what: str) -> httpx.Response:
    """Await `send()` and return its response, retrying transient failures.

    Retries 5xx responses and network errors up to _MAX_ATTEMPTS with
    exponential backoff. Non-5xx responses (including 4xx) are returned
    as-is for the caller to handle.

    Public because `transcribe` shares it: both talk to a GitHub HTTP API
    with the same transient-failure profile.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = await send()
        except httpx.TransportError as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if r.status_code < 500 or attempt == _MAX_ATTEMPTS:
                return r
            reason = f"HTTP {r.status_code}"
        delay = _RETRY_BASE_DELAY * 2 ** (attempt - 1)
        logger.warning(
            "%s failed (%s), retrying in %.0fs (attempt %d/%d)",
            what, reason, delay, attempt, _MAX_ATTEMPTS,
        )
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class CopilotAuth:
    """Manages OAuth token + short-lived Copilot bearer token."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._token_file = state_dir / OAUTH_TOKEN_FILENAME
        self._bearer: str | None = None
        self._bearer_expires: float = 0.0

    # ------------------------------------------------------------------
    # Device flow
    # ------------------------------------------------------------------

    async def device_flow(self) -> None:
        """Run device-flow OAuth to obtain and persist the OAuth token."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                _DEVICE_CODE_URL,
                data={"client_id": _CLIENT_ID},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_url = data["verification_uri"]
        interval = int(data.get("interval", 5))

        print(f"\n{'='*60}")
        print(f"Open: {verification_url}")
        print(f"Enter code: {user_code}")
        print(f"{'='*60}\n")

        # Poll until authorized
        async with httpx.AsyncClient() as client:
            while True:
                await asyncio.sleep(interval)
                r = await client.post(
                    _OAUTH_TOKEN_URL,
                    data={
                        "client_id": _CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                r.raise_for_status()
                result = r.json()
                error = result.get("error")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    interval += 5
                    continue
                elif error:
                    raise RuntimeError(f"Device flow error: {error}: {result.get('error_description')}")

                oauth_token = result.get("access_token")
                if not oauth_token:
                    raise RuntimeError(f"No access_token in response: {result}")
                break

        self._persist_oauth_token(oauth_token)
        print("✓ Authorized. OAuth token saved.")

    def _persist_oauth_token(self, token: str) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token)
        self._token_file.chmod(0o600)

    def _load_oauth_token(self) -> str:
        if not self._token_file.exists():
            raise RuntimeError("No OAuth token found. Run `assistant auth` first.")
        return self._token_file.read_text().strip()

    # ------------------------------------------------------------------
    # Copilot bearer token (short-lived)
    # ------------------------------------------------------------------

    async def get_bearer(self) -> str:
        """Return a valid Copilot bearer token, refreshing if needed."""
        # Refresh 2 minutes before expiry
        if self._bearer and time.time() < self._bearer_expires - 120:
            return self._bearer
        return await self._refresh_bearer()

    async def _refresh_bearer(self) -> str:
        oauth_token = self._load_oauth_token()
        async with httpx.AsyncClient() as client:
            r = await send_with_retries(
                lambda: client.get(
                    _COPILOT_TOKEN_URL,
                    headers={
                        **_COPILOT_HEADERS,
                        "Authorization": f"token {oauth_token}",
                    },
                    timeout=15,
                ),
                "Copilot token exchange",
            )
            if r.status_code == 401:
                raise RuntimeError(
                    "OAuth token rejected by GitHub. Re-run `assistant auth`."
                )
            r.raise_for_status()
            data = r.json()

        # Response: {"token": "...", "expires_at": 1234567890, ...}
        bearer = data.get("token")
        expires_at = data.get("expires_at", 0)
        if not bearer:
            raise RuntimeError(f"No token in Copilot token response: {data}")

        self._bearer = bearer
        self._bearer_expires = float(expires_at)
        logger.debug("Copilot bearer refreshed, expires at %s", expires_at)
        return bearer


class _TransientServerError(RuntimeError):
    """A 5xx response worth retrying."""


async def _read_sse_message(r: httpx.Response) -> dict[str, Any]:
    """Aggregate an SSE chat-completions stream into one non-streaming-shaped response."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_opaque: str | None = None
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] = {}

    async for line in r.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable SSE chunk: %.200s", data)
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_text"):
                reasoning_parts.append(delta["reasoning_text"])
            if delta.get("reasoning_opaque"):
                reasoning_opaque = delta["reasoning_opaque"]
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                entry = tool_calls.setdefault(
                    idx,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]

    # A stream that ends without a finish_reason was cut off mid-response;
    # a partial message (truncated tool arguments) would poison the history.
    if finish_reason is None:
        raise _TransientServerError("stream truncated (no finish_reason)")

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if tool_calls:
        for tc in tool_calls.values():
            args = tc["function"]["arguments"].strip()
            if not args:
                tc["function"]["arguments"] = "{}"
                continue
            try:
                json.loads(args)
            except json.JSONDecodeError:
                raise _TransientServerError(
                    f"malformed tool call arguments for {tc['function']['name']!r}"
                ) from None
        message["tool_calls"] = [
            {**tc, "id": tc["id"] or f"call_{i}"}
            for i, tc in sorted(tool_calls.items())
        ]
    # Reasoning fields must round-trip through history for thinking models
    if reasoning_parts:
        message["reasoning_text"] = "".join(reasoning_parts)
    if reasoning_opaque:
        message["reasoning_opaque"] = reasoning_opaque

    return {
        "choices": [{"message": message, "finish_reason": finish_reason or "stop"}],
        "usage": usage,
    }


class CopilotClient:
    """High-level Copilot chat client."""

    def __init__(self, auth: CopilotAuth, model: str) -> None:
        self._auth = auth
        self._model = model
        # model id -> whether /models reports structured-output support;
        # fetched lazily on first use, None until then.
        self._structured_outputs: dict[str, bool] | None = None

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model_id: str) -> None:
        """Switch the model used for all subsequent chat requests."""
        self._model = model_id

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        initiator: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST to chat/completions (streaming); returns an aggregated response dict.

        Streaming is required: the non-streaming endpoint drops tool calls for
        reasoning models (message arrives with reasoning_* but no tool_calls).

        ``initiator`` overrides the X-Initiator inference — sub-agents whose
        payload ends with a user message but which serve an ongoing user turn
        (research, vision) pass ``"agent"`` explicitly.

        ``response_format`` is included only when /models reports the current
        model supports structured outputs; callers must not rely on it being
        enforced (the endpoint accepts and ignores it today) and always parse
        the reply tolerantly.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format and await self._supports_structured_outputs(payload["model"]):
            payload["response_format"] = response_format

        # Copilot bills premium requests only for user-initiated calls; the
        # X-Initiator header marks agent-loop follow-ups (tool results) so
        # they don't each consume quota.
        if initiator is None:
            initiator = "user" if messages and messages[-1].get("role") == "user" else "agent"

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._chat_once(payload, initiator)
                # Stamp the model actually used so call sites can attribute
                # usage accurately across /model switches mid-flight.
                response["model"] = payload["model"]
                return response
            except (httpx.TransportError, _TransientServerError) as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay = _RETRY_BASE_DELAY * 2 ** (attempt - 1)
                logger.warning(
                    "Copilot chat completion failed (%s), retrying in %.0fs (attempt %d/%d)",
                    exc, delay, attempt, _MAX_ATTEMPTS,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _supports_structured_outputs(self, model: str) -> bool:
        """Whether /models reports structured-output support for ``model``.

        The capability map is fetched once and cached for the process. A
        failed fetch caches an empty map: response_format is advisory today,
        so degrading to prompt-only beats re-hitting a broken endpoint on
        every request.
        """
        if self._structured_outputs is None:
            self._structured_outputs = await self._fetch_structured_outputs()
        return self._structured_outputs.get(model, False)

    async def _fetch_structured_outputs(self) -> dict[str, bool]:
        try:
            bearer = await self._auth.get_bearer()
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    _MODELS_URL,
                    headers={**_COPILOT_HEADERS, "Authorization": "Bearer " + bearer},
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
        except Exception:
            logger.warning(
                "Model capability fetch failed; response_format disabled", exc_info=True
            )
            return {}
        return {
            m["id"]: bool(
                ((m.get("capabilities") or {}).get("supports") or {}).get("structured_outputs")
            )
            for m in data.get("data", [])
            if m.get("id")
        }

    async def _chat_once(self, payload: dict[str, Any], initiator: str) -> dict[str, Any]:
        bearer = await self._auth.get_bearer()
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                _CHAT_URL,
                json=payload,
                headers={
                    **_COPILOT_HEADERS,
                    "Authorization": "Bearer " + bearer,
                    "Content-Type": "application/json",
                    "X-Initiator": initiator,
                },
                timeout=90,
            ) as r:
                if r.status_code >= 500:
                    raise _TransientServerError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    logger.error(
                        "Copilot API error %d (model=%s): %s",
                        r.status_code, self._model, body,
                    )
                r.raise_for_status()
                return await _read_sse_message(r)


# ------------------------------------------------------------------
# Module-level singleton helpers
# ------------------------------------------------------------------

_auth: CopilotAuth | None = None
_client: CopilotClient | None = None


def init(state_dir: Path, model: str) -> None:
    global _auth, _client
    _auth = CopilotAuth(state_dir)
    _client = CopilotClient(_auth, model)


def get_client() -> CopilotClient:
    if _client is None:
        raise RuntimeError("copilot.init() has not been called")
    return _client


async def run_device_flow(state_dir: Path) -> None:
    auth = CopilotAuth(state_dir)
    await auth.device_flow()
