"""Audio transcription via ElevenLabs Scribe.

The Copilot chat API has no audio modality, so voice notes go through the
ElevenLabs speech-to-text API instead (auth: an API key from elevenlabs.io,
read from ELEVENLABS_API_KEY). Scribe accepts Telegram's OGG/Opus voice
notes — and every other common audio format — directly, so no local
conversion or chunking is needed.
"""

from __future__ import annotations

import logging

import httpx

from .copilot import send_with_retries

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
_MODEL = "scribe_v2"
_TIMEOUT = 120.0


class TranscriptionError(RuntimeError):
    """Audio could not be transcribed."""


def _detail_status(r: httpx.Response) -> str:
    """The `detail.status` code of an ElevenLabs error body, or ""."""
    try:
        detail = r.json().get("detail")
    except ValueError:
        return ""
    if isinstance(detail, dict):
        return str(detail.get("status") or "")
    return ""


class Transcriber:
    """Transcribes audio bytes to text using ElevenLabs Scribe."""

    def __init__(self, api_key: str, model: str = _MODEL) -> None:
        # Stripped defensively: stray whitespace from a copy-pasted .env value
        # would otherwise become an illegal header at send time.
        self._api_key = api_key.strip()
        self._model = model

    async def transcribe(self, audio: bytes) -> str:
        """Return the transcript of `audio` (any common audio format)."""
        async with httpx.AsyncClient() as client:
            try:
                r = await send_with_retries(
                    lambda: client.post(
                        _ENDPOINT,
                        headers={"xi-api-key": self._api_key},
                        data={"model_id": self._model},
                        files={"file": ("audio", audio)},
                        timeout=_TIMEOUT,
                    ),
                    "ElevenLabs transcription",
                )
            except httpx.HTTPError as exc:
                raise TranscriptionError(f"ElevenLabs request failed: {exc}") from exc

        if r.status_code >= 400:
            logger.error("ElevenLabs error %d: %.500s", r.status_code, r.text)
            # Exhausted credits arrive as a 401 with detail.status
            # "quota_exceeded" — without this check they would be
            # misreported as a bad API key.
            if _detail_status(r) == "quota_exceeded":
                raise TranscriptionError(
                    "ElevenLabs transcription credits are used up — top up or "
                    "wait for the monthly reset"
                )
            if r.status_code in (401, 403):
                raise TranscriptionError(
                    "ElevenLabs rejected the API key — check that ELEVENLABS_API_KEY "
                    "is set to a valid key from elevenlabs.io"
                )
            if r.status_code == 429:
                raise TranscriptionError(
                    "ElevenLabs rate limit reached — try again in a little while"
                )
            raise TranscriptionError(f"ElevenLabs returned HTTP {r.status_code}")

        try:
            transcript = (r.json().get("text") or "").strip()
        except ValueError:
            logger.error("ElevenLabs non-JSON 200 body: %.500s", r.text)
            raise TranscriptionError(
                "ElevenLabs returned an unexpected response"
            ) from None
        if not transcript:
            raise TranscriptionError("transcription came back empty")
        return transcript
