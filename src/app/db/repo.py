"""Repository helpers — every DB write the app performs goes through here."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    AnalyticsReport,
    AuditLog,
    Borrower,
    Call,
    Campaign,
    Channel,
    ComplianceEvent,
    CrmWriteback,
    Disposition,
    Escalation,
    Event,
    PaymentLink,
    PromiseToPay,
    Speaker,
    ToolInvocation,
    ToolStatus,
    TranslationCache,
    Turn,
    utcnow,
)


# --- borrowers / campaigns ---------------------------------------------------
def upsert_borrower(s: Session, **fields: Any) -> Borrower:
    b = s.get(Borrower, fields["loan_id"])
    if b is None:
        b = Borrower(**fields)
        s.add(b)
    else:
        for k, v in fields.items():
            setattr(b, k, v)
    s.flush()
    return b


def get_borrower(s: Session, loan_id: str) -> Borrower | None:
    return s.get(Borrower, loan_id)


def list_borrowers(s: Session, limit: int = 200) -> Sequence[Borrower]:
    return s.scalars(select(Borrower).order_by(desc(Borrower.dpd)).limit(limit)).all()


def get_or_create_campaign(s: Session, name: str, **fields: Any) -> Campaign:
    c = s.scalar(select(Campaign).where(Campaign.name == name))
    if c is None:
        c = Campaign(name=name, **fields)
        s.add(c)
        s.flush()
    return c


# --- calls -------------------------------------------------------------------
def create_call(
    s: Session,
    *,
    correlation_id: str,
    loan_id: str,
    campaign_id: str | None = None,
    channel: Channel = Channel.BROWSER,
    script_language: str = "hi-IN",
    compliance: dict | None = None,
) -> Call:
    call = Call(
        correlation_id=correlation_id,
        loan_id=loan_id,
        campaign_id=campaign_id,
        channel=channel,
        script_language=script_language,
        compliance=compliance or {},
        answered_at=utcnow(),
    )
    s.add(call)
    s.flush()
    return call


def get_call(s: Session, call_id: str) -> Call | None:
    return s.scalar(
        select(Call)
        .options(selectinload(Call.turns), selectinload(Call.tool_invocations), selectinload(Call.borrower))
        .where(Call.id == call_id)
    )


def list_calls(s: Session, limit: int = 50) -> Sequence[Call]:
    return s.scalars(
        select(Call).options(selectinload(Call.borrower)).order_by(desc(Call.started_at)).limit(limit)
    ).all()


def finalise_call(
    s: Session,
    call: Call,
    *,
    disposition: Disposition,
    summary: str | None = None,
    sentiment: str | None = None,
    latency_stats: dict | None = None,
    recording_uri: str | None = None,
    transcript_uri: str | None = None,
    cost_paise: int | None = None,
) -> Call:
    call.ended_at = utcnow()
    started = call.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        call.duration_s = round((call.ended_at - started).total_seconds(), 2)
    call.disposition = disposition
    if summary is not None:
        call.summary = summary
    if sentiment is not None:
        call.sentiment = sentiment
    if latency_stats is not None:
        call.latency_stats = latency_stats
    if recording_uri is not None:
        call.recording_uri = recording_uri
    if transcript_uri is not None:
        call.transcript_uri = transcript_uri
    if cost_paise is not None:
        call.cost_paise = cost_paise
    s.flush()
    return call


# --- turns -------------------------------------------------------------------
def add_turn(
    s: Session,
    call: Call,
    *,
    speaker: Speaker,
    text: str,
    language: str | None = None,
    language_confidence: float | None = None,
    latency_ms: dict | None = None,
    barged_in: bool = False,
) -> Turn:
    next_idx = s.scalar(select(func.coalesce(func.max(Turn.idx), -1)).where(Turn.call_id == call.id)) + 1
    turn = Turn(
        call_id=call.id,
        idx=next_idx,
        speaker=speaker,
        text=text,
        language=language,
        language_confidence=language_confidence,
        latency_ms=latency_ms or {},
        barged_in=barged_in,
    )
    s.add(turn)

    # Track the set of languages seen, in order — evidence of code-switching.
    if language and language not in (call.languages_detected or []):
        call.languages_detected = [*(call.languages_detected or []), language]
    s.flush()
    return turn


def call_transcript(s: Session, call_id: str) -> list[dict]:
    turns = s.scalars(select(Turn).where(Turn.call_id == call_id).order_by(Turn.idx)).all()
    return [
        {
            "idx": t.idx,
            "speaker": t.speaker.value,
            "text": t.text,
            "language": t.language,
            "confidence": t.language_confidence,
            "latency_ms": t.latency_ms,
            "barged_in": t.barged_in,
            "ts": t.created_at.isoformat() if t.created_at else None,
        }
        for t in turns
    ]


# --- tool invocations (idempotency) -----------------------------------------
def find_tool_invocation(s: Session, idempotency_key: str) -> ToolInvocation | None:
    return s.scalar(select(ToolInvocation).where(ToolInvocation.idempotency_key == idempotency_key))


def begin_tool_invocation(
    s: Session, *, call_id: str | None, name: str, arguments: dict, idempotency_key: str
) -> ToolInvocation:
    ti = ToolInvocation(
        call_id=call_id,
        name=name,
        arguments=arguments,
        idempotency_key=idempotency_key,
        status=ToolStatus.PENDING,
        attempts=1,
    )
    s.add(ti)
    s.flush()
    return ti


def complete_tool_invocation(
    s: Session, ti: ToolInvocation, *, result: dict | None, status: ToolStatus, error: str | None = None
) -> ToolInvocation:
    ti.result = result
    ti.status = status
    ti.error = error
    ti.completed_at = utcnow()
    s.flush()
    return ti


# --- side-effect records ----------------------------------------------------
def add_payment_link(s: Session, **fields: Any) -> PaymentLink:
    pl = PaymentLink(**fields)
    s.add(pl)
    s.flush()
    return pl


def add_ptp(s: Session, **fields: Any) -> PromiseToPay:
    ptp = PromiseToPay(**fields)
    s.add(ptp)
    s.flush()
    return ptp


def add_crm_writeback(s: Session, **fields: Any) -> CrmWriteback:
    wb = CrmWriteback(**fields)
    s.add(wb)
    s.flush()
    return wb


def add_escalation(s: Session, **fields: Any) -> Escalation:
    e = Escalation(**fields)
    s.add(e)
    s.flush()
    return e


# --- events / compliance / audit -------------------------------------------
def add_event(s: Session, call_id: str | None, stage: str, payload: dict | None = None) -> Event:
    ev = Event(call_id=call_id, stage=stage, payload=payload or {})
    s.add(ev)
    s.flush()
    return ev


def add_compliance_event(
    s: Session,
    call_id: str | None,
    kind: str,
    *,
    passed: bool = True,
    detail: str | None = None,
    regulation: str | None = None,
) -> ComplianceEvent:
    ce = ComplianceEvent(
        call_id=call_id, kind=kind, passed=passed, detail=detail, regulation=regulation
    )
    s.add(ce)
    s.flush()
    return ce


def add_audit(
    s: Session, action: str, *, actor: str = "system", entity: str | None = None,
    entity_id: str | None = None, payload: dict | None = None,
) -> AuditLog:
    a = AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, payload=payload or {})
    s.add(a)
    s.flush()
    return a


def list_events(s: Session, call_id: str) -> Sequence[Event]:
    return s.scalars(select(Event).where(Event.call_id == call_id).order_by(Event.id)).all()


# --- analytics --------------------------------------------------------------
def upsert_analytics(s: Session, call_id: str, **fields: Any) -> AnalyticsReport:
    rep = s.scalar(select(AnalyticsReport).where(AnalyticsReport.call_id == call_id))
    if rep is None:
        rep = AnalyticsReport(call_id=call_id, **fields)
        s.add(rep)
    else:
        for k, v in fields.items():
            setattr(rep, k, v)
    s.flush()
    return rep


def get_analytics(s: Session, call_id: str) -> AnalyticsReport | None:
    return s.scalar(select(AnalyticsReport).where(AnalyticsReport.call_id == call_id))


# --- translation cache ------------------------------------------------------
def cached_translation(s: Session, text: str, src: str, tgt: str) -> str | None:
    return s.scalar(
        select(TranslationCache.translated_text).where(
            TranslationCache.source_text == text,
            TranslationCache.source_language == src,
            TranslationCache.target_language == tgt,
        )
    )


def cache_translation(s: Session, text: str, src: str, tgt: str, out: str, model: str) -> None:
    s.add(
        TranslationCache(
            source_text=text, source_language=src, target_language=tgt,
            translated_text=out, model=model,
        )
    )
    s.flush()


# --- dashboard aggregates ---------------------------------------------------
def portfolio_stats(s: Session) -> dict:
    """Numbers the dashboard and the ROI slide are computed from."""
    total_calls = s.scalar(select(func.count()).select_from(Call)) or 0
    by_disp = dict(
        s.execute(select(Call.disposition, func.count()).group_by(Call.disposition)).all()
    )
    ptp_count = s.scalar(select(func.count()).select_from(PromiseToPay)) or 0
    ptp_value = s.scalar(select(func.coalesce(func.sum(PromiseToPay.amount_paise), 0))) or 0
    links = s.scalar(select(func.count()).select_from(PaymentLink)) or 0
    avg_duration = s.scalar(select(func.avg(Call.duration_s))) or 0.0
    escalations = s.scalar(select(func.count()).select_from(Escalation)) or 0
    tools_ok = s.scalar(
        select(func.count()).select_from(ToolInvocation).where(ToolInvocation.status == ToolStatus.SUCCEEDED)
    ) or 0

    contained = total_calls - (by_disp.get(Disposition.ESCALATED, 0) + by_disp.get(Disposition.INCOMPLETE, 0))
    return {
        "total_calls": total_calls,
        "dispositions": {
            (k.value if hasattr(k, "value") else str(k)): v for k, v in by_disp.items()
        },
        "ptp_count": ptp_count,
        "ptp_value_rupees": round(ptp_value / 100.0, 2),
        "payment_links": links,
        "avg_duration_s": round(float(avg_duration), 1),
        "escalations": escalations,
        "tool_invocations_succeeded": tools_ok,
        "containment_pct": round(100.0 * contained / total_calls, 1) if total_calls else 0.0,
        "ptp_rate_pct": round(100.0 * ptp_count / total_calls, 1) if total_calls else 0.0,
    }


def due_today(s: Session, on: date | None = None) -> Sequence[Borrower]:
    """Callable universe: consent given, not DND, overdue."""
    return s.scalars(
        select(Borrower)
        .where(Borrower.consent.is_(True), Borrower.dnd_registered.is_(False), Borrower.dpd > 0)
        .order_by(desc(Borrower.dpd))
    ).all()
