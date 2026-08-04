"""Bulbul v3 as a Pipecat TTS service.

Pipecat's ``TTSService`` contract is: yield ``TTSAudioRawFrame``s containing
**16-bit signed PCM**. Bulbul's WebSocket can emit exactly that, so this service
has no decode step in the hot path.

That is worth spelling out, because it contradicts a comment in
``app/sarvam/tts.py`` ("the socket supports mp3 only", written 2026-07-30). Probed
again on 2026-08-04, the socket accepts several codecs:

    linear16   TTFA 0.41s   content_type: audio/pcm      <- used here
    mp3        TTFA 0.44s   content_type: audio/mpeg
    mulaw      TTFA 0.39s   content_type: audio/mulaw    <- streaming telephony
    wav        TTFA 0.14s   RIFF header on every chunk
    pcm_s16le  rejected

Taking ``linear16`` means no mp3 decoder, no ffmpeg/av dependency, and one less
buffer copy between Bulbul and the caller's ear.

Design notes carried over from the proven implementation in ``app/sarvam/tts.py``:

* One socket per call, not per sentence — a handshake costs ~0.15s and there is
  no reason to pay it every turn.
* ``min_buffer_size: 30`` flushes after roughly a clause instead of waiting for
  the whole sentence. This is the single biggest lever on time-to-first-audio.
* ``send_completion_event=true`` is required, or the socket simply goes quiet
  after the last chunk and there is no way to know the utterance ended.
* After an interruption the socket still holds the abandoned utterance's chunks
  **and** its ``final`` event. They must be cleared before the next sentence, or
  the caller hears the sentence they just interrupted resume.

Two things measured here that the original implementation gets differently:

1. Clearing by *draining* costs 2.49s, because Bulbul only sends ``final`` once it
   has finished synthesising the sentence you interrupted. Dropping the socket and
   reconnecting costs 0.53s. See :meth:`SarvamTTSService._clear_stale`.
2. ``run_tts`` must not yield inside ``finally``. Barge-in makes the consumer
   break out of its ``async for``, which throws ``GeneratorExit``; yielding then
   raises "async generator ignored GeneratorExit" and the turn deadlocks.

Measured 2026-08-04, live API: TTFA 0.21-0.28s on a warm socket, 0.53s on the
turn immediately after a barge-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import AsyncGenerator

import websockets
from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    # Pipecat 1.7 renamed this: it is InterruptionFrame, not
    # StartInterruptionFrame as in the 0.x examples still common online.
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService

SARVAM_WS = "wss://api.sarvam.ai/text-to-speech/ws"

# Bulbul's first chunk lands in 0.14-0.45s measured; allow headroom for a cold
# model without letting a dead socket hold the turn open indefinitely.
FIRST_CHUNK_TIMEOUT_S = 10.0
# Once audio flows, chunks are back-to-back. A gap this long means the utterance
# is over -- belt and braces behind the completion event.
INTER_CHUNK_TIMEOUT_S = 1.5
# How long to wait for an interrupted utterance's "final" event before giving up
# and reconnecting instead. Short on purpose: this sits directly in front of the
# reply after a barge-in. See _clear_stale.
QUICK_DRAIN_BUDGET_S = 0.15


class SarvamTTSService(TTSService):
    """Streaming Bulbul v3 TTS over a persistent WebSocket.

    Args:
        api_key: Sarvam subscription key.
        voice_id: Bulbul speaker, e.g. ``priya`` (hi-IN) or ``kavitha`` (ta-IN).
        language: BCP-47 target, e.g. ``hi-IN``.
        sample_rate: Output rate. Pipecat resamples downstream if the transport
            wants something else, but matching the transport avoids that work.
        pace: 0.95 reads as unhurried, which matters when the subject is a missed
            payment. Carried over from the existing agent deliberately.
        min_buffer_size: Characters buffered before Bulbul starts synthesising.
            Lower is faster to first audio; too low and prosody suffers.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = "priya",
        language: str = "hi-IN",
        model: str = "bulbul:v3",
        sample_rate: int = 24000,
        pace: float = 0.95,
        min_buffer_size: int = 30,
        max_chunk_length: int = 150,
        **kwargs,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        # TTSService.sample_rate stays None until the pipeline delivers a
        # StartFrame, which resolves the negotiated transport rate. Bulbul
        # rejects a null speech_sample_rate with 422 "Input parameters has to be
        # a valid dictionary", so keep the constructor value as a fallback and
        # the service works standalone (tests, benchmarks) as well as in a pipeline.
        self._sample_rate_hint = sample_rate
        self._api_key = api_key
        self._voice_id = voice_id
        self._language = language
        self._model = model
        self._pace = pace
        self._min_buffer_size = min_buffer_size
        self._max_chunk_length = max_chunk_length

        self._ws: websockets.ClientConnection | None = None
        self._needs_drain = False
        # The socket is a single request/response channel: two concurrent
        # run_tts() calls would interleave their recv() loops and steal each
        # other's audio. Pipecat should serialise, but the lock makes it certain.
        self._lock = asyncio.Lock()

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def _rate(self) -> int:
        """The rate to actually use: pipeline-negotiated if available, else the hint."""
        return self.sample_rate or self._sample_rate_hint

    # --- lifecycle -----------------------------------------------------------
    async def start(self, frame: Frame) -> None:
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: Frame) -> None:
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: Frame) -> None:
        await super().cancel(frame)
        await self._disconnect()

    async def _connect(self) -> None:
        if self._ws is not None:
            return
        url = f"{SARVAM_WS}?model={self._model}&send_completion_event=true"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"api-subscription-key": self._api_key},
                max_size=8 * 1024 * 1024,
                ping_interval=20,
            )
        except Exception as e:
            logger.error(f"{self}: could not open Bulbul socket: {e}")
            self._ws = None
            return
        await self._send_config()
        logger.debug(f"{self}: Bulbul socket open ({self._language}/{self._voice_id})")

    async def _disconnect(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._needs_drain = False

    async def _send_config(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "config",
            "data": {
                "target_language_code": self._language,
                "speaker": self._voice_id,
                # linear16 => raw 16-bit PCM, which is what TTSAudioRawFrame
                # carries. Anything else would need decoding here.
                "output_audio_codec": "linear16",
                "speech_sample_rate": self._rate,
                "pace": self._pace,
                "min_buffer_size": self._min_buffer_size,
                "max_chunk_length": self._max_chunk_length,
            },
        }))

    # --- voice switching -----------------------------------------------------
    async def switch_voice(self, *, language: str, voice_id: str) -> None:
        """Re-configure mid-call when the borrower code-switches.

        Sarvam's language ID runs upstream of this service; when it reports a new
        language the pipeline calls this and the next sentence is spoken in the
        new voice on the same socket.
        """
        self._language = language
        self._voice_id = voice_id
        await self._send_config()
        logger.info(f"{self}: switched to {language}/{voice_id}")

    # --- interruption --------------------------------------------------------
    async def process_frame(self, frame: Frame, direction) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # Whatever is still in flight belongs to the abandoned utterance.
            self._needs_drain = True

    async def _clear_stale(self) -> str:
        """Get rid of an interrupted utterance's leftovers before the next turn.

        Two ways to do this, and the fast one is not the obvious one.

        *Draining* — read until the abandoned utterance's ``final`` event — sounds
        cheapest, but Bulbul keeps synthesising the sentence you interrupted and
        only sends ``final`` when it is done. Measured on 2026-08-04 that put
        **2.49s** in front of the next reply, i.e. the pause lands exactly when
        the borrower has just cut the agent off and expects a fast answer.

        *Reconnecting* throws the whole socket away. The handshake costs ~0.15s
        and the stale utterance dies with the connection.

        So: try a very short drain in case ``final`` is already sitting in the
        buffer (cheap, common for short utterances), and if it is not, reconnect.
        """
        if not self._needs_drain:
            return "nothing-to-clear"
        if self._ws is None:
            self._needs_drain = False
            await self._connect()
            return "reconnected"

        deadline = asyncio.get_running_loop().time() + QUICK_DRAIN_BUDGET_S
        discarded = 0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed:
                self._ws = None
                break
            discarded += 1
            try:
                msg = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                continue
            if msg.get("type") == "error":
                break
            if (msg.get("type") == "event"
                    and (msg.get("data") or {}).get("event_type") == "final"):
                self._needs_drain = False
                logger.debug(f"{self}: cleared {discarded} stale messages by draining")
                return "drained"

        # final never arrived inside the budget: the model is still working on the
        # abandoned sentence. Cheaper to drop the socket than to wait it out.
        self._needs_drain = False
        await self._disconnect()
        await self._connect()
        logger.debug(f"{self}: reconnected after interruption (discarded {discarded})")
        return "reconnected"

    # --- synthesis -----------------------------------------------------------
    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        if not text.strip():
            return

        async with self._lock:
            if self._ws is None:
                await self._connect()
            if self._ws is None:
                yield ErrorFrame("Bulbul socket unavailable")
                return

            await self._clear_stale()

            # Whether the utterance ran to its natural end. Anything else -- a
            # timeout, a closed socket, or the consumer abandoning the generator
            # on barge-in -- means the socket may still owe us chunks plus a
            # "final" event, and the next turn has to drain them first.
            completed = False
            try:
                await self.start_ttfb_metrics()
                yield TTSStartedFrame()
                await self._ws.send(json.dumps({"type": "text", "data": {"text": text}}))
                await self._ws.send(json.dumps({"type": "flush"}))

                first = True
                while True:
                    timeout = FIRST_CHUNK_TIMEOUT_S if first else INTER_CHUNK_TIMEOUT_S
                    try:
                        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        if first:
                            logger.warning(f"{self}: no audio within {timeout}s")
                        else:
                            # Never saw the completion event; the socket may owe
                            # us more messages, so the next turn must drain.
                            self._needs_drain = True
                        break
                    except websockets.ConnectionClosed:
                        self._ws = None
                        break

                    try:
                        msg = json.loads(raw) if isinstance(raw, str) else {}
                    except Exception:
                        continue

                    typ = msg.get("type")
                    if typ == "audio":
                        b64 = (msg.get("data") or {}).get("audio")
                        if not b64:
                            continue
                        if first:
                            await self.stop_ttfb_metrics()
                            first = False
                        yield TTSAudioRawFrame(
                            audio=base64.b64decode(b64),
                            sample_rate=self._rate,
                            num_channels=1,
                        )
                    elif typ == "error":
                        logger.error(f"{self}: Bulbul error {msg.get('data')}")
                        yield ErrorFrame(f"Bulbul: {msg.get('data')}")
                        break
                    elif typ == "event":
                        if (msg.get("data") or {}).get("event_type") == "final":
                            completed = True
                            break
            finally:
                # NOTHING MAY YIELD IN HERE. On barge-in the consumer breaks out
                # of its `async for`, which throws GeneratorExit into this
                # generator; yielding during that raises
                #   RuntimeError: async generator ignored GeneratorExit
                # and the coroutine then hangs. That is the barge-in path, so the
                # bug would fire on the feature this agent is judged on.
                if not completed:
                    self._needs_drain = True
                try:
                    await self.stop_ttfb_metrics()
                except Exception:
                    pass

            # Reached only when the utterance finished on its own terms.
            yield TTSStoppedFrame()
