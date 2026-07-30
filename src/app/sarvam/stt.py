"""Saaras v3 speech-to-text — batch (REST) and streaming (WebSocket).

Two very different jobs:

* :func:`transcribe` — REST ``POST /speech-to-text``. Used by the post-call
  analytics pipeline, where accuracy matters and latency does not.
* :class:`StreamingSTT` — ``wss://api.sarvam.ai/speech-to-text/ws``. Used on the
  live call. With ``vad_signals=true`` the *server* tells us
  ``START_SPEECH`` / ``END_SPEECH``, which gives us endpointing and the
  barge-in trigger without running a local VAD.

Measured on the live API (2026-07-30): REST round trip 0.80 s for 8 s of Hindi
audio; the WS emits START_SPEECH, END_SPEECH, then the final transcript with
``language_code`` and ``language_probability``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

import websockets

from app.config import settings
from app.sarvam.client import SarvamError, auth_headers, post_multipart, ws_url

logger = logging.getLogger("emi.stt")

Mode = Literal["transcribe", "translate", "verbatim", "translit", "codemix"]


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None = None
    language_confidence: float | None = None
    request_id: str | None = None
    audio_duration_s: float | None = None
    processing_latency_s: float | None = None
    diarized: list[dict] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        """Gate on confidence: reprompt rather than act on a bad hypothesis."""
        if self.language_confidence is None:
            return True
        return self.language_confidence >= settings.stt_min_language_confidence


# --- batch -------------------------------------------------------------------
async def transcribe(
    wav: bytes,
    *,
    language_code: str = "unknown",
    mode: Mode = "transcribe",
    with_timestamps: bool = False,
) -> Transcript:
    """Transcribe a complete WAV clip.

    ``language_code="unknown"`` makes Saaras auto-detect, which is what you want
    when the call list says Hindi but the borrower answers in Tamil.
    """
    if settings.offline_mode:
        from app.sarvam import mock
        return mock.transcribe(wav, language_code=language_code)

    payload = await post_multipart(
        "/speech-to-text",
        files={"file": ("audio.wav", wav, "audio/wav")},
        data={
            "model": settings.stt_model,
            "mode": mode,
            "language_code": language_code,
            "with_timestamps": str(with_timestamps).lower(),
        },
    )
    return Transcript(
        text=(payload.get("transcript") or "").strip(),
        language=payload.get("language_code"),
        language_confidence=payload.get("language_probability"),
        request_id=payload.get("request_id"),
    )


async def transcribe_to_english(wav: bytes) -> Transcript:
    """Saaras ``mode="translate"`` — speech in any Indic language -> English text.

    One API call instead of transcribe-then-translate; used by the analytics
    pipeline so an English-only reviewer can read every call.
    """
    return await transcribe(wav, language_code="unknown", mode="translate")


# --- streaming ---------------------------------------------------------------
@dataclass(slots=True)
class SttEvent:
    """Normalised event from the streaming socket."""

    kind: Literal["speech_start", "speech_end", "transcript", "error"]
    text: str = ""
    language: str | None = None
    language_confidence: float | None = None
    request_id: str | None = None
    occurred_at: float | None = None
    detail: str | None = None


class StreamingSTT:
    """Live transcription socket for one call.

    Usage::

        async with StreamingSTT(language="hi-IN") as stt:
            asyncio.create_task(feeder(stt))       # stt.send_pcm(...)
            async for ev in stt.events():
                ...

    Audio in is mono 16-bit PCM at 16 kHz (8 kHz also supported, for a raw
    telephony leg). Frames are base64'd into a JSON envelope, as the AsyncAPI
    spec requires.
    """

    def __init__(
        self,
        *,
        language: str = "unknown",
        mode: Mode = "transcribe",
        sample_rate: int = 16000,
        high_vad_sensitivity: bool = False,
    ) -> None:
        self.language = language
        self.mode = mode
        self.sample_rate = sample_rate
        self.high_vad_sensitivity = high_vad_sensitivity
        self._ws: websockets.ClientConnection | None = None
        self._closed = False

    async def __aenter__(self) -> "StreamingSTT":
        url = ws_url(
            "/speech-to-text/ws",
            {
                "model": settings.stt_model,
                "mode": self.mode,
                # Always let Saaras detect: the call list is often wrong.
                "language-code": self.language,
                "sample_rate": self.sample_rate,
                "input_audio_codec": "pcm_s16le",
                "vad_signals": "true",
                "flush_signal": "true",
                "high_vad_sensitivity": str(self.high_vad_sensitivity).lower(),
            },
        )
        self._ws = await websockets.connect(
            url, additional_headers=auth_headers(), max_size=8 * 1024 * 1024, ping_interval=20
        )
        logger.debug("STT socket open (%s @ %d Hz)", settings.stt_model, self.sample_rate)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def send_pcm(self, pcm: bytes) -> None:
        """Push one audio window (100-320 ms works well)."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({
                "audio": {
                    "data": base64.b64encode(pcm).decode("ascii"),
                    "sample_rate": str(self.sample_rate),
                    "encoding": "audio/wav",
                }
            }))
        except websockets.ConnectionClosed:
            self._closed = True

    async def flush(self) -> None:
        """Force finalisation of the current utterance."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"type": "flush"}))
        except websockets.ConnectionClosed:
            self._closed = True

    async def events(self) -> AsyncIterator[SttEvent]:
        """Yield normalised events until the socket closes."""
        if self._ws is None:
            raise SarvamError("StreamingSTT used outside its context manager")
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                typ, data = msg.get("type"), msg.get("data") or {}

                if typ == "events":
                    signal = data.get("signal_type")
                    if signal == "START_SPEECH":
                        yield SttEvent("speech_start", occurred_at=data.get("occured_at"))
                    elif signal == "END_SPEECH":
                        yield SttEvent("speech_end", occurred_at=data.get("occured_at"))
                elif typ == "data":
                    text = (data.get("transcript") or "").strip()
                    if text:
                        yield SttEvent(
                            "transcript",
                            text=text,
                            language=data.get("language_code"),
                            language_confidence=data.get("language_probability"),
                            request_id=data.get("request_id"),
                        )
                elif typ == "error":
                    yield SttEvent("error", detail=str(data.get("error") or data))
        except websockets.ConnectionClosed as exc:
            logger.debug("STT socket closed: %s", exc)
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover - best effort teardown
                pass
            self._ws = None
