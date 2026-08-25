"""Tests for audio transcription via ElevenLabs Scribe."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.transcribe import _ENDPOINT, Transcriber, TranscriptionError


@pytest.fixture
def transcriber() -> Transcriber:
    return Transcriber(api_key="xi-secret")


@respx.mock
async def test_transcribe_posts_audio_and_returns_text(transcriber: Transcriber) -> None:
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"text": "  hello world \n"})
    )

    text = await transcriber.transcribe(b"OGGDATA")

    assert text == "hello world"
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == "xi-secret"
    body = request.read()
    assert b'name="model_id"' in body
    assert b"scribe_v2" in body
    assert b'name="file"' in body
    assert b"OGGDATA" in body


@respx.mock
async def test_transcribe_auth_error_mentions_api_key(transcriber: Transcriber) -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(401, text="Unauthorized"))

    with pytest.raises(TranscriptionError, match="ELEVENLABS_API_KEY"):
        await transcriber.transcribe(b"OGG")


@respx.mock
async def test_transcribe_rate_limit_error(transcriber: Transcriber) -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(429, text="rate limited"))

    with pytest.raises(TranscriptionError, match="[Rr]ate limit"):
        await transcriber.transcribe(b"OGG")


@respx.mock
async def test_transcribe_other_http_error(transcriber: Transcriber) -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(400, text="bad audio"))

    with pytest.raises(TranscriptionError, match="HTTP 400"):
        await transcriber.transcribe(b"OGG")


@respx.mock
async def test_transcribe_empty_transcript_raises(transcriber: Transcriber) -> None:
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json={"text": "  "}))

    with pytest.raises(TranscriptionError, match="empty"):
        await transcriber.transcribe(b"OGG")


@respx.mock
async def test_transcribe_retries_on_5xx(transcriber: Transcriber) -> None:
    route = respx.post(_ENDPOINT).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"text": "hi"}),
        ]
    )

    with patch("assistant.copilot.asyncio.sleep", new=AsyncMock()):
        text = await transcriber.transcribe(b"OGG")

    assert text == "hi"
    assert route.call_count == 2


@respx.mock
async def test_transcribe_wraps_network_errors(transcriber: Transcriber) -> None:
    respx.post(_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))

    with patch("assistant.copilot.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(TranscriptionError, match="boom"):
            await transcriber.transcribe(b"OGG")
