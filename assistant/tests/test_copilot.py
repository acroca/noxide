"""Tests for Copilot token refresh logic."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

from assistant.copilot import (
    _CHAT_URL,
    _RESPONSES_URL,
    CopilotAuth,
    CopilotClient,
    CopilotUnavailableError,
)
from assistant.responses import OUTPUT_KEY


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def auth(state_dir: Path) -> CopilotAuth:
    state_dir.mkdir(parents=True)
    return CopilotAuth(state_dir)


def _write_oauth_token(state_dir: Path, token: str) -> None:
    tf = state_dir / "oauth_token"
    tf.write_text(token)
    tf.chmod(0o600)


# ------------------------------------------------------------------
# Device flow
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_device_flow_requests_no_oauth_scopes(
    auth: CopilotAuth, state_dir: Path
) -> None:
    device_response = MagicMock()
    device_response.json.return_value = {
        "device_code": "device-code",
        "user_code": "user-code",
        "verification_uri": "https://github.com/login/device",
        "interval": 5,
    }
    token_response = MagicMock()
    token_response.json.return_value = {"access_token": "oauth-token"}

    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=[device_response, token_response])
        mock_client_cls.return_value = mock_client

        await auth.device_flow()

    assert mock_client.post.call_args_list[0].kwargs["data"] == {
        "client_id": "Iv1.b507a08c87ecfe98"
    }
    assert (state_dir / "oauth_token").read_text() == "oauth-token"


# ------------------------------------------------------------------
# Token file I/O
# ------------------------------------------------------------------

def test_load_oauth_token_missing(auth: CopilotAuth) -> None:
    with pytest.raises(RuntimeError, match="No OAuth token"):
        auth._load_oauth_token()


def test_load_oauth_token_present(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "mytoken123")
    assert auth._load_oauth_token() == "mytoken123"


def test_persist_oauth_token(auth: CopilotAuth, state_dir: Path) -> None:
    auth._persist_oauth_token("newtoken")
    tf = state_dir / "oauth_token"
    assert tf.exists()
    assert tf.read_text() == "newtoken"
    # Check file permissions
    mode = tf.stat().st_mode & 0o777
    assert mode == 0o600


# ------------------------------------------------------------------
# ****** caching and refresh
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bearer_cached(auth: CopilotAuth, state_dir: Path) -> None:
    """get_bearer() should return cached token if not expired."""
    _write_oauth_token(state_dir, "oauth")
    auth._bearer = "cached_bearer"
    auth._bearer_expires = time.time() + 3600  # expires in 1 hour

    result = await auth.get_bearer()
    assert result == "cached_bearer"


@pytest.mark.asyncio
async def test_bearer_refreshes_when_expired(auth: CopilotAuth, state_dir: Path) -> None:
    """get_bearer() should call _refresh_bearer when expired."""
    _write_oauth_token(state_dir, "oauth")
    auth._bearer = "old_bearer"
    auth._bearer_expires = time.time() - 1  # expired

    new_expiry = time.time() + 1800
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "new_bearer", "expires_at": new_expiry}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await auth.get_bearer()

    assert result == "new_bearer"
    assert auth._bearer == "new_bearer"
    assert auth._bearer_expires == new_expiry


@pytest.mark.asyncio
async def test_bearer_refreshes_when_near_expiry(auth: CopilotAuth, state_dir: Path) -> None:
    """get_bearer() should refresh when within 2 minutes of expiry."""
    _write_oauth_token(state_dir, "oauth")
    auth._bearer = "old_bearer"
    auth._bearer_expires = time.time() + 60  # only 1 minute left (< 2 min threshold)

    new_expiry = time.time() + 1800
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "refreshed_bearer", "expires_at": new_expiry}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await auth.get_bearer()

    assert result == "refreshed_bearer"


@pytest.mark.asyncio
async def test_bearer_401_raises(auth: CopilotAuth, state_dir: Path) -> None:
    """401 from GitHub should raise RuntimeError with helpful message."""
    _write_oauth_token(state_dir, "bad_oauth")
    auth._bearer = None
    auth._bearer_expires = 0

    mock_response = MagicMock()
    mock_response.status_code = 401

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="OAuth token rejected"):
            await auth.get_bearer()

    assert mock_client.get.await_count == 1  # auth errors are not retried


# ------------------------------------------------------------------
# Transient error retries
# ------------------------------------------------------------------

def _response(status_code: int, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    if status_code >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=r
        )
    else:
        r.raise_for_status = MagicMock()
    return r


def _mock_client_cls(mock_client: AsyncMock) -> MagicMock:
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    cls = MagicMock()
    cls.return_value = mock_client
    return cls


@pytest.mark.asyncio
async def test_bearer_retries_on_5xx(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")
    ok = _response(200, {"token": "bearer", "expires_at": time.time() + 1800})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[_response(502), _response(502), ok])

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        result = await auth.get_bearer()

    assert result == "bearer"
    assert mock_client.get.await_count == 3
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_bearer_gives_up_after_max_attempts(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(502))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(CopilotUnavailableError):
            await auth.get_bearer()

    assert mock_client.get.await_count == 3


@pytest.mark.asyncio
async def test_bearer_retries_on_network_error(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")
    ok = _response(200, {"token": "bearer", "expires_at": time.time() + 1800})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[httpx.ConnectError("boom"), ok])

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        result = await auth.get_bearer()

    assert result == "bearer"
    assert mock_client.get.await_count == 2


# ------------------------------------------------------------------
# Streaming chat
# ------------------------------------------------------------------

def _sse_lines(chunks: list[dict]) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


def _stream_cm(status_code: int, lines: list[str] | None = None) -> MagicMock:
    """Mock for the context manager returned by httpx.AsyncClient.stream()."""
    resp = MagicMock()
    resp.status_code = status_code

    async def _aiter():
        for line in lines or []:
            yield line

    resp.aiter_lines = _aiter
    resp.aread = AsyncMock(return_value=b"error body")
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _chat_client(auth: CopilotAuth) -> CopilotClient:
    auth._bearer = "bearer"
    auth._bearer_expires = time.time() + 3600
    return CopilotClient(auth, model="test-model")


def _offline_chat_client(auth: CopilotAuth) -> CopilotClient:
    """A client whose capability map is already (emptily) cached, for tests
    about streaming and retries that don't stub /models."""
    client = _chat_client(auth)
    client._capabilities = {}
    return client


@pytest.mark.asyncio
async def test_chat_streams_and_aggregates_tool_calls(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Tool call deltas split across chunks must be reassembled; reasoning kept."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "¡Claro!"}}]},
        {"choices": [{"delta": {"reasoning_text": "think", "reasoning_opaque": "OPAQUE"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"path"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ': "a.md", "content": "x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "init vault"}])

    choice = result["choices"][0]
    msg = choice["message"]
    assert choice["finish_reason"] == "tool_calls"
    assert msg["content"] == "¡Claro!"
    assert msg["reasoning_opaque"] == "OPAQUE"
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tc1"
    assert tc["function"]["name"] == "write_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "a.md", "content": "x"}
    assert result["usage"] == {"prompt_tokens": 5, "completion_tokens": 7}
    # Request must ask for streaming
    payload = mock_client.stream.call_args.kwargs["json"]
    assert payload["stream"] is True


@pytest.mark.asyncio
async def test_chat_text_only_stream(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "hi"}])

    msg = result["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert "tool_calls" not in msg


@pytest.mark.asyncio
async def test_chat_response_includes_model(auth: CopilotAuth, state_dir: Path) -> None:
    """chat() stamps the model id used so call sites can attribute usage."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "hi"}])

    assert result["model"] == "test-model"  # _chat_client constructs with model="test-model"


# ------------------------------------------------------------------
# response_format gating on /models capability
# ------------------------------------------------------------------

_TEXT_CHUNKS = [
    {"choices": [{"delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
]

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "job_close", "strict": True, "schema": {"type": "object"}},
}


def _models_payload(structured_outputs: bool | None) -> dict:
    return {"data": [{
        "id": "test-model",
        "capabilities": {"supports": {"structured_outputs": structured_outputs}},
    }]}


@pytest.mark.asyncio
async def test_chat_sends_response_format_when_model_supports_it(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Capabilities are fetched once and cached; the schema rides the payload."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(200, _models_payload(True)))
    mock_client.stream = MagicMock(
        side_effect=lambda *a, **k: _stream_cm(200, _sse_lines(_TEXT_CHUNKS))
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        client = _chat_client(auth)
        await client.chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )
        await client.chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )

    for call in mock_client.stream.call_args_list:
        assert call.kwargs["json"]["response_format"] == _RESPONSE_FORMAT
    assert mock_client.get.await_count == 1  # capability map cached


@pytest.mark.asyncio
async def test_chat_drops_response_format_when_model_lacks_support(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(200, _models_payload(None)))
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(_TEXT_CHUNKS)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _chat_client(auth).chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )

    assert "response_format" not in mock_client.stream.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_chat_drops_response_format_when_capability_fetch_fails(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A broken /models endpoint must degrade to prompt-only, not break chat."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(500))
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(_TEXT_CHUNKS)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _chat_client(auth).chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert "response_format" not in mock_client.stream.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_chat_defaults_to_chat_completions_on_malformed_catalog(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A /models body that isn't the expected shape degrades like a failed fetch."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(200, ["not", "a", "catalog"]))
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(_TEXT_CHUNKS)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _chat_client(auth).chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )

    assert mock_client.stream.call_args.args[1] == _CHAT_URL
    assert "response_format" not in mock_client.stream.call_args.kwargs["json"]
    assert result["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_chat_fetches_capabilities_once_for_the_process(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Endpoint routing needs the catalog for every chat, so it is fetched on
    the first one and cached — never a /models round-trip per request."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_response(200, _models_payload(None)))
    mock_client.stream = MagicMock(
        side_effect=lambda *a, **k: _stream_cm(200, _sse_lines(_TEXT_CHUNKS))
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        client = _chat_client(auth)
        await client.chat([{"role": "user", "content": "hi"}])
        await client.chat([{"role": "user", "content": "hi"}])

    assert mock_client.get.await_count == 1


@pytest.mark.asyncio
async def test_chat_marks_user_initiated_requests(auth: CopilotAuth, state_dir: Path) -> None:
    """A request ending with a user message is user-initiated (counts as a premium request)."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _offline_chat_client(auth).chat([{"role": "user", "content": "hi"}])

    headers = mock_client.stream.call_args.kwargs["headers"]
    assert headers["X-Initiator"] == "user"


@pytest.mark.asyncio
async def test_chat_marks_tool_followups_as_agent_initiated(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Agent-loop iterations after tool results must not consume premium requests."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    messages = [
        {"role": "user", "content": "read my memo"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "memo.md"}'}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "Buy milk"},
    ]
    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _offline_chat_client(auth).chat(messages)

    headers = mock_client.stream.call_args.kwargs["headers"]
    assert headers["X-Initiator"] == "agent"


@pytest.mark.asyncio
async def test_chat_honors_explicit_initiator_override(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Sub-agents (research, vision) serve an ongoing user prompt: they pass
    initiator='agent' even though their payload ends with a user message."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _offline_chat_client(auth).chat(
            [{"role": "user", "content": "question"}], initiator="agent"
        )

    headers = mock_client.stream.call_args.kwargs["headers"]
    assert headers["X-Initiator"] == "agent"


@pytest.mark.asyncio
async def test_chat_retries_truncated_stream(auth: CopilotAuth, state_dir: Path) -> None:
    """A stream that dies before delivering finish_reason must be retried,
    never returned as a partial message."""
    _write_oauth_token(state_dir, "oauth")
    truncated = [
        {"choices": [{"delta": {"role": "assistant", "content": "¡Claro!"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"pa'}}]}}]},
        # connection drops here: no finish_reason, no [DONE]
    ]
    complete = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"path": "a.md", "content": "x"}'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    truncated_lines = [f"data: {json.dumps(c)}" for c in truncated]  # no [DONE]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(
        side_effect=[_stream_cm(200, truncated_lines), _stream_cm(200, _sse_lines(complete))]
    )

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "go"}])

    assert mock_client.stream.call_count == 2
    tc = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(tc["function"]["arguments"]) == {"path": "a.md", "content": "x"}


@pytest.mark.asyncio
async def test_chat_retries_on_malformed_tool_arguments(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """Tool calls with unparseable JSON arguments must never be returned —
    they poison history and every later request 400s."""
    _write_oauth_token(state_dir, "oauth")
    malformed = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"path": "a.md'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    complete = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "write_file", "arguments": '{"path": "a.md"}'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(
        side_effect=[_stream_cm(200, _sse_lines(malformed)), _stream_cm(200, _sse_lines(complete))]
    )

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "go"}])

    assert mock_client.stream.call_count == 2
    tc = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(tc["function"]["arguments"]) == {"path": "a.md"}


@pytest.mark.asyncio
async def test_chat_normalizes_empty_tool_arguments(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """No-arg tools may stream an empty arguments string; store '{}' instead."""
    _write_oauth_token(state_dir, "oauth")
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "tc1", "type": "function",
             "function": {"name": "list_scheduled", "arguments": ""}}]},
            "finish_reason": "tool_calls"}]},
    ]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(chunks)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "list"}])

    tc = result["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["arguments"] == "{}"


@pytest.mark.asyncio
async def test_chat_retries_on_5xx(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")
    ok_chunks = [{"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}]
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(
        side_effect=[_stream_cm(502), _stream_cm(200, _sse_lines(ok_chunks))]
    )

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        result = await _offline_chat_client(auth).chat([{"role": "user", "content": "hello"}])

    assert result["choices"][0]["message"]["content"] == "hi"
    assert mock_client.stream.call_count == 2


@pytest.mark.asyncio
async def test_chat_gives_up_after_max_attempts(auth: CopilotAuth, state_dir: Path) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(side_effect=[_stream_cm(502) for _ in range(3)])

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="502"):
            await _offline_chat_client(auth).chat([{"role": "user", "content": "hello"}])

    assert mock_client.stream.call_count == 3


# ------------------------------------------------------------------
# Outage classification (CopilotUnavailableError)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_5xx_exhaustion_raises_unavailable(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(side_effect=[_stream_cm(502) for _ in range(3)])

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(CopilotUnavailableError):
            await _offline_chat_client(auth).chat([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_chat_network_exhaustion_raises_unavailable(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(side_effect=httpx.ConnectError("dns down"))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(CopilotUnavailableError):
            await _offline_chat_client(auth).chat([{"role": "user", "content": "hello"}])

    assert mock_client.stream.call_count == 3


@pytest.mark.asyncio
async def test_chat_4xx_is_not_unavailable(auth: CopilotAuth, state_dir: Path) -> None:
    """Rejected requests must not look like an outage — they would retry forever."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(side_effect=[_stream_cm(400)])

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await _offline_chat_client(auth).chat([{"role": "user", "content": "hello"}])

    assert not isinstance(excinfo.value, CopilotUnavailableError)


@pytest.mark.asyncio
async def test_bearer_network_exhaustion_raises_unavailable(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("dns down"))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(CopilotUnavailableError):
            await auth.get_bearer()

    assert mock_client.get.await_count == 3


# ------------------------------------------------------------------
# Runtime model switching
# ------------------------------------------------------------------

def test_set_model_changes_model_for_subsequent_chats(auth: CopilotAuth) -> None:
    client = CopilotClient(auth, "model-a")
    assert client.model == "model-a"

    client.set_model("model-b")

    assert client.model == "model-b"


# ------------------------------------------------------------------
# Dynamic model catalog (/models)
# ------------------------------------------------------------------

def _catalog_payload() -> dict:
    return {
        "data": [
            {
                "id": "claude-fable-5",
                "name": "Claude Fable 5",
                "vendor": "Anthropic",
                "model_picker_enabled": True,
                "policy": {"state": "enabled"},
                "capabilities": {"supports": {"structured_outputs": True}},
            },
            {
                "id": "grok-4.6",
                "name": "Grok 4.6",
                "vendor": "xAI",
                "model_picker_enabled": True,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_models_returns_filtered_catalog(auth: CopilotAuth) -> None:
    client = CopilotClient(auth, "claude-sonnet-5")
    auth._bearer = "bearer"
    auth._bearer_expires = time.time() + 3600
    response = MagicMock()
    response.json.return_value = _catalog_payload()
    response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        models = await client.list_models(["Anthropic"])

    assert [(m.id, m.name) for m in models] == [("claude-fable-5", "Claude Fable 5")]


@pytest.mark.asyncio
async def test_list_models_refreshes_capability_map(auth: CopilotAuth) -> None:
    """The catalog fetch carries the capability map too — one request, both caches."""
    client = CopilotClient(auth, "claude-sonnet-5")
    auth._bearer = "bearer"
    auth._bearer_expires = time.time() + 3600
    response = MagicMock()
    response.json.return_value = _catalog_payload()
    response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await client.list_models(["Anthropic"])

    assert client._capabilities is not None
    assert client._capabilities["claude-fable-5"].structured_outputs is True
    assert client._capabilities["grok-4.6"].structured_outputs is False


# ------------------------------------------------------------------
# Endpoint routing: /chat/completions vs /responses
# ------------------------------------------------------------------

_TOOL_SCHEMA = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read.", "parameters": {"type": "object"}},
}

_TEXT_OUTPUT = [
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}
]


def _catalog(endpoints: list[str] | None, structured_outputs: bool = True) -> dict:
    entry: dict = {
        "id": "test-model",
        "capabilities": {"supports": {"structured_outputs": structured_outputs}},
    }
    if endpoints is not None:
        entry["supported_endpoints"] = endpoints
    return {"data": [entry]}


def _responses_sse_lines(output: list[dict] | None) -> list[str]:
    """A /responses event stream; ``None`` output means cut off before completion."""
    lines = ["event: response.created", 'data: {"type":"response.created","response":{}}', ""]
    if output is None:
        return lines
    completed = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    }
    return lines + ["event: response.completed", f"data: {json.dumps(completed)}", ""]


def _routing_client(endpoints: list[str] | None, streams: list[MagicMock]) -> AsyncMock:
    mock_client = AsyncMock()
    if endpoints is None:
        mock_client.get = AsyncMock(return_value=_response(500))
    else:
        mock_client.get = AsyncMock(return_value=_response(200, _catalog(endpoints)))
    mock_client.stream = MagicMock(side_effect=streams)
    return mock_client


@pytest.mark.asyncio
async def test_chat_routes_responses_only_models_to_responses_endpoint(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        ["/responses", "ws:/responses"],
        [_stream_cm(200, _responses_sse_lines(_TEXT_OUTPUT))],
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _chat_client(auth).chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            tools=[_TOOL_SCHEMA],
        )

    args, kwargs = mock_client.stream.call_args
    assert args[1] == _RESPONSES_URL
    assert kwargs["json"]["instructions"] == "sys"
    assert kwargs["json"]["input"] == [{"role": "user", "content": "hi"}]
    assert "messages" not in kwargs["json"]
    assert kwargs["json"]["tools"][0]["name"] == "read_file"
    assert kwargs["headers"]["X-Initiator"] == "user"
    assert result["choices"][0]["message"]["content"] == "ok"
    assert result["usage"]["prompt_tokens"] == 3
    assert result["model"] == "test-model"


@pytest.mark.asyncio
async def test_chat_prefers_chat_completions_when_model_lists_both(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        ["/responses", "/chat/completions"], [_stream_cm(200, _sse_lines(_TEXT_CHUNKS))]
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _chat_client(auth).chat([{"role": "user", "content": "hi"}])

    args, kwargs = mock_client.stream.call_args
    assert args[1] == _CHAT_URL
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_chat_defaults_to_chat_completions_without_catalog(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A failed /models fetch must leave today's behavior intact."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(None, [_stream_cm(200, _sse_lines(_TEXT_CHUNKS))])

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        result = await _chat_client(auth).chat([{"role": "user", "content": "hi"}])

    assert mock_client.stream.call_args.args[1] == _CHAT_URL
    assert result["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_chat_defaults_to_chat_completions_for_unlisted_models(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A pinned config alias the catalog doesn't know keeps the chat endpoint."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=_response(200, {"data": [{"id": "other", "supported_endpoints": ["/responses"]}]})
    )
    mock_client.stream = MagicMock(return_value=_stream_cm(200, _sse_lines(_TEXT_CHUNKS)))

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _chat_client(auth).chat([{"role": "user", "content": "hi"}])

    assert mock_client.stream.call_args.args[1] == _CHAT_URL


@pytest.mark.asyncio
async def test_chat_sends_text_format_on_responses_endpoint(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        ["/responses"], [_stream_cm(200, _responses_sse_lines(_TEXT_OUTPUT))]
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _chat_client(auth).chat(
            [{"role": "user", "content": "hi"}], response_format=_RESPONSE_FORMAT
        )

    payload = mock_client.stream.call_args.kwargs["json"]
    assert payload["text"]["format"]["name"] == "job_close"
    assert "response_format" not in payload


@pytest.mark.asyncio
async def test_chat_strips_responses_reasoning_from_chat_payload(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """History produced on /responses must continue cleanly on a chat model."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        ["/chat/completions"], [_stream_cm(200, _sse_lines(_TEXT_CHUNKS))]
    )
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ],
        OUTPUT_KEY: [{"type": "reasoning", "encrypted_content": "ENC"}],
    }
    messages = [
        {"role": "user", "content": "hi"},
        assistant,
        {"role": "tool", "tool_call_id": "c", "content": "r"},
    ]

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        await _chat_client(auth).chat(messages)

    sent = mock_client.stream.call_args.kwargs["json"]["messages"]
    assert OUTPUT_KEY not in sent[1]
    assert sent[1]["tool_calls"] == assistant["tool_calls"]
    assert OUTPUT_KEY in assistant  # the caller's history is left untouched


@pytest.mark.asyncio
async def test_chat_retries_truncated_responses_stream(
    auth: CopilotAuth, state_dir: Path
) -> None:
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        ["/responses"],
        [
            _stream_cm(200, _responses_sse_lines(None)),
            _stream_cm(200, _responses_sse_lines(_TEXT_OUTPUT)),
        ],
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)), patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        result = await _chat_client(auth).chat([{"role": "user", "content": "hi"}])

    assert mock_client.stream.call_count == 2
    assert result["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_chat_does_not_refetch_capabilities_within_the_retry_delay(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A broken /models must not be hit on every request."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = _routing_client(
        None,
        [_stream_cm(200, _sse_lines(_TEXT_CHUNKS)), _stream_cm(200, _sse_lines(_TEXT_CHUNKS))],
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        client = _chat_client(auth)
        await client.chat([{"role": "user", "content": "hi"}])
        await client.chat([{"role": "user", "content": "hi"}])

    assert mock_client.get.await_count == 1


@pytest.mark.asyncio
async def test_chat_refetches_capabilities_after_a_failed_fetch(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """A failed catalog fetch must not pin a Responses-only model to the chat
    endpoint for the rest of the process: once the retry delay has passed the
    next chat fetches again and routes correctly."""
    _write_oauth_token(state_dir, "oauth")
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[_response(500), _response(200, _catalog(["/responses"]))]
    )
    mock_client.stream = MagicMock(
        side_effect=[
            _stream_cm(200, _sse_lines(_TEXT_CHUNKS)),
            _stream_cm(200, _responses_sse_lines(_TEXT_OUTPUT)),
        ]
    )

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        client = _chat_client(auth)
        await client.chat([{"role": "user", "content": "hi"}])
        client._capabilities_retry_at = 0.0  # the delay has elapsed
        await client.chat([{"role": "user", "content": "hi"}])

    assert mock_client.get.await_count == 2
    assert [c.args[1] for c in mock_client.stream.call_args_list] == [_CHAT_URL, _RESPONSES_URL]


@pytest.mark.asyncio
async def test_chat_round_trips_a_responses_turn_through_history(
    auth: CopilotAuth, state_dir: Path
) -> None:
    """The reply of one /responses turn, appended to history the way the agent
    does, replays verbatim in the next request ahead of the tool result."""
    _write_oauth_token(state_dir, "oauth")
    output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path": "a.md"}',
            "status": "completed",
        },
    ]
    mock_client = _routing_client(
        ["/responses"],
        [
            _stream_cm(200, _responses_sse_lines(output)),
            _stream_cm(200, _responses_sse_lines(_TEXT_OUTPUT)),
        ],
    )
    history = [{"role": "user", "content": "read a.md"}]

    with patch("httpx.AsyncClient", _mock_client_cls(mock_client)):
        client = _chat_client(auth)
        first = await client.chat(history, tools=[_TOOL_SCHEMA])
        reply = first["choices"][0]["message"]
        history.append(reply)
        history.append({"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"], "content": "# A"})
        second = await client.chat(history, tools=[_TOOL_SCHEMA])

    assert reply["tool_calls"][0]["function"]["name"] == "read_file"
    assert mock_client.stream.call_args_list[1].kwargs["json"]["input"] == [
        {"role": "user", "content": "read a.md"},
        *output,
        {"type": "function_call_output", "call_id": "call_1", "output": "# A"},
    ]
    assert second["choices"][0]["message"]["content"] == "ok"
