"""Mayura / Sarvam-Translate text translation.

Translate does two jobs here that neither STT nor the LLM should do:

1. **Compliance localisation.** The mandatory RBI disclosure and the safe
   fallback phrases are authored *once* in English, reviewed by legal, then
   translated per language and cached in the DB. Every borrower hears the same
   approved sentence in their own language — auditable, and no per-call latency
   after the first use.
2. **HQ reporting.** Post-call summaries are produced in the call's language and
   translated to English so one reviewer can read all six languages.

``mayura:v1`` supports 11 languages and colloquial modes;
``sarvam-translate:v1`` covers all 22 scheduled languages but formal-only. We
pick per language automatically.

**Known limitation:** these models paraphrase proper nouns — a lender name
came back as "PrimeLife Finance" in testing. So Translate is used for *fixed,
reviewed* strings (the compliance disclosure), and free-form call summaries are
rendered into English by sarvam-105b instead, which keeps entity names intact
because it has the conversation in context. See analytics/pipeline.py.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.config import settings
from app.sarvam.client import post_json
from app.sarvam.voices import TTS_LANGUAGES

logger = logging.getLogger("emi.translate")

Mode = Literal["formal", "classic-colloquial", "modern-colloquial", "code-mixed"]

# Mayura's language set (the 11 that also have colloquial modes).
MAYURA_LANGUAGES = frozenset(TTS_LANGUAGES)


def _model_for(target_language: str) -> tuple[str, Mode]:
    """Colloquial Mayura where available, else formal Sarvam-Translate."""
    if target_language in MAYURA_LANGUAGES:
        # Collections calls are spoken, not written: colloquial reads far more
        # naturally through TTS than formal register.
        return "mayura:v1", "modern-colloquial"
    return "sarvam-translate:v1", "formal"


async def translate(
    text: str,
    *,
    source_language: str = "en-IN",
    target_language: str = "hi-IN",
    mode: Mode | None = None,
    speaker_gender: str = "Female",
) -> str:
    """Translate one string. Returns the input unchanged if src == tgt."""
    if not text.strip() or source_language == target_language:
        return text
    if settings.offline_mode:
        from app.sarvam import mock
        return mock.translate(text, target_language)

    model, default_mode = _model_for(target_language)
    body = {
        "input": text[:2000],
        "source_language_code": source_language,
        "target_language_code": target_language,
        "model": model,
        "mode": mode or default_mode,
    }
    if model == "mayura:v1":
        body["speaker_gender"] = speaker_gender

    payload = await post_json("/translate", body)
    return (payload.get("translated_text") or text).strip()


async def translate_cached(
    text: str, *, source_language: str = "en-IN", target_language: str = "hi-IN",
    mode: Mode | None = None,
) -> str:
    """Translate through the DB cache.

    Compliance-critical strings must be byte-identical across calls, and the
    cache also keeps Translate off the live-call hot path after first use.
    """
    if not text.strip() or source_language == target_language:
        return text

    from app.db.base import session_scope
    from app.db.repo import cache_translation, cached_translation

    with session_scope() as s:
        hit = cached_translation(s, text, source_language, target_language)
    if hit:
        return hit

    out = await translate(
        text, source_language=source_language, target_language=target_language, mode=mode
    )
    model, _ = _model_for(target_language)
    with session_scope() as s:
        try:
            cache_translation(s, text, source_language, target_language, out, model)
        except Exception:  # a concurrent writer won the race; harmless
            logger.debug("translation cache insert raced", exc_info=True)
    return out


async def detect_language(text: str) -> dict:
    """``POST /text-lid`` — language identification for a text snippet."""
    if settings.offline_mode:
        return {"language_code": "hi-IN", "script_code": "Deva"}
    return await post_json("/text-lid", {"input": text[:1000]})
