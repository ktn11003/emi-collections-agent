"""Bulbul v3 text-to-speech — streaming (WebSocket) and batch (REST).

Never synthesise a whole reply before playing it: that adds a full sentence of
latency. Two paths:

* :func:`stream_sentence` — ``wss://api.sarvam.ai/text-to-speech/ws``. Config,
  then text, then flush; mp3 chunks come back as they are produced. Measured
  **234 ms** to first audio chunk on the live API (2026-07-30).
* :func:`synthesize` — REST ``POST /text-to-speech``. Returns base64 audio in one
  shot. Supports codecs the socket does not, notably **mulaw @ 8 kHz**, which is
  exactly what an RTP leg wants — so this is the telephony path.

Both take a ``cancel`` event. Cancellation is the whole game for barge-in: when
the caller interrupts, the socket must stop within a frame or two.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Literal

import websockets

from app.config import settings
from app.sarvam.client import auth_headers, post_json, ws_url
from app.sarvam.voices import tts_language_for, voice_for

logger = logging.getLogger("emi.tts")

Codec = Literal["wav", "mp3", "linear16", "mulaw", "alaw", "opus", "flac", "aac"]

# Bulbul's first chunk lands in ~250-450 ms; allow headroom for a cold model.
FIRST_CHUNK_TIMEOUT_S = 12.0
# Once audio is flowing, chunks arrive back-to-back. If one is late by this much
# the utterance is over (belt-and-braces behind the "final" event).
INTER_CHUNK_TIMEOUT_S = 1.5
# Discarding leftovers from a barged-in utterance: they are already buffered, so
# this only needs to outlast one network hop.
DRAIN_TIMEOUT_S = 0.8


@dataclass(slots=True)
class AudioChunk:
    data: bytes
    content_type: str
    seq: int
    first: bool = False


# --- batch / telephony -------------------------------------------------------
async def synthesize(
    text: str,
    *,
    language: str = "hi-IN",
    speaker: str | None = None,
    sample_rate: int = 22050,
    codec: Codec = "wav",
    pace: float = 0.95,
    temperature: float = 0.6,
) -> bytes:
    """Synthesise one utterance and return the raw audio bytes.

    ``pace=0.95`` is deliberate: a slightly unhurried voice reads as empathetic,
    which matters when the subject is a missed payment.
    """
    lang = tts_language_for(language)
    if settings.offline_mode:
        from app.sarvam import mock
        return mock.synthesize(text, sample_rate=sample_rate)

    payload = await post_json(
        "/text-to-speech",
        {
            "text": text[:2500],  # bulbul:v3 hard limit
            "target_language_code": lang,
            "speaker": speaker or voice_for(lang),
            "model": settings.tts_model,
            "speech_sample_rate": sample_rate,
            "output_audio_codec": codec,
            "pace": pace,
            "temperature": temperature,
        },
    )
    audios = payload.get("audios") or []
    return base64.b64decode(audios[0]) if audios else b""


async def synthesize_for_telephony(text: str, *, language: str = "hi-IN",
                                   speaker: str | None = None) -> bytes:
    """8 kHz mu-law, ready to drop straight into RTP payloads."""
    return await synthesize(
        text, language=language, speaker=speaker, sample_rate=8000, codec="mulaw"
    )


# --- streaming ---------------------------------------------------------------
class StreamingTTS:
    """A reusable Bulbul socket.

    The socket is configured once per language and kept open for the call, so
    each sentence pays no handshake cost. Sending a new ``config`` mid-call
    switches voice/language (used when the borrower code-switches).
    """

    def __init__(self, *, language: str = "hi-IN", speaker: str | None = None,
                 pace: float = 0.95, min_buffer_size: int = 30) -> None:
        self.language = tts_language_for(language)
        self.speaker = speaker or voice_for(self.language)
        self.pace = pace
        self.min_buffer_size = min_buffer_size
        self._ws: websockets.ClientConnection | None = None
        self._seq = 0
        # Set when an utterance is abandoned mid-stream (barge-in). The socket
        # still holds that utterance's remaining chunks plus its "final" event,
        # and they must be discarded before the next sentence is sent -- else the
        # caller hears a fragment of the sentence they just interrupted.
        self._needs_drain = False

    async def __aenter__(self) -> "StreamingTTS":
        self._ws = await websockets.connect(
            # send_completion_event is what terminates an utterance: without it
            # the socket just goes quiet after the last chunk and the caller has
            # no way to know the sentence finished.
            ws_url("/text-to-speech/ws", {
                "model": settings.tts_model,
                "send_completion_event": "true",
            }),
            additional_headers=auth_headers(),
            max_size=8 * 1024 * 1024,
            ping_interval=20,
        )
        await self._send_config()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _send_config(self) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({
            "type": "config",
            "data": {
                "target_language_code": self.language,
                "speaker": self.speaker,
                "output_audio_codec": "mp3",   # the socket supports mp3 only
                "output_audio_bitrate": "128k",
                "pace": self.pace,
                # Flush early so the first chunk lands fast; 30 chars ~ a clause.
                "min_buffer_size": self.min_buffer_size,
                "max_chunk_length": 150,
            },
        }))

    def mark_interrupted(self) -> None:
        """Flag that an utterance was abandoned by the *consumer*.

        ``speak()`` sets this itself when it notices the cancel event, but a
        consumer that simply stops iterating (``return`` inside ``async for``)
        abandons the generator before that code runs — so it must say so
        explicitly, or the next sentence inherits this one's leftover chunks.
        """
        self._needs_drain = True

    async def switch_language(self, language: str, speaker: str | None = None) -> None:
        """Re-configure mid-call. Buffered text is flushed before the switch."""
        self.language = tts_language_for(language)
        self.speaker = speaker or voice_for(self.language)
        if self._ws is not None:
            await self._send_config()
            logger.info("TTS switched to %s / %s", self.language, self.speaker)

    async def _drain(self) -> int:
        """Discard everything left over from an abandoned utterance.

        Reads until the abandoned utterance's ``final`` event (or the socket goes
        quiet), throwing the audio away. Without this the next ``speak()`` would
        return stale chunks as if they were the new sentence — audible as the
        interrupted sentence resuming, and it also reports a bogus ~0 ms TTFA.
        """
        if self._ws is None or not self._needs_drain:
            self._needs_drain = False
            return 0

        discarded = 0
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=DRAIN_TIMEOUT_S)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            discarded += 1
            msg = json.loads(raw)
            if msg.get("type") == "event" and (msg.get("data") or {}).get("event_type") == "final":
                break
            if msg.get("type") == "error":
                break

        self._needs_drain = False
        if discarded:
            logger.debug("drained %d stale TTS messages after barge-in", discarded)
        return discarded

    async def speak(self, text: str, cancel: asyncio.Event | None = None) -> AsyncIterator[AudioChunk]:
        """Send one sentence, yield mp3 chunks as they arrive.

        Stops immediately if ``cancel`` is set — that is barge-in.

        **Not re-entrant.** The socket is one request/response channel, so two
        concurrent ``speak()`` calls interleave their ``recv()`` loops and steal
        each other's chunks. Callers must serialise; the session does this with
        its own lock around ``_stream_tts``.
        """
        if self._ws is None:
            return
        await self._drain()
        await self._ws.send(json.dumps({"type": "text", "data": {"text": text}}))
        await self._ws.send(json.dumps({"type": "flush"}))

        first = True
        # Generous while we wait for the model to warm up; tight once audio is
        # flowing, so a missing completion event costs a beat, not the turn.
        while True:
            if cancel is not None and cancel.is_set():
                logger.debug("TTS cancelled mid-utterance (barge-in)")
                # Anything still in flight belongs to this abandoned utterance.
                self._needs_drain = True
                return
            timeout = FIRST_CHUNK_TIMEOUT_S if first else INTER_CHUNK_TIMEOUT_S
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if first:
                    logger.warning("TTS produced no audio within %.1fs", timeout)
                else:
                    # Never saw "final"; the socket may still owe us messages.
                    self._needs_drain = True
                return
            except websockets.ConnectionClosed:
                return

            msg = json.loads(raw)
            typ = msg.get("type")
            if typ == "audio":
                data = msg.get("data") or {}
                b64 = data.get("audio")
                if not b64:
                    continue
                self._seq += 1
                yield AudioChunk(
                    data=base64.b64decode(b64),
                    content_type=data.get("content_type", "audio/mpeg"),
                    seq=self._seq,
                    first=first,
                )
                first = False
            elif typ == "error":
                logger.error("TTS error: %s", msg.get("data"))
                return
            elif typ == "event":
                # {"type":"event","data":{"event_type":"final", ...}} -- the
                # utterance is complete. Requires send_completion_event=true.
                if (msg.get("data") or {}).get("event_type") == "final":
                    return

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass
            self._ws = None


async def stream_sentence(
    text: str, *, language: str = "hi-IN", speaker: str | None = None,
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[AudioChunk]:
    """One-shot helper: open a socket, speak one sentence, close it.

    Convenient for scripts; the live call uses a long-lived
    :class:`StreamingTTS` instead so it does not pay per-sentence handshakes.
    """
    if settings.offline_mode:
        from app.sarvam import mock
        for chunk in mock.stream_sentence(text):
            if cancel is not None and cancel.is_set():
                return
            yield chunk
        return

    async with StreamingTTS(language=language, speaker=speaker) as tts:
        async for chunk in tts.speak(text, cancel):
            yield chunk
