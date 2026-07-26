"""Audio transcription via GitHub Models (Phi-4-multimodal).

The Copilot chat API has no audio modality, so voice notes go through
GitHub Models instead: https://models.github.ai (auth: a fine-grained PAT
with the `models: read` permission, i.e. GITHUB_TOKEN).

GitHub Models rejects request bodies over 8000 tokens, and the base64
audio counts toward that cap — so audio is compressed to low-bitrate mono
MP3 with ffmpeg (uncompressed WAV overflows the cap in under a second of
speech), and anything still over the per-request budget is split into
time slices that are transcribed one by one and joined.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
from typing import Any

import httpx

from .copilot import send_with_retries

logger = logging.getLogger(__name__)

_ENDPOINT = "https://models.github.ai/inference/chat/completions"
_MODEL = "microsoft/Phi-4-multimodal-instruct"
_PROMPT = "Transcribe this audio verbatim. Reply with only the transcript text."
_TIMEOUT = 60.0

_MP3_BITRATE = "16k"  # CBR, so byte size maps linearly to duration
_MP3_BYTES_PER_SECOND = 16_000 // 8
# Keeps each request's base64 audio (~4/3 × raw bytes) comfortably under the
# 8000-token body cap even if the gateway tokenizes at ~3 chars per token.
_MAX_CHUNK_BYTES = 12_000

_FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
_FFMPEG_MP3_OUT = ["-ac", "1", "-ar", "16000", "-b:a", _MP3_BITRATE, "-f", "mp3", "pipe:1"]


class TranscriptionError(RuntimeError):
    """Audio could not be converted or transcribed."""


async def convert_to_mp3(
    data: bytes, seek: float | None = None, duration: float | None = None
) -> bytes:
    """Convert any ffmpeg-readable audio to 16 kHz mono 16 kbps MP3.

    `seek`/`duration` (seconds) select a slice of the input, used to split
    long audio into request-sized chunks.
    """
    argv = list(_FFMPEG_BASE)
    if seek is not None:
        argv += ["-ss", f"{seek:.3f}"]
    if duration is not None:
        argv += ["-t", f"{duration:.3f}"]
    argv += _FFMPEG_MP3_OUT
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise TranscriptionError(
            "ffmpeg is not installed — it is required to convert Telegram audio"
        ) from None
    stdout, stderr = await proc.communicate(input=data)
    if proc.returncode != 0 or not stdout:
        detail = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise TranscriptionError(f"audio conversion failed: {detail}")
    return stdout


async def _split_into_chunks(mp3: bytes) -> list[bytes]:
    """Split MP3 audio into slices that each fit the per-request size budget."""
    if len(mp3) <= _MAX_CHUNK_BYTES:
        return [mp3]
    total_seconds = len(mp3) / _MP3_BYTES_PER_SECOND
    count = math.ceil(len(mp3) / _MAX_CHUNK_BYTES)
    chunk_seconds = total_seconds / count
    return [
        await convert_to_mp3(mp3, seek=i * chunk_seconds, duration=chunk_seconds)
        for i in range(count)
    ]


class Transcriber:
    """Transcribes audio bytes to text using GitHub Models."""

    def __init__(self, token: str, model: str = _MODEL) -> None:
        self._token = token
        self._model = model

    async def transcribe(self, audio: bytes) -> str:
        """Return the transcript of `audio` (any ffmpeg-readable format)."""
        mp3 = await convert_to_mp3(audio)
        chunks = await _split_into_chunks(mp3)
        if len(chunks) > 1:
            logger.info("transcribing audio in %d chunks", len(chunks))
        async with httpx.AsyncClient() as client:
            texts = [await self._transcribe_chunk(client, chunk) for chunk in chunks]
        transcript = " ".join(t for t in texts if t).strip()
        if not transcript:
            raise TranscriptionError("transcription came back empty")
        return transcript

    async def _transcribe_chunk(self, client: httpx.AsyncClient, mp3: bytes) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(mp3).decode(),
                                "format": "mp3",
                            },
                        },
                    ],
                }
            ],
        }

        try:
            r = await send_with_retries(
                lambda: client.post(
                    _ENDPOINT,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=_TIMEOUT,
                ),
                "GitHub Models transcription",
            )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"GitHub Models request failed: {exc}") from exc

        if r.status_code in (401, 403):
            raise TranscriptionError(
                "GitHub Models rejected the token — check that GITHUB_TOKEN is set "
                "and has the `models: read` permission"
            )
        if r.status_code == 429:
            raise TranscriptionError(
                "GitHub Models rate limit reached — try again in a little while"
            )
        if r.status_code >= 400:
            logger.error("GitHub Models error %d: %.500s", r.status_code, r.text)
            raise TranscriptionError(f"GitHub Models returned HTTP {r.status_code}")

        content = r.json()["choices"][0]["message"].get("content") or ""
        return content.strip()
