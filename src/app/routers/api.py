"""REST surface: borrowers, calls, tools-as-webhooks, analytics, dashboard data."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.guardrails import in_calling_window, precall_check
from app.analytics.pipeline import analyse_all_pending, analyse_call, portfolio_report
from app.config import settings
from app.db.base import db_flavour, get_session
from app.db.repo import (
    call_transcript,
    get_analytics,
    get_borrower,
    get_call,
    list_borrowers,
    list_calls,
    list_events,
    portfolio_stats,
)
from app.ingest.csv_loader import load_csv
from app.orchestrator.executor import execute as execute_tool
from app.orchestrator.executor import replay_dead_letters
from app.sarvam.voices import ACTIVE_LANGUAGES, voice_for

logger = logging.getLogger("emi.api")
router = APIRouter(prefix="/api")


# --- health / config ---------------------------------------------------------
@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": db_flavour(),
        "sarvam_configured": not settings.offline_mode,
        "mode": "offline-mock" if settings.offline_mode else "live",
        "models": {
            "stt": settings.stt_model, "tts": settings.tts_model,
            "llm": settings.llm_model, "translate": settings.translate_model,
        },
        "tts_transport": settings.tts_transport,
        "reasoning_effort": settings.reasoning_effort,
        "in_calling_window": in_calling_window(),
        # Whether the window is actually *enforced* — a demo override can be on.
        # The UI needs both to avoid warning about a gate that is not active.
        "enforce_calling_window": settings.enforce_calling_window,
        "languages": list(ACTIVE_LANGUAGES),
    }


# --- borrowers / dialer -----------------------------------------------------
@router.get("/borrowers")
def borrowers(s: Session = Depends(get_session)) -> list[dict]:
    out = []
    for b in list_borrowers(s):
        gate = precall_check(b)
        out.append({
            "loan_id": b.loan_id,
            "name": b.name,
            "phone": b.phone,
            "language": b.language,
            "voice": voice_for(b.language),
            "emi_rupees": b.emi_amount_paise / 100.0,
            "due_date": b.due_date.isoformat(),
            "dpd": b.dpd,
            "product": b.product,
            "consent": b.consent,
            "dnd_registered": b.dnd_registered,
            "attempts_today": b.attempts_today,
            "callable": gate.allowed,
            "block_reasons": [{"reason": r, "regulation": g} for r, g in gate.reasons],
        })
    return out


@router.get("/borrowers/{loan_id}/precheck")
def precheck(loan_id: str, s: Session = Depends(get_session)) -> dict:
    b = get_borrower(s, loan_id)
    if b is None:
        raise HTTPException(404, f"unknown loan_id {loan_id}")
    gate = precall_check(b)
    return {
        "loan_id": loan_id,
        "allowed": gate.allowed,
        "checks": gate.checks,
        "reasons": [{"reason": r, "regulation": g} for r, g in gate.reasons],
    }


@router.post("/borrowers/reset-attempts")
def reset_attempts(loan_id: str | None = None, s: Session = Depends(get_session)) -> dict:
    """Clear today's attempt counters — what the dialer does at the day boundary.

    RBI caps attempts per borrower per day, so the counter is genuinely daily
    state. Exposed as an endpoint because a demo often needs to re-run a call.
    """
    from app.db.models import Borrower

    q = s.query(Borrower) if loan_id is None else s.query(Borrower).filter(Borrower.loan_id == loan_id)
    n = q.update({Borrower.attempts_today: 0}, synchronize_session=False)
    return {"reset": n, "loan_id": loan_id}


class IngestRequest(BaseModel):
    path: str = Field(default="data/call_list.csv")
    campaign_name: str = Field(default="EMI Reminder - July 2026")


@router.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    """Re-run the SFTP-style CSV load (validate + consent/DND scrub)."""
    path = settings.resolve(req.path)
    if not path.exists():
        raise HTTPException(404, f"call list not found: {path}")
    return load_csv(path, campaign_name=req.campaign_name).as_dict()


# --- calls -------------------------------------------------------------------
@router.get("/calls")
def calls(limit: int = Query(50, le=200), s: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "call_id": c.id,
            "correlation_id": c.correlation_id,
            "loan_id": c.loan_id,
            "borrower": c.borrower.name if c.borrower else None,
            "channel": c.channel.value,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "duration_s": c.duration_s,
            "disposition": c.disposition.value,
            "languages_detected": c.languages_detected,
            "sentiment": c.sentiment,
            "compliance": c.compliance,
            "latency_stats": c.latency_stats,
            "cost_rupees": round((c.cost_paise or 0) / 100.0, 2),
            "summary": c.summary,
        }
        for c in list_calls(s, limit)
    ]


@router.get("/calls/{call_id}")
def call_detail(call_id: str, s: Session = Depends(get_session)) -> dict:
    c = get_call(s, call_id)
    if c is None:
        raise HTTPException(404, f"unknown call {call_id}")
    report = get_analytics(s, call_id)
    return {
        "call_id": c.id,
        "correlation_id": c.correlation_id,
        "loan_id": c.loan_id,
        "borrower": {
            "name": c.borrower.name, "language": c.borrower.language,
            "emi_rupees": c.borrower.emi_amount_paise / 100.0, "dpd": c.borrower.dpd,
        } if c.borrower else None,
        "channel": c.channel.value,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "ended_at": c.ended_at.isoformat() if c.ended_at else None,
        "duration_s": c.duration_s,
        "disposition": c.disposition.value,
        "languages_detected": c.languages_detected,
        "compliance": c.compliance,
        "latency_stats": c.latency_stats,
        "cost_rupees": round((c.cost_paise or 0) / 100.0, 2),
        "transcript": call_transcript(s, call_id),
        "tool_invocations": [
            {
                "name": t.name, "arguments": t.arguments, "result": t.result,
                "status": t.status.value, "attempts": t.attempts,
                "idempotency_key": t.idempotency_key[:16], "error": t.error,
            }
            for t in c.tool_invocations
        ],
        "events": [
            {"stage": e.stage, "payload": e.payload,
             "at": e.created_at.isoformat() if e.created_at else None}
            for e in list_events(s, call_id)
        ],
        "analytics": {
            "summary": report.summary,
            "summary_english": report.summary_english,
            "sentiment": report.sentiment,
            "sentiment_score": report.sentiment_score,
            "disposition_predicted": report.disposition_predicted,
            "intents": report.intents,
            "objections": report.objections,
            "qa_score": report.qa_score,
            "compliance_flags": report.compliance_flags,
            "model_versions": report.model_versions,
        } if report else None,
    }


# --- tools as webhooks -------------------------------------------------------
class ToolRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


@router.post("/tools/invoke")
async def invoke_tool(
    req: ToolRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Run an orchestrator tool over HTTP.

    Same code path the voice agent uses, exposed so an n8n / LangGraph flow (or a
    curl during the demo) can drive it. Send the same ``Idempotency-Key`` twice
    and the second call replays the first result instead of repeating the effect.
    """
    result = await execute_tool(
        req.name, req.arguments, call_id=req.call_id, explicit_key=idempotency_key
    )
    return {
        "ok": result.ok, "name": result.name, "data": result.data,
        "replayed": result.replayed, "error": result.error,
    }


@router.post("/tools/replay-dead-letters")
async def replay_dlq() -> dict:
    return {"replayed": await replay_dead_letters()}


# --- analytics ---------------------------------------------------------------
@router.post("/analytics/{call_id}")
async def run_analytics(call_id: str) -> dict:
    try:
        return (await analyse_call(call_id)).as_dict()
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/analytics/run/pending")
async def run_pending_analytics(limit: int = Query(25, le=100)) -> dict:
    results = await analyse_all_pending(limit)
    return {"scored": len(results), "call_ids": [r.call_id for r in results]}


@router.get("/analytics/portfolio")
def analytics_portfolio() -> dict:
    return portfolio_report()


# --- dashboard ---------------------------------------------------------------
@router.get("/stats")
def stats(s: Session = Depends(get_session)) -> dict:
    base = portfolio_stats(s)
    # ROI arithmetic, with the assumptions stated inline (see docs/cost.md).
    human_cost_rupees = 60.0
    automated = base["total_calls"]
    bot_cost = sum((c.cost_paise or 0) for c in list_calls(s, 500)) / 100.0
    avg_bot_cost = round(bot_cost / automated, 2) if automated else 0.0
    base["roi"] = {
        "assumed_human_touch_rupees": human_cost_rupees,
        "avg_automated_call_rupees": avg_bot_cost,
        "saving_per_call_rupees": round(human_cost_rupees - avg_bot_cost, 2),
        "total_saving_rupees": round((human_cost_rupees - avg_bot_cost) * automated, 2),
    }
    return base


@router.get("/steps")
def steps(limit: int = Query(300, le=2000), call_id: str | None = None) -> list[dict]:
    """Tail ``logs/steps.jsonl`` — the step-by-step trace, for the walkthrough."""
    path: Path = settings.resolve(settings.step_log_path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit * 3 :]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if call_id and rec.get("call_id") != call_id:
            continue
        rows.append(rec)
    return rows[-limit:]
