"""Tests for audio transcription via GitHub Models (Phi-4-multimodal)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.transcribe import (
    _MAX_CHUNK_BYTES,
    _MP3_BYTES_PER_SECOND,
    Transcriber,
    TranscriptionError,
    _split_into_chunks,
    convert_to_mp3,
)


def _response(status_code: int, payload: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    r.text = text
    return r


def _mock_client_cls(mock_client: AsyncMock) -> MagicMock:
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    cls = MagicMock()
    cls.return_value = mock_client
    return cls


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture
def transcriber() -> Transcriber:
    return Transcriber(token="gh-models-token")


# ------------------------------------------------------------------
# API request/response
# ------------------------------------------------------------------

async def test_transcribe_posts_input_audio_and_returns_text(transcriber: Transcriber) -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_response(200, _chat_payload("  hello world \n")))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3DATA")),
    ):
        text = await transcriber.transcribe(b"OGGDATA")

    assert text == "hello world"
    call = mock_client.post.call_args
    assert call.args[0] == "https://models.github.ai/inference/chat/completions"
    assert call.kwargs["headers"]["Authorization"] == "Bearer gh-models-token"
    payload = call.kwargs["json"]
    assert payload["model"] == "microsoft/Phi-4-multimodal-instruct"
    parts = payload["messages"][-1]["content"]
    audio_parts = [p for p in parts if p["type"] == "input_audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0]["input_audio"]["format"] == "mp3"
    assert base64.b64decode(audio_parts[0]["input_audio"]["data"]) == b"MP3DATA"


async def test_transcribe_splits_large_audio_and_joins_transcripts(
    transcriber: Transcriber,
) -> None:
    """Audio over the request-size budget is chunked; transcripts are joined."""
    big = b"x" * (_MAX_CHUNK_BYTES * 2 + 1)  # forces 3 chunks

    async def fake_convert(data: bytes, seek: float | None = None, duration: float | None = None) -> bytes:
        if seek is None:
            return big
        return f"CHUNK@{seek:.3f}".encode()

    responses = [_response(200, _chat_payload(t)) for t in ("uno", "dos", "tres")]
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=responses)

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=fake_convert),
    ):
        text = await transcriber.transcribe(b"OGGDATA")

    assert text == "uno dos tres"
    assert mock_client.post.await_count == 3


async def test_transcribe_auth_error_mentions_github_token(transcriber: Transcriber) -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_response(401, text="Unauthorized"))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3")),
    ):
        with pytest.raises(TranscriptionError, match="GITHUB_TOKEN"):
            await transcriber.transcribe(b"OGG")


async def test_transcribe_rate_limit_error(transcriber: Transcriber) -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_response(429, text="rate limited"))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3")),
    ):
        with pytest.raises(TranscriptionError, match="[Rr]ate limit"):
            await transcriber.transcribe(b"OGG")


async def test_transcribe_empty_transcript_raises(transcriber: Transcriber) -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_response(200, _chat_payload("")))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3")),
    ):
        with pytest.raises(TranscriptionError, match="empty"):
            await transcriber.transcribe(b"OGG")


async def test_transcribe_retries_on_5xx(transcriber: Transcriber) -> None:
    ok = _response(200, _chat_payload("hi"))
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[_response(502), ok])

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3")),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        text = await transcriber.transcribe(b"OGG")

    assert text == "hi"
    assert mock_client.post.await_count == 2


async def test_transcribe_wraps_network_errors(transcriber: Transcriber) -> None:
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

    with (
        patch("httpx.AsyncClient", _mock_client_cls(mock_client)),
        patch("assistant.transcribe.convert_to_mp3", new=AsyncMock(return_value=b"MP3")),
        patch("assistant.copilot.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(TranscriptionError, match="boom"):
            await transcriber.transcribe(b"OGG")


# ------------------------------------------------------------------
# Chunking
# ------------------------------------------------------------------

async def test_split_small_audio_passes_through() -> None:
    mp3 = b"m" * _MAX_CHUNK_BYTES
    with patch("assistant.transcribe.convert_to_mp3", new=AsyncMock()) as mock_convert:
        chunks = await _split_into_chunks(mp3)

    assert chunks == [mp3]
    mock_convert.assert_not_awaited()


async def test_split_large_audio_reencodes_even_time_slices() -> None:
    # 3× the budget → 3 chunks covering the full duration back to back
    mp3 = b"m" * (_MAX_CHUNK_BYTES * 3)
    total_seconds = len(mp3) / _MP3_BYTES_PER_SECOND
    mock_convert = AsyncMock(side_effect=[b"C1", b"C2", b"C3"])

    with patch("assistant.transcribe.convert_to_mp3", new=mock_convert):
        chunks = await _split_into_chunks(mp3)

    assert chunks == [b"C1", b"C2", b"C3"]
    chunk_seconds = total_seconds / 3
    for i, call in enumerate(mock_convert.await_args_list):
        assert call.args[0] == mp3
        assert call.kwargs["seek"] == pytest.approx(i * chunk_seconds)
        assert call.kwargs["duration"] == pytest.approx(chunk_seconds)


# ------------------------------------------------------------------
# ffmpeg conversion
# ------------------------------------------------------------------

def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


async def test_convert_to_mp3_pipes_through_ffmpeg() -> None:
    proc = _mock_proc(0, stdout=b"MP3DATA")
    with patch(
        "assistant.transcribe.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        out = await convert_to_mp3(b"OGGDATA")

    assert out == b"MP3DATA"
    argv = list(mock_exec.call_args.args)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-b:a") + 1] == "16k"
    assert argv[argv.index("-f") + 1] == "mp3"
    assert "-ss" not in argv
    proc.communicate.assert_awaited_once_with(input=b"OGGDATA")


async def test_convert_to_mp3_seek_and_duration_args() -> None:
    proc = _mock_proc(0, stdout=b"CHUNK")
    with patch(
        "assistant.transcribe.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        out = await convert_to_mp3(b"MP3", seek=5.0, duration=2.5)

    assert out == b"CHUNK"
    argv = list(mock_exec.call_args.args)
    assert argv[argv.index("-ss") + 1] == "5.000"
    assert argv[argv.index("-t") + 1] == "2.500"


async def test_convert_to_mp3_missing_ffmpeg() -> None:
    with patch(
        "assistant.transcribe.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("ffmpeg")),
    ):
        with pytest.raises(TranscriptionError, match="ffmpeg"):
            await convert_to_mp3(b"OGG")


async def test_convert_to_mp3_ffmpeg_failure() -> None:
    proc = _mock_proc(1, stderr=b"Invalid data found")
    with patch(
        "assistant.transcribe.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ):
        with pytest.raises(TranscriptionError, match="Invalid data found"):
            await convert_to_mp3(b"NOTAUDIO")
