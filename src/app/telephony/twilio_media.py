"""Real phone calls: Twilio / Plivo / Exotel Media Streams bridge.

This is the production path the browser demo stands in for. Nothing about the
agent changes — :class:`~app.agent.session.CallSession` is transport-agnostic.
Only two things differ, and both are audio plumbing:

* **In:** the carrier sends base64 G.711 mu-law at 8 kHz in 20 ms frames. We
  mu-law-decode, condition (high-pass + AGC), and resample 8 -> 16 kHz for
  Saaras. See :func:`app.audio.telephony_to_stt`.
* **Out:** we ask Bulbul for **mu-law @ 8 kHz directly** (the REST endpoint
  supports that codec, the WebSocket does not) and hand frames straight back —
  no transcoding on the hot path.

The SIP ``Call-ID`` becomes the CDR's ``correlation_id``, so one trace spans
SIP -> media -> STT -> LLM -> TTS -> tools.

Status: written against the documented Twilio Media Streams protocol and the
verified Sarvam mu-law path, but **not exercised against a live carrier** — that
needs a paid CPaaS account and a public HTTPS URL. See docs/telephony.md for the
15-minute activation checklist.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.agent.session import CallBlocked, CallSession
from app.audio import telephony_to_stt
from app.config import settings
from app.db.models import Channel
from app.sarvam import tts as tts_api
from app.steplog import step

logger = logging.getLogger("emi.telephony")
router = APIRouter(prefix="/telephony")

# 8 kHz mu-law, 20 ms frames = 160 bytes. Batch to ~100 ms before hitting STT.
FRAMES_PER_WINDOW = 5
MULAW_FRAME_BYTES = 160


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    """TwiML answer: open a bidirectional media stream to our WebSocket."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    to_number = str(form.get("To", ""))
    loan_id = request.query_params.get("loan_id", "")

    base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

    step("call.started", None, provider="twilio", call_sid=call_sid, to=to_number, loan_id=loan_id)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_base}/telephony/twilio/stream">
      <Parameter name="loan_id" value="{loan_id}" />
      <Parameter name="call_sid" value="{call_sid}" />
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/twilio/stream")
async def twilio_media_stream(ws: WebSocket) -> None:
    """Bidirectional mu-law media stream <-> the voice agent."""
    await ws.accept()

    stream_sid: str = ""
    session: CallSession | None = None
    send_lock = asyncio.Lock()
    inbound = bytearray()

    async def emit(event: str, payload: dict[str, Any]) -> None:
        """Agent events -> carrier media frames (audio) or logs (everything else)."""
        if event == "audio":
            # The agent sent mp3 (WS transport) or wav; for a carrier leg we want
            # mu-law. Configure TTS_TRANSPORT=rest for telephony so
            # synthesize_for_telephony() is used and this is already mu-law.
            async with send_lock:
                try:
                    await ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload["b64"]},
                    }))
                except (WebSocketDisconnect, RuntimeError):
                    pass
        elif event == "barge_in":
            # Tell the carrier to drop everything it has already buffered,
            # otherwise the caller keeps hearing the bot after we stopped.
            async with send_lock:
                try:
                    await ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                except (WebSocketDisconnect, RuntimeError):
                    pass
            step("agent.barge_in", session.call_id if session else None, transport="twilio")

    try:
        while True:
            message = json.loads(await ws.receive_text())
            event = message.get("event")

            if event == "connected":
                logger.info("twilio media stream connected")

            elif event == "start":
                start = message.get("start") or {}
                stream_sid = start.get("streamSid", "")
                params = start.get("customParameters") or {}
                loan_id = params.get("loan_id", "")
                # The carrier's call id is our correlation key end to end.
                correlation_id = params.get("call_sid") or start.get("callSid") or stream_sid

                session = CallSession(
                    loan_id=loan_id,
                    emit=emit,
                    channel=Channel.PSTN,
                    correlation_id=correlation_id,
                )
                try:
                    await session.start()
                except CallBlocked as blocked:
                    logger.warning("call blocked: %s", blocked)
                    step("dialer.precheck", None, blocked=True, detail=str(blocked))
                    await ws.close(code=1008)
                    return

            elif event == "media" and session is not None:
                frame = base64.b64decode(message["media"]["payload"])
                inbound.extend(frame)
                if len(inbound) >= MULAW_FRAME_BYTES * FRAMES_PER_WINDOW:
                    window = bytes(inbound)
                    inbound.clear()
                    # mu-law 8 kHz -> conditioned 16 kHz PCM for Saaras.
                    await session.push_audio(telephony_to_stt(window))

            elif event == "dtmf" and session is not None:
                # RFC 2833 telephone-event, surfaced by the carrier.
                digit = (message.get("dtmf") or {}).get("digit", "")
                step("media.audio_in", session.call_id, dtmf=digit)

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("twilio stream disconnected")
    except Exception:  # noqa: BLE001
        logger.exception("twilio media stream error")
        step("error", session.call_id if session else None, source="twilio_stream")
    finally:
        if session is not None:
            await session.stop()


async def synthesize_mulaw_frames(text: str, language: str = "hi-IN") -> list[bytes]:
    """Bulbul -> 8 kHz mu-law, split into 20 ms RTP-sized frames.

    Used by the telephony path instead of the mp3 WebSocket: the carrier wants
    mu-law, and Bulbul's REST endpoint produces it natively, so no transcode.
    """
    audio = await tts_api.synthesize_for_telephony(text, language=language)
    return [audio[i : i + MULAW_FRAME_BYTES] for i in range(0, len(audio), MULAW_FRAME_BYTES)]
