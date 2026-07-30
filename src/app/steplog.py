"""Structured step logging.

Every hop in the pipeline emits one step record. Three sinks:

1. the console logger (human readable, for the terminal during a demo),
2. ``logs/steps.jsonl`` (machine readable, replayable after the fact),
3. the ``events`` table (queryable, joined to the call — see db/models.py).

The stage names below are the same ones used in docs/architecture.md, so a
recorded call can be walked hop-by-hop while presenting.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger("emi.step")

# --- pipeline stages, in the order a turn travels through them ---------------
STAGES = (
    "ingest.csv",             # SFTP-style call list -> DB
    "dialer.precheck",        # consent, DND, calling window
    "call.started",
    "media.audio_in",         # browser mic / RTP frames arrive
    "stt.speech_start",       # Saaras VAD: caller began speaking
    "stt.speech_end",         # Saaras VAD: endpoint detected
    "stt.transcript",         # Saaras final transcript + detected language
    "agent.barge_in",         # caller interrupted the bot
    "llm.request",
    "llm.first_token",
    "llm.sentence",           # sentence aggregated, handed to TTS
    "llm.tool_call",
    "tool.executed",          # orchestrator ran a side effect
    "tts.request",
    "tts.first_audio",
    "tts.complete",
    "media.audio_out",
    "guardrail.check",
    "compliance.flag",
    "call.ended",
    "cdr.written",
    "analytics.stage",
    "error",
)

# An Indian mobile with optional country code: +91 98XXXXXXXX / 09812345678.
# Anchored on non-digit boundaries and capped in length so it cannot swallow an
# epoch timestamp or a UUID fragment -- an earlier, greedier pattern mangled
# `"ts": 1785...` into `178******60.05` and made every log line unparseable.
_PHONE_RE = re.compile(r"(?<![\d.\-])(\+?91[\-\s]?|0)?([6-9]\d{9})(?![\d.\-])")
_LOAN_RE = re.compile(r"\b([A-Z]{2,3}\d{4,})\b")

# Keys whose values are structural, never PII, and must survive verbatim.
_NEVER_REDACT = frozenset({"ts", "stage", "call_id", "correlation_id", "ms", "seq"})

_lock = threading.Lock()
_path = settings.resolve(settings.step_log_path)


def redact(text: str) -> str:
    """Mask phone numbers and loan ids so logs are DPDP-safe by default.

    Applied to individual *values*, never to a serialised JSON document — masking
    a JSON blob corrupts numbers and identifiers and destroys the log's
    machine-readability.
    """
    if not settings.redact_pii_in_logs or not text:
        return text
    text = _PHONE_RE.sub(lambda m: f"{(m.group(1) or '')}{m.group(2)[:2]}{'*' * 6}{m.group(2)[-2:]}", text)
    return _LOAN_RE.sub(lambda m: m.group(1)[:2] + "*" * (len(m.group(1)) - 4) + m.group(1)[-2:], text)


def _redact_value(key: str, value: Any) -> Any:
    """Recursively redact string leaves, preserving structure and types."""
    if key in _NEVER_REDACT:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, v) for v in value]
    return value


def step(stage: str, call_id: str | None = None, **fields: Any) -> dict[str, Any]:
    """Record one pipeline step. Returns the record (also used for WS fan-out)."""
    rec: dict[str, Any] = {
        "ts": time.time(),
        "stage": stage,
        "call_id": call_id,
        **fields,
    }
    if settings.redact_pii_in_logs:
        safe = {k: _redact_value(k, v) for k, v in rec.items()}
    else:
        safe = rec
    line = json.dumps(safe, ensure_ascii=False, default=str)

    with _lock:
        try:
            _path.parent.mkdir(parents=True, exist_ok=True)
            with _path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:  # never let logging break a live call
            logger.debug("step log write failed", exc_info=True)

    detail = " ".join(f"{k}={v}" for k, v in fields.items() if k != "call_id")
    logger.info("%-20s %s %s", stage, (call_id or "-")[:8], redact(detail)[:220])
    return rec


@contextmanager
def timed(stage: str, call_id: str | None = None, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a block and log its duration under ``ms``."""
    t0 = time.perf_counter()
    box: dict[str, Any] = {}
    try:
        yield box
    finally:
        step(stage, call_id, ms=round((time.perf_counter() - t0) * 1000, 1), **{**fields, **box})


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-5s  %(name)-14s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
