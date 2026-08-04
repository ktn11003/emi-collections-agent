"""Post-call analytics — batch STT, diarization, sentiment, disposition, QA.

This is the optional deliverable that mirrors the real "call analytics"
pre-sales conversation, and it is where the 100%-QA claim gets made concrete:
human teams sample ~2% of calls; this scores every one, in every language.

Pipeline
--------
1. **Source the text.** If a recording exists, re-transcribe it with batch Saaras
   (higher accuracy than the streaming pass, and it can diarize). Otherwise use
   the per-turn transcript already in the database, which is already diarized by
   construction — we know who was speaking because we generated one side of it.
2. **Reason over it** with sarvam-105b, reasoning left ON (accuracy over latency,
   the opposite of the live loop) and constrained to JSON.
3. **Render an English summary** so one reviewer can read all six languages.
   Produced by sarvam-105b in the same call rather than by a translation pass:
   Mayura paraphrases proper nouns (it renamed the lender name to "PrimeLife
   Finance" in testing). Mayura remains the fallback.
4. **Score compliance** deterministically, not by asking the model — the
   regulator wants a rule, not an opinion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.guardrails import compliance_score, compliance_summary, screen_utterance
from app.config import settings
from app.db.base import session_scope
from app.db.models import Disposition, Speaker
from app.db.repo import (
    add_event,
    call_transcript,
    get_call,
    upsert_analytics,
)
from app.sarvam.chat import complete_json
from app.sarvam.stt import transcribe
from app.sarvam.translate import translate
from app.steplog import step, timed

logger = logging.getLogger("emi.analytics")

ANALYSIS_SYSTEM = """You are a collections QA analyst for an Indian NBFC.
You read one recorded EMI-reminder call between an AI agent and a borrower and
return a strict JSON object. The conversation may be code-mixed (Hindi/English or
another Indian language with English). Judge what the borrower actually committed
to, not what the agent hoped for.

Return exactly this shape:
{
  "summary": "3-4 sentence factual summary in the language of the call",
  "summary_english": "the same summary in English. Keep every proper noun exactly as written: the lender's name, the agent's name, the borrower's name, the loan id and all amounts.",
  "sentiment": "positive" | "neutral" | "negative",
  "sentiment_score": -1.0 to 1.0,
  "disposition": "PTP" | "PAID" | "DISPUTE" | "WRONG_NUMBER" | "CALLBACK" | "REFUSED" | "ESCALATED" | "INCOMPLETE",
  "ptp_date": "YYYY-MM-DD or null",
  "intents": ["short snake_case labels"],
  "objections": ["short snake_case labels for reasons given for non-payment"],
  "borrower_stated_reason": "one line, or null",
  "agent_quality_notes": "one line on how the agent handled it"
}
Output JSON only. No prose, no code fences."""


@dataclass
class AnalyticsResult:
    call_id: str
    summary: str = ""
    summary_english: str = ""
    sentiment: str = "neutral"
    sentiment_score: float = 0.0
    disposition_predicted: str = Disposition.INCOMPLETE.value
    intents: list[str] = field(default_factory=list)
    objections: list[str] = field(default_factory=list)
    qa_score: float = 0.0
    compliance_flags: dict[str, Any] = field(default_factory=dict)
    diarized_turns: list[dict] = field(default_factory=list)
    model_versions: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "summary": self.summary,
            "summary_english": self.summary_english,
            "sentiment": self.sentiment,
            "sentiment_score": self.sentiment_score,
            "disposition_predicted": self.disposition_predicted,
            "intents": self.intents,
            "objections": self.objections,
            "qa_score": self.qa_score,
            "compliance_flags": self.compliance_flags,
            "diarized_turns": self.diarized_turns,
            "model_versions": self.model_versions,
        }


def _render_conversation(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        who = "AGENT" if t["speaker"] == Speaker.BOT.value else (
            "BORROWER" if t["speaker"] == Speaker.BORROWER.value else "SYSTEM"
        )
        lang = f" [{t['language']}]" if t.get("language") else ""
        lines.append(f"{who}{lang}: {t['text']}")
    return "\n".join(lines)


async def _rebuild_from_recording(recording_uri: str | None) -> list[dict] | None:
    """Batch-transcribe the recording if one was captured.

    Streaming STT optimises for latency; the batch endpoint is more accurate and
    is what you would run over an archive of calls overnight.
    """
    if not recording_uri or not recording_uri.startswith("file://"):
        return None
    path = Path(recording_uri.removeprefix("file://"))
    if not path.exists():
        return None

    with timed("analytics.stage", None, phase="batch_stt", file=path.name):
        transcript = await transcribe(path.read_bytes(), language_code="unknown")
    return [{
        "idx": 0, "speaker": Speaker.BORROWER.value, "text": transcript.text,
        "language": transcript.language, "confidence": transcript.language_confidence,
    }]


async def analyse_call(call_id: str) -> AnalyticsResult:
    """Run the full post-call pipeline for one call and persist the report."""
    step("analytics.stage", call_id, phase="start")

    with session_scope() as s:
        call = get_call(s, call_id)
        if call is None:
            raise ValueError(f"unknown call {call_id}")
        turns = call_transcript(s, call_id)
        recording_uri = call.recording_uri
        script_language = call.script_language or "hi-IN"
        languages_detected = list(call.languages_detected or [])
        stored_compliance = dict(call.compliance or {})
        bot_lines = [t["text"] for t in turns if t["speaker"] == Speaker.BOT.value]
        consent = bool(call.borrower.consent) if call.borrower else False

    # 1. source the text -----------------------------------------------------
    from_recording = await _rebuild_from_recording(recording_uri)
    diarized = from_recording or turns
    step("analytics.stage", call_id, phase="diarization",
         source="batch_stt" if from_recording else "live_turns", turns=len(diarized))

    result = AnalyticsResult(
        call_id=call_id,
        diarized_turns=diarized,
        model_versions={
            "stt": settings.stt_model, "llm": settings.llm_model,
            "tts": settings.tts_model, "translate": settings.translate_model,
        },
    )

    if not diarized:
        step("analytics.stage", call_id, phase="skipped", reason="no transcript")
        return result

    conversation = _render_conversation(diarized)

    # 2. reason over it ------------------------------------------------------
    with timed("analytics.stage", call_id, phase="llm_analysis"):
        analysis = await complete_json(
            [
                {"role": "system", "content": ANALYSIS_SYSTEM},
                {"role": "user", "content":
                    f"Call languages detected: {languages_detected or [script_language]}\n\n{conversation}"},
            ],
            # Generous on purpose: reasoning is enabled here and consumes the
            # budget before any content appears (see chat.REASONING_TOKEN_FLOOR).
            max_tokens=4000,
        )

    result.summary = str(analysis.get("summary") or "").strip()
    result.sentiment = str(analysis.get("sentiment") or "neutral").lower()
    try:
        result.sentiment_score = float(analysis.get("sentiment_score") or 0.0)
    except (TypeError, ValueError):
        result.sentiment_score = 0.0
    result.intents = [str(i) for i in (analysis.get("intents") or [])]
    result.objections = [str(o) for o in (analysis.get("objections") or [])]

    predicted = str(analysis.get("disposition") or "").upper()
    result.disposition_predicted = (
        predicted if predicted in {d.value for d in Disposition} else Disposition.INCOMPLETE.value
    )

    # 3. English for HQ reporting -------------------------------------------
    # Produced by the LLM in the same call, not by a separate translation pass.
    # Why: Mayura paraphrases proper nouns — it rendered the lender name as
    # "PrimeLife Finance", which is unacceptable in a document a compliance
    # reviewer reads, and placeholder-protection was unreliable because the model
    # sometimes drops the placeholder too. sarvam-105b keeps entity names intact
    # because it has the conversation in context, and it saves an API call.
    # Mayura remains the fallback, and is still the primary path for localising
    # the compliance disclosure (translate_cached) where the text is fixed and
    # reviewed once.
    result.summary_english = str(analysis.get("summary_english") or "").strip()
    if result.summary and not result.summary_english:
        source_lang = languages_detected[0] if languages_detected else script_language
        if source_lang.startswith("en"):
            result.summary_english = result.summary
        else:
            with timed("analytics.stage", call_id, phase="translate_summary_fallback"):
                result.summary_english = await translate(
                    result.summary, source_language=source_lang,
                    target_language="en-IN", mode="formal",
                )
                logger.info("used Mayura fallback for the English summary")

    # 4. deterministic compliance scoring -----------------------------------
    # Re-screen every agent line rather than trusting the live pass: this is the
    # independent check an auditor would run over the archive.
    replayed_tags: list[str] = []
    for line in bot_lines:
        screen = screen_utterance(line)
        if not screen.allowed:
            replayed_tags.extend(screen.tags)

    flags = compliance_summary(
        bot_lines,
        borrower=_Consent(consent),
        in_window=bool(stored_compliance.get("in_window", True)),
        violation_tags=replayed_tags + list(stored_compliance.get("violation_tags") or []),
    )
    result.compliance_flags = flags
    result.qa_score = compliance_score(flags)
    step("analytics.stage", call_id, phase="qa_scoring", qa_score=result.qa_score,
         violations=flags.get("violation_tags"))

    # persist ----------------------------------------------------------------
    with session_scope() as s:
        upsert_analytics(
            s, call_id,
            summary=result.summary,
            summary_english=result.summary_english,
            sentiment=result.sentiment,
            sentiment_score=result.sentiment_score,
            disposition_predicted=result.disposition_predicted,
            intents=result.intents,
            objections=result.objections,
            qa_score=result.qa_score,
            compliance_flags=result.compliance_flags,
            diarized_turns=result.diarized_turns,
            model_versions=result.model_versions,
        )
        call = get_call(s, call_id)
        if call is not None:
            call.sentiment = result.sentiment
            call.summary = result.summary_english or result.summary
        add_event(s, call_id, "analytics.completed",
                  {"qa_score": result.qa_score, "disposition": result.disposition_predicted})

    step("analytics.stage", call_id, phase="done", qa_score=result.qa_score,
         disposition=result.disposition_predicted, sentiment=result.sentiment)
    return result


async def analyse_all_pending(limit: int = 25) -> list[AnalyticsResult]:
    """Batch mode: score every completed call that has no report yet.

    This is the overnight job — "every call, scored, in every language".
    """
    from sqlalchemy import select

    from app.db.models import AnalyticsReport, Call

    with session_scope() as s:
        scored = set(s.scalars(select(AnalyticsReport.call_id)).all())
        candidates = [
            c.id for c in s.scalars(
                select(Call).where(Call.ended_at.is_not(None)).order_by(Call.started_at.desc())
            ).all()
            if c.id not in scored
        ][:limit]

    results = []
    for call_id in candidates:
        try:
            results.append(await analyse_call(call_id))
        except Exception:  # noqa: BLE001 - one bad call must not stop the batch
            logger.exception("analytics failed for %s", call_id)
            step("error", call_id, source="analytics")
    return results


def portfolio_report() -> dict:
    """Aggregate view for the dashboard / the ROI slide."""
    from sqlalchemy import func, select

    from app.db.models import AnalyticsReport
    from app.db.repo import portfolio_stats

    with session_scope() as s:
        stats = portfolio_stats(s)
        avg_qa = s.scalar(select(func.avg(AnalyticsReport.qa_score))) or 0.0
        sentiment_mix = dict(
            s.execute(
                select(AnalyticsReport.sentiment, func.count()).group_by(AnalyticsReport.sentiment)
            ).all()
        )
        scored = s.scalar(select(func.count()).select_from(AnalyticsReport)) or 0

    stats.update({
        "calls_scored": scored,
        "avg_qa_score": round(float(avg_qa), 1),
        "sentiment_mix": sentiment_mix,
        # Human QA samples ~2% of calls; this pipeline scores 100% of them.
        "qa_coverage_pct": round(100.0 * scored / stats["total_calls"], 1) if stats["total_calls"] else 0.0,
    })
    return stats


@dataclass(slots=True)
class _Consent:
    consent: bool
