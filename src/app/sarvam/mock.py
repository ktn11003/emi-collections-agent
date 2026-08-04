"""Deterministic stand-ins for the Sarvam APIs, used when no key is configured.

Why this exists: an evaluator should be able to `git clone`, `pip install`,
`uvicorn` and see the whole pipeline — DB writes, tool execution, analytics,
dashboard — before they have credentials. Set SARVAM_API_KEY and the same code
paths hit the real models; nothing else changes.

Every function here mirrors the real signature and returns plausible Hinglish
so the flow is legible.
"""

from __future__ import annotations

import math
import struct
from typing import Any, AsyncIterator

from app.sarvam.chat import Delta, ToolCall
from app.sarvam.stt import Transcript
from app.sarvam.tts import AudioChunk

_CANNED_TRANSCRIPTS = [
    "Haan ji boliye",
    "Abhi paisa nahi hai, salary aane par kar dunga",
    "Theek hai, 8 tareekh ko kar dunga payment",
    "Mujhe payment link bhej dijiye WhatsApp par",
]
_counter = {"stt": 0, "llm": 0}


def transcribe(wav: bytes, *, language_code: str = "unknown") -> Transcript:
    i = _counter["stt"] % len(_CANNED_TRANSCRIPTS)
    _counter["stt"] += 1
    return Transcript(
        text=_CANNED_TRANSCRIPTS[i],
        language="hi-IN" if language_code in ("unknown", "hi-IN") else language_code,
        language_confidence=0.93,
        request_id="mock-stt",
        audio_duration_s=round(len(wav) / 32000.0, 2),
    )


def synthesize(text: str, *, sample_rate: int = 22050) -> bytes:
    """A short 440 Hz tone as a WAV, length proportional to the text."""
    from app.audio import wav_bytes

    seconds = min(6.0, max(0.4, len(text) / 14.0))
    n = int(sample_rate * seconds)
    frames = bytearray()
    for i in range(n):
        # fade in/out so it does not click
        env = min(1.0, i / (0.02 * sample_rate), (n - i) / (0.02 * sample_rate))
        frames += struct.pack("<h", int(6000 * env * math.sin(2 * math.pi * 440 * i / sample_rate)))
    return wav_bytes(bytes(frames), sample_rate=sample_rate)


def stream_sentence(text: str) -> list[AudioChunk]:
    audio = synthesize(text)
    third = max(1, len(audio) // 3)
    return [
        AudioChunk(data=audio[i : i + third], content_type="audio/wav", seq=n + 1, first=(n == 0))
        for n, i in enumerate(range(0, len(audio), third))
    ]


async def stream_chat(
    messages: list[dict[str, Any]], *, tools: list[dict] | None = None
) -> AsyncIterator[Delta]:
    """Walk a scripted collections conversation, including one tool call."""
    turn = _counter["llm"]
    _counter["llm"] += 1

    scripts = [
        "Namaste Rahul ji, main Priya bol rahi hoon Generic Finance se. "
        "Yeh call quality aur record ke liye record ki ja rahi hai. "
        "Aapki 4,500 rupees ki EMI 5 tareekh ko due thi. Kya aap aaj payment kar sakte hain?",
        "Bilkul samajh sakti hoon. Koi baat nahi. "
        "Aap batayein kis tareekh tak kar payenge?",
        "Theek hai, main 8 tareekh note kar rahi hoon. Main aapko payment link bhej deti hoon.",
        "Link bhej diya hai. Dhanyavaad Rahul ji, aapka din shubh ho.",
    ]
    text = scripts[min(turn, len(scripts) - 1)]
    for word in text.split(" "):
        yield Delta(content=word + " ")

    if turn == 2 and tools:
        yield Delta(
            tool_calls=[
                ToolCall(id="mock_ptp", name="schedule_ptp",
                         arguments={"loan_id": "PL0098", "promised_date": "2026-07-08", "amount": 4500}),
                ToolCall(id="mock_link", name="send_payment_link",
                         arguments={"loan_id": "PL0098", "amount": 4500, "channel": "WHATSAPP"}),
            ],
            finish_reason="tool_calls",
        )
    else:
        yield Delta(finish_reason="stop")


def complete(messages: list[dict[str, Any]]) -> str:
    """Used by analytics/guardrails in offline mode."""
    prompt = " ".join(str(m.get("content", "")) for m in messages).lower()
    if "json" in prompt and "sentiment" in prompt:
        return (
            '{"summary": "Borrower acknowledged the overdue EMI, cited a salary delay, '
            'and committed to pay on 8 July. Payment link requested on WhatsApp.", '
            '"sentiment": "neutral", "sentiment_score": 0.1, '
            '"disposition": "PTP", "intents": ["payment_intent", "salary_delay"], '
            '"objections": ["no_funds_now"]}'
        )
    if "compliance" in prompt or "qa" in prompt:
        return (
            '{"qa_score": 92, "disclosed_recording": true, "identified_self": true, '
            '"no_threat": true, "in_window": true, "notes": "All mandatory disclosures present."}'
        )
    return "Borrower committed to pay on 8 July 2026."


def translate(text: str, target_language: str) -> str:
    return f"[{target_language}] {text}"
