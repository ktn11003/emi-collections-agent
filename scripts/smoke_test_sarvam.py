"""Verify every Sarvam API this project depends on, end to end.

    python scripts/smoke_test_sarvam.py

Checks, in order:
  1. Bulbul v3 TTS (REST)         -> writes a wav, reports bytes + sample rate
  2. Saaras v3 STT (REST)         -> transcribes that wav back (round trip)
  3. Saaras v3 STT (WebSocket)    -> VAD events + streaming transcript
  4. Bulbul v3 TTS (WebSocket)    -> measures real time-to-first-audio
  5. sarvam-105b chat (streaming) -> measures time-to-first-token
  6. sarvam-105b tool calling     -> confirms function calling works
  7. Mayura translate             -> en-IN -> hi-IN
  8. Bulbul mu-law 8 kHz          -> the telephony codec path

Run this first when something breaks: it isolates "the API changed" from
"our code is wrong".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.agent.tools import ALL_TOOLS            # noqa: E402
from app.audio import read_wav, wav_bytes, wav_to_stt_pcm  # noqa: E402
from app.config import settings                  # noqa: E402
from app.sarvam import tts as tts_api            # noqa: E402
from app.sarvam.chat import stream_chat          # noqa: E402
from app.sarvam.client import close_http         # noqa: E402
from app.sarvam.stt import StreamingSTT, transcribe  # noqa: E402
from app.sarvam.translate import translate       # noqa: E402

HINDI = ("Namaste Rahul ji, main Priya bol rahi hoon Piramal Finance se. "
         "Yeh call quality ke liye record ki ja rahi hai. "
         "Aapki 4,500 rupees ki EMI 5 tareekh ko due thi.")

OUT = settings.resolve("data/smoke")
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} {detail}")


async def check_tts_rest() -> bytes:
    t0 = time.perf_counter()
    audio = await tts_api.synthesize(HINDI, language="hi-IN", sample_rate=22050, codec="wav")
    ms = (time.perf_counter() - t0) * 1000
    _pcm, sr, ch = read_wav(audio)
    (OUT / "tts_hindi.wav").write_bytes(audio)
    record("Bulbul v3 TTS (REST)", len(audio) > 1000,
           f"{len(audio):,} bytes, {sr} Hz, {ch}ch, {ms:.0f} ms")
    return audio


async def check_stt_rest(audio: bytes) -> None:
    pcm16k = wav_to_stt_pcm(audio)
    t0 = time.perf_counter()
    tr = await transcribe(wav_bytes(pcm16k), language_code="unknown")
    ms = (time.perf_counter() - t0) * 1000
    record("Saaras v3 STT (REST)", bool(tr.text),
           f"{ms:.0f} ms · lang={tr.language} p={tr.language_confidence} · {tr.text[:60]!r}")


async def check_stt_ws(audio: bytes) -> None:
    pcm = wav_to_stt_pcm(audio)
    events: list[str] = []
    transcript = ""

    async with StreamingSTT(language="unknown", sample_rate=16000) as stt:
        async def pump() -> None:
            window = 16000 * 2 // 10          # 100 ms
            for i in range(0, len(pcm), window):
                await stt.send_pcm(pcm[i : i + window])
                await asyncio.sleep(0.05)      # roughly real time
            await stt.flush()

        task = asyncio.create_task(pump())
        deadline = time.perf_counter() + 35
        async for ev in stt.events():
            if ev.kind in ("speech_start", "speech_end"):
                events.append(ev.kind)
            elif ev.kind == "transcript":
                transcript = ev.text
                break
            if time.perf_counter() > deadline:
                break
        task.cancel()

    record("Saaras v3 STT (WebSocket)", bool(transcript),
           f"vad={events} · {transcript[:55]!r}")


async def check_tts_ws() -> None:
    t0 = time.perf_counter()
    ttfa = None
    chunks = 0
    total = 0
    async for chunk in tts_api.stream_sentence(
        "Aapki EMI pending hai. Kya aap aaj payment kar sakte hain?", language="hi-IN"
    ):
        if ttfa is None:
            ttfa = (time.perf_counter() - t0) * 1000
        chunks += 1
        total += len(chunk.data)
    record("Bulbul v3 TTS (WebSocket)", chunks > 0,
           f"TTFA {ttfa:.0f} ms · {chunks} chunks · {total:,} bytes" if ttfa else "no audio")


async def check_chat_stream() -> None:
    t0 = time.perf_counter()
    ttft = None
    text = ""
    async for delta in stream_chat(
        [
            {"role": "system", "content":
                "You are Priya, an EMI reminder agent for Piramal Finance. Reply in one short "
                "Hindi-English sentence. No markdown."},
            {"role": "user", "content": "Haan boliye, kya baat hai?"},
        ],
        max_tokens=120,
    ):
        if delta.content and ttft is None:
            ttft = (time.perf_counter() - t0) * 1000
        text += delta.content
    record("sarvam-105b chat (streaming)", bool(text.strip()),
           f"TTFT {ttft:.0f} ms · {text.strip()[:60]!r}" if ttft else "no content")


async def check_tool_calling() -> None:
    calls = []
    async for delta in stream_chat(
        [
            {"role": "system", "content":
                "You are a collections agent. The borrower just said they will pay on 8 July 2026. "
                "Their loan_id is PL0098 and the EMI is 4500 rupees. Call the schedule_ptp tool now."},
            {"role": "user", "content": "Main 8 tareekh ko payment kar dunga."},
        ],
        tools=ALL_TOOLS,
        max_tokens=200,
    ):
        calls.extend(delta.tool_calls)
    names = [f"{c.name}({json.dumps(c.arguments, ensure_ascii=False)})" for c in calls]
    record("sarvam-105b tool calling", bool(calls), "; ".join(names)[:110] or "no tool call emitted")


async def check_translate() -> None:
    out = await translate(
        "Your EMI of 4,500 rupees was due on 5 July. Please pay to avoid late charges.",
        source_language="en-IN", target_language="hi-IN",
    )
    record("Mayura translate en->hi", bool(out), out[:70])


async def check_telephony_codec() -> None:
    from app.audio import mulaw_to_pcm16

    mu = await tts_api.synthesize_for_telephony("Test telephony audio.", language="hi-IN")
    pcm = mulaw_to_pcm16(mu)
    record("Bulbul mu-law 8 kHz (telephony)", len(mu) > 500,
           f"{len(mu):,} mu-law bytes -> {len(pcm):,} PCM bytes ({len(mu) / 8000:.2f}s)")


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    mode = "OFFLINE MOCK (no SARVAM_API_KEY)" if settings.offline_mode else "LIVE"
    print(f"\nSarvam API smoke test — {mode}")
    print(f"  base   : {settings.sarvam_base_url}")
    print(f"  models : {settings.stt_model} · {settings.llm_model} · "
          f"{settings.tts_model} · {settings.translate_model}\n")

    try:
        audio = await check_tts_rest()
        await check_stt_rest(audio)
        if not settings.offline_mode:
            await check_stt_ws(audio)
            await check_tts_ws()
        await check_chat_stream()
        if not settings.offline_mode:
            await check_tool_calling()
        await check_translate()
        if not settings.offline_mode:
            await check_telephony_codec()
    finally:
        await close_http()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed. Artifacts in {OUT}\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
