"""The browser-mic voice channel.

Protocol (deliberately simple, and the same shape a SIP media bridge would use):

* client -> server: **binary** frames of mono 16-bit little-endian PCM @ 16 kHz
  (the browser's AudioWorklet does the downsample), plus **text** JSON control
  messages (``hangup``, ``dtmf``, ``ping``).
* server -> client: JSON events — ``transcript``, ``audio`` (base64 mp3/wav),
  ``vad``, ``barge_in``, ``state``, ``latency``, ``tool_call``, ``tool_result``,
  ``compliance_block``, ``call_ended``.

Everything the UI renders — the live transcript, the latency HUD, the barge-in
flash, the tool ledger — is driven by those events, which are the same events
written to ``logs/steps.jsonl`` and the ``events`` table.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.agent.session import CallBlocked, CallSession
from app.db.models import Channel, Disposition
from app.steplog import step

logger = logging.getLogger("emi.ws")
router = APIRouter()

# 16 kHz * 2 bytes * 0.1 s = 3200 bytes per 100 ms window.
MIN_WINDOW_BYTES = 3200


@router.websocket("/ws/voice")
async def voice_socket(
    ws: WebSocket,
    loan_id: str = Query(..., description="Borrower loan account to call"),
    language: str | None = Query(None, description="Override the stored language preference"),
) -> None:
    await ws.accept()
    send_lock = asyncio.Lock()

    async def emit(event: str, payload: dict[str, Any]) -> None:
        """Serialise all sends: audio chunks and events share one socket."""
        async with send_lock:
            try:
                await ws.send_text(json.dumps({"event": event, **payload}, default=str))
            except (WebSocketDisconnect, RuntimeError):
                pass

    session = CallSession(
        loan_id=loan_id, emit=emit, channel=Channel.BROWSER, language=language
    )

    try:
        await session.start()
    except CallBlocked as blocked:
        # The compliance gate refused. Tell the client *why*, with the regulation.
        await emit("call_blocked", {
            "reasons": [{"reason": r, "regulation": reg} for r, reg in blocked.reasons],
        })
        await ws.close(code=1008)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to start call")
        await emit("error", {"source": "start", "detail": str(exc)})
        await ws.close(code=1011)
        return

    buffer = bytearray()
    try:
        while True:
            message = await ws.receive()

            if message["type"] == "websocket.disconnect":
                break

            if (payload := message.get("bytes")) is not None:
                # Accumulate into ~100 ms windows before forwarding: one JSON
                # envelope per 20 ms frame would be pure overhead.
                buffer.extend(payload)
                if len(buffer) >= MIN_WINDOW_BYTES:
                    chunk = bytes(buffer)
                    buffer.clear()
                    await session.push_audio(chunk)
                continue

            if (text := message.get("text")) is None:
                continue

            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue

            action = control.get("action")
            if action == "hangup":
                if buffer:
                    await session.push_audio(bytes(buffer))
                    buffer.clear()
                await session.say_closing()
                break
            if action == "ping":
                await emit("pong", {})
            elif action == "dtmf":
                # RFC 2833 telephone-event on a real leg; a keypress here.
                digit = str(control.get("digit", ""))[:1]
                step("media.audio_in", session.call_id, dtmf=digit)
                await emit("dtmf_ack", {"digit": digit})

    except WebSocketDisconnect:
        logger.info("client disconnected from call %s", session.call_id[:8])
    except Exception:  # noqa: BLE001
        logger.exception("voice socket error")
        step("error", session.call_id, source="ws_voice")
    finally:
        # A caller who hangs up mid-negotiation is INCOMPLETE, not a refusal —
        # the disposition must not flatter the numbers.
        final = session.disposition if session.disposition != Disposition.INCOMPLETE else None
        try:
            await session.stop(disposition=final)
        except Exception:  # noqa: BLE001
            logger.exception("failed to finalise call %s", session.call_id)
        with_close = getattr(ws, "client_state", None)
        if with_close is None or with_close.name != "DISCONNECTED":
            try:
                await ws.close()
            except RuntimeError:
                pass
