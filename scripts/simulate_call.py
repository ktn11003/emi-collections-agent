"""Drive a full call headlessly, with a synthetic borrower.

The browser demo needs a human with a microphone. This script replaces the human:
it synthesises the borrower's replies with Bulbul (a different voice), streams
them into the same ``/ws/voice`` endpoint as real PCM, and prints every event.

That makes the whole loop testable without a person:
    STT (VAD + transcript) -> sarvam-105b (+ tools) -> TTS -> DB -> analytics

    # server must already be running
    python scripts/simulate_call.py --loan-id PL0098
    python scripts/simulate_call.py --loan-id PL0102 --script tamil
    python scripts/simulate_call.py --loan-id PL0098 --barge-in

Exit code is 0 only if the call produced a transcript, at least one tool call and
a written CDR — so it doubles as a smoke test for CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import websockets  # noqa: E402

from app.audio import wav_to_stt_pcm  # noqa: E402
from app.config import settings  # noqa: E402
from app.sarvam import tts as tts_api  # noqa: E402
from app.sarvam.client import close_http  # noqa: E402

# A realistic collections conversation: acknowledge -> objection -> commit -> ask for link.
SCRIPTS: dict[str, list[str]] = {
    "hindi": [
        "Haan ji boliye, kaun bol raha hai?",
        "Dekhiye abhi mere paas paise nahi hain, salary late ho gayi hai is mahine.",
        "Theek hai, main aath tareekh ko payment kar dunga pakka.",
        "Haan mujhe payment link WhatsApp par bhej dijiye please.",
    ],
    "tamil": [
        "Yaaru pesuraanga?",
        "Ippo kaila panam illa, salary varala.",
        "Sari, ettu thaedhi kattiduven.",
        "Payment link WhatsApp la anuppunga.",
    ],
    "dispute": [
        "Haan boliye.",
        "Nahi nahi, main to pichle hafte hi payment kar diya tha. Aapka record galat hai.",
        "Mujhe kisi insaan se baat karni hai, aap galat bol rahe hain.",
    ],
    "wrong_number": [
        "Hello? Aap kise dhundh rahe hain?",
        "Yahan koi Rahul nahi rehta, aapne galat number lagaya hai.",
    ],
}

# Voices distinct from the agent's, so the transcript is unambiguous.
BORROWER_VOICE = {"hindi": "aditya", "dispute": "aditya", "wrong_number": "aditya", "tamil": "gokul"}
# Keys must cover every entry in SCRIPTS: the lookup below is unguarded, so a
# missing script raises KeyError before a single utterance is synthesised.
# "tamil" was advertised in the module docstring but absent here.
BORROWER_LANG = {"hindi": "hi-IN", "tamil": "ta-IN", "dispute": "hi-IN", "wrong_number": "hi-IN"}


class Recorder:
    """Collects what happened so the run can be asserted on."""

    def __init__(self) -> None:
        self.transcript: list[tuple[str, str]] = []
        self.tool_calls: list[dict] = []
        self.tool_results: list[dict] = []
        self.latencies: list[dict] = []
        self.barge_ins = 0
        self.compliance_blocks: list[dict] = []
        self.language_switches: list[dict] = []
        self.ended: dict | None = None
        self.blocked: dict | None = None
        self.audio_chunks = 0
        self.audio_bytes = 0
        self.errors: list[dict] = []
        self.bot_speaking = asyncio.Event()
        self.bot_idle = asyncio.Event()
        self.bot_idle.set()


async def borrower_audio(text: str, script: str) -> bytes:
    """Synthesise the borrower's line and return 16 kHz mono PCM."""
    wav = await tts_api.synthesize(
        text,
        language=BORROWER_LANG[script],
        speaker=BORROWER_VOICE[script],
        sample_rate=16000,
        codec="wav",
        pace=1.0,
    )
    return wav_to_stt_pcm(wav)


async def stream_pcm(ws, pcm: bytes, *, realtime: bool = True) -> None:
    """Push PCM at wall-clock speed, so the server's VAD behaves as on a real call."""
    window = 16000 * 2 // 10          # 100 ms
    for i in range(0, len(pcm), window):
        await ws.send(pcm[i : i + window])
        if realtime:
            await asyncio.sleep(0.1)
    # A tail of silence is what makes Saaras emit END_SPEECH.
    for _ in range(6):
        await ws.send(b"\x00" * window)
        if realtime:
            await asyncio.sleep(0.1)


async def reader(ws, rec: Recorder, verbose: bool) -> None:
    async for raw in ws:
        msg = json.loads(raw)
        ev = msg.get("event")

        if ev == "call_started":
            print(f"  call     {msg['call_id'][:8]} · {msg['borrower']['name']} · "
                  f"₹{msg['borrower']['emi_rupees']:,.0f} · {msg['language']}")
        elif ev == "call_blocked":
            rec.blocked = msg
            print(f"  BLOCKED  {msg['reasons']}")
        elif ev == "transcript":
            rec.transcript.append((msg["speaker"], msg["text"]))
            tag = "BOT " if msg["speaker"] == "BOT" else "CALLER"
            lang = f" [{msg.get('language')}]" if msg.get("language") else ""
            print(f"  {tag:7} {msg['text'][:96]}{lang}")
        elif ev == "audio":
            rec.audio_chunks += 1
            rec.audio_bytes += len(msg["b64"]) * 3 // 4
        elif ev == "state":
            if msg["state"] == "Speaking":
                rec.bot_speaking.set(); rec.bot_idle.clear()
            elif msg["state"] in ("Listening", "Ended"):
                rec.bot_idle.set()
            if verbose:
                print(f"  state    {msg['state']}")
        elif ev == "vad" and verbose:
            print(f"  vad      {msg['signal']} (state={msg['state']})")
        elif ev == "barge_in":
            rec.barge_ins += 1
            print("  BARGE-IN caller interrupted; TTS cancelled + buffer flushed")
        elif ev == "latency":
            rec.latencies.append(msg)
            print(f"  latency  stt={msg.get('stt_finalisation_ms','–')}ms "
                  f"llm_ttft={msg.get('llm_ttft_ms','–')}ms "
                  f"tts_ttfa={msg.get('tts_ttfa_ms','–')}ms "
                  f"END→AUDIO={msg.get('end_of_speech_to_first_audio_ms','–')}ms")
        elif ev == "tool_call":
            rec.tool_calls.append(msg)
            print(f"  TOOL     {msg['name']}({json.dumps(msg['arguments'], ensure_ascii=False)})")
        elif ev == "tool_result":
            rec.tool_results.append(msg)
            flag = "replayed" if msg.get("replayed") else ("ok" if msg["ok"] else "FAILED")
            print(f"  result   {msg['name']} [{flag}] {json.dumps(msg.get('data'), ensure_ascii=False)[:90]}")
        elif ev == "compliance_block":
            rec.compliance_blocks.append(msg)
            print(f"  GUARDRAIL blocked: {msg['tags']}")
        elif ev == "language_switched":
            rec.language_switches.append(msg)
            print(f"  LANGUAGE {msg['from']} → {msg['to']} (voice {msg['voice']})")
        elif ev == "call_ended":
            rec.ended = msg
            print(f"  ended    disposition={msg['disposition']} "
                  f"compliance={msg.get('compliance_score')}/100")
        elif ev == "error":
            rec.errors.append(msg)
            print(f"  ERROR    {msg['source']}: {msg['detail']}")


async def wait_for_bot(rec: Recorder, timeout: float = 30.0) -> None:
    """Let the bot finish its turn before the borrower replies."""
    try:
        await asyncio.wait_for(rec.bot_idle.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print("  (timeout waiting for the bot to finish)")
    await asyncio.sleep(0.7)          # a beat of human hesitation


async def run(args: argparse.Namespace) -> int:
    lines = SCRIPTS[args.script]
    rec = Recorder()

    print(f"\nSimulated call · loan {args.loan_id} · script '{args.script}' "
          f"({'barge-in' if args.barge_in else 'polite'})")
    print(f"pre-synthesising {len(lines)} borrower utterances with Bulbul…")
    audio = [await borrower_audio(t, args.script) for t in lines]
    print(f"  {sum(len(a) for a in audio) / 32000:.1f}s of borrower audio ready\n")

    url = f"ws://{args.host}:{args.port}/ws/voice?loan_id={args.loan_id}"
    t0 = time.perf_counter()

    try:
        async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
            task = asyncio.create_task(reader(ws, rec, args.verbose))
            await asyncio.sleep(1.0)

            for i, (line, pcm) in enumerate(zip(lines, audio)):
                if rec.blocked or rec.ended:
                    break
                if args.barge_in and i == 1:
                    # Interrupt while the bot is mid-sentence, deliberately.
                    try:
                        await asyncio.wait_for(rec.bot_speaking.wait(), timeout=20)
                    except asyncio.TimeoutError:
                        pass
                    await asyncio.sleep(0.5)
                    print(f"  (interrupting the bot) → {line[:60]}")
                else:
                    await wait_for_bot(rec)
                    print(f"  (borrower says) → {line[:60]}")
                await stream_pcm(ws, pcm)

            if not rec.blocked:
                await wait_for_bot(rec, timeout=35)
                await ws.send(json.dumps({"action": "hangup"}))
            try:
                await asyncio.wait_for(task, timeout=25)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                task.cancel()
    except websockets.ConnectionClosed:
        # The server closes with 1008 when the pre-call compliance gate refuses.
        pass
    finally:
        await close_http()
    wall = time.perf_counter() - t0

    if rec.blocked:
        print(f"\n{'-' * 74}")
        print("call never placed - the pre-call compliance gate refused it:")
        for r in rec.blocked["reasons"]:
            print(f"  - {r['reason']}  [{r['regulation']}]")
        print("\nThis is the gate working as designed. To clear a daily attempt cap:")
        print("  python scripts/seed_db.py          # re-ingest resets attempts_today")
        print("  curl -X POST http://127.0.0.1:8000/api/borrowers/reset-attempts")
        return 0 if args.expect_blocked else 2

    # --- report -------------------------------------------------------------
    print(f"\n{'-' * 74}")
    print(f"wall clock            : {wall:.1f}s")
    print(f"turns                 : {len(rec.transcript)} "
          f"({sum(1 for s, _ in rec.transcript if s == 'BOT')} bot / "
          f"{sum(1 for s, _ in rec.transcript if s == 'BORROWER')} borrower)")
    print(f"audio streamed to peer: {rec.audio_chunks} chunks, {rec.audio_bytes:,} bytes")
    print(f"tool calls            : {[t['name'] for t in rec.tool_calls]}")
    print(f"barge-ins             : {rec.barge_ins}")
    print(f"guardrail blocks      : {len(rec.compliance_blocks)}")
    print(f"language switches     : {[(s['from'], s['to']) for s in rec.language_switches]}")
    print(f"errors                : {len(rec.errors)}")

    if rec.latencies:
        totals = [m["end_of_speech_to_first_audio_ms"] for m in rec.latencies
                  if "end_of_speech_to_first_audio_ms" in m]
        if totals:
            totals.sort()
            print(f"\nend-of-speech → first-audio (the budget that matters):")
            print(f"  p50 {totals[len(totals) // 2]:.0f} ms · "
                  f"max {totals[-1]:.0f} ms · target ≤800 ms · n={len(totals)}")

    if rec.ended:
        print(f"\ndisposition           : {rec.ended['disposition']}")
        print(f"compliance score      : {rec.ended.get('compliance_score')}/100")
        for k, v in (rec.ended.get("compliance") or {}).items():
            if k != "violation_tags":
                print(f"    {'PASS' if v else 'FAIL'}  {k}")
        print(f"transcript written    : {rec.ended.get('transcript_uri')}")

    ok = bool(rec.transcript) and bool(rec.ended) and not rec.errors
    if args.script in ("hindi", "tamil"):
        ok = ok and bool(rec.tool_calls)
    print(f"\n{'PASS' if ok else 'FAIL'}\n")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loan-id", default="PL0098")
    ap.add_argument("--script", default="hindi", choices=sorted(SCRIPTS))
    ap.add_argument("--barge-in", action="store_true", help="interrupt the bot mid-sentence")
    ap.add_argument("--expect-blocked", action="store_true",
                    help="pass when the compliance gate refuses the call (for testing the gate)")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    ap.add_argument("-v", "--verbose", action="store_true", help="also print state/VAD events")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
