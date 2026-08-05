"""REST surface: borrowers, calls, tools-as-webhooks, analytics, dashboard data."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter, Body, Depends, File, Form, Header, HTTPException, Query, UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openpyxl.styles import Font, PatternFill
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.guardrails import in_calling_window, precall_check
from app.analytics.pipeline import analyse_all_pending, analyse_call, portfolio_report
from app.config import settings
from app.db.base import db_flavour, get_session, session_scope
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
from app.ingest.excel_loader import ExcelIngestError, xlsx_to_csv_text
from app.orchestrator.executor import execute as execute_tool
from app.orchestrator.executor import replay_dead_letters
from app.sarvam.voices import ACTIVE_LANGUAGES, voice_for

# Disposition colouring for the export. Green = money committed, amber = still
# open and needs a human, red = no outcome reached.
DISPOSITION_FILL = {
    "PTP": "C6EFCE", "PAID": "A9D08E", "LINK_SENT": "D9E1F2",
    "CALLBACK": "FFF2CC", "ESCALATED": "FCE4D6", "DISPUTE": "FCE4D6",
    "REFUSED": "FFC7CE", "WRONG_NUMBER": "E7E6E6",
    "NO_ANSWER": "F2F2F2", "INCOMPLETE": "FFC7CE",
}
DISPOSITION_FONT = {
    "PTP": "1E7A3C", "PAID": "0B5A28", "REFUSED": "9C0006",
    "INCOMPLETE": "9C0006", "DISPUTE": "8A4B08", "ESCALATED": "8A4B08",
}
DISPOSITION_MEANING = {
    "PTP": "Promise to pay captured - a date and an amount were given",
    "PAID": "Already paid, or paying during the call",
    "LINK_SENT": "Payment link delivered, no date committed",
    "CALLBACK": "Asked to be called back later",
    "ESCALATED": "Handed to a human agent",
    "DISPUTE": "Borrower disputes the amount - needs a human",
    "REFUSED": "Refused to pay",
    "WRONG_NUMBER": "Not the borrower - flagged, will not be called again",
    "NO_ANSWER": "No usable response from the borrower",
    "INCOMPLETE": "Call dropped before an outcome was reached",
}

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
def borrowers(
    limit: int = Query(1000, le=5000, description="Cap on rows returned"),
    s: Session = Depends(get_session),
) -> list[dict]:
    # list_borrowers defaults to 200. On a real campaign that silently truncates
    # the callable universe, so the UI would show 200 while ingest reported 431 -
    # a discrepancy an auditor (or an audience) will spot immediately.
    out = []
    for b in list_borrowers(s, limit=limit):
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


# --- call-list upload --------------------------------------------------------
# The demo starts from a spreadsheet on someone's desktop, so the browser has to be
# able to hand the file over. /ingest (above) takes a *server-side path*, which is
# the right shape for the production SFTP feed and the wrong shape for a UI.
@router.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(..., description=".xlsx or .csv call list"),
    campaign_name: str = Form("Uploaded call list"),
    sheet: str | None = Form(None),
    replace: bool = Form(True, description="Replace the callable universe "
                                            "rather than adding to it"),
) -> dict:
    """Validate, scrub and load an uploaded call list.

    Runs the same loader as the SFTP path, so the consent/DND scrub and the header
    contract are enforced in exactly one place. Returns the scrub report including
    per-row rejections, because "which borrowers did you drop, and under what rule"
    is the question an auditor actually asks.
    """
    name = (file.filename or "upload").strip()
    suffix = Path(name).suffix.lower()
    if suffix not in {".xlsx", ".xlsm", ".csv", ".txt"}:
        raise HTTPException(400, f"unsupported file type {suffix or '(none)'}; use .xlsx or .csv")

    data = await file.read()
    if not data:
        raise HTTPException(400, "uploaded file is empty")
    # A call list is kilobytes. Anything larger is a mistake, and reading it into
    # memory to discover that is the mistake compounding.
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "file larger than 8 MB; call lists are expected to be far smaller")

    used_sheet = None
    if suffix in {".xlsx", ".xlsm"}:
        try:
            csv_text, used_sheet = xlsx_to_csv_text(data, sheet=sheet)
        except ExcelIngestError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        try:
            csv_text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Exports from Windows tooling are frequently cp1252, not UTF-8.
            csv_text = data.decode("cp1252", errors="replace")

    # An ingested file IS the callable universe for that cycle, not an addition to
    # whatever was loaded before. Without this, uploading a 3-row list on top of a
    # 500-row one leaves 500 callable and the scrub report describes 3 - and a
    # borrower whose consent has since been withdrawn stays callable forever,
    # which is the exact outcome the DPDP scrub exists to prevent.
    #
    # Borrowers that already have call records are kept: deleting them would take
    # the CDR and the audit trail with them.
    removed = 0
    if replace:
        with session_scope() as sess:
            rows = sess.execute(
                text(
                    "DELETE FROM borrowers WHERE loan_id NOT IN "
                    "(SELECT DISTINCT loan_id FROM calls WHERE loan_id IS NOT NULL)"
                )
            )
            removed = rows.rowcount or 0
        logger.info("upload replace: cleared %d borrowers with no call history", removed)

    try:
        report = load_csv(io.StringIO(csv_text), campaign_name=campaign_name)
    except ValueError as exc:
        # Missing required columns even after aliasing: a real header contract failure.
        raise HTTPException(422, str(exc)) from exc

    report.sheet = used_sheet
    payload = report.as_dict()
    payload["source"] = name
    payload["replaced_universe"] = bool(replace)
    payload["removed_before_load"] = removed
    logger.info("upload %s: %d/%d loaded", name, report.loaded, report.total_rows)
    return payload


# --- spreadsheet export ------------------------------------------------------
def _autosize(ws) -> None:
    for column in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in column)
        ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 48)


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    d = getattr(obj, "__dict__", {}) or {}
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _breach_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _promises_by_call(s) -> dict[str, dict]:
    """Promise-to-pay per call, so the export can show the commercial outcome.

    A disposition of PTP without a date and an amount is not a promise to pay -
    it is a call that sounded encouraging. Joining the actual PTP record is what
    makes the export usable for follow-up.
    """
    rows = s.execute(text(
        "SELECT call_id, promised_date, amount_paise FROM promises_to_pay "
        "WHERE call_id IS NOT NULL"
    )).fetchall()
    return {r[0]: {"promised_date": r[1], "amount_paise": r[2]} for r in rows}


def _export_rows(s, limit: int) -> list[dict]:
    """One flat record per call, analytics and PTP joined in where they exist."""
    ptps = _promises_by_call(s)
    out = []
    for c in list_calls(s, limit=limit):
        rec = _as_dict(c)
        cid = rec.get("id") or rec.get("call_id")
        a = _as_dict(get_analytics(s, cid) or {})
        ptp = ptps.get(cid) or {}
        out.append({
            "promised_date": str(ptp.get("promised_date") or ""),
            "promised_amount": (ptp.get("amount_paise") or 0) / 100.0 if ptp else "",
            "call_id": cid,
            "correlation_id": rec.get("correlation_id"),
            "loan_id": rec.get("loan_id"),
            "language": rec.get("language"),
            "disposition": str(rec.get("disposition") or ""),
            "duration_s": rec.get("duration_s"),
            "started_at": str(rec.get("started_at") or ""),
            "qa_score": a.get("qa_score"),
            "sentiment": str(a.get("sentiment") or ""),
            "summary_en": (a.get("summary_en") or "")[:500],
            "breaches": _breach_list(a.get("breaches")),
        })
    return out


@router.get("/export/analytics.xlsx")
def export_analytics(limit: int = Query(500, le=5000), s: Session = Depends(get_session)):
    """Every scored call as a workbook.

    A collections floor runs on spreadsheets and a compliance reviewer will ask for
    one, so this is the format the audit trail leaves the system in. Three sheets:
    the portfolio summary, one row per call, and the breaches on their own so the
    exceptions are not buried among the passes.
    """
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(500, "openpyxl is not installed on the server") from exc

    rows = _export_rows(s, limit)
    wb = Workbook()

    summary = wb.active
    summary.title = "Summary"
    summary.append(["Metric", "Value"])
    for key, value in portfolio_report().items():
        summary.append([key, json.dumps(value) if isinstance(value, (dict, list)) else value])
    _autosize(summary)

    detail = wb.create_sheet("Calls")
    headers = ["call_id", "correlation_id", "loan_id", "language", "disposition",
               "promised_date", "promised_amount", "duration_s", "started_at",
               "qa_score", "sentiment", "summary_en", "breaches"]
    detail.append(headers)
    for r in rows:
        detail.append([r["call_id"], r["correlation_id"], r["loan_id"], r["language"],
                       r["disposition"], r["promised_date"], r["promised_amount"],
                       r["duration_s"], r["started_at"], r["qa_score"],
                       r["sentiment"], r["summary_en"], ", ".join(map(str, r["breaches"]))])

    # Colour the disposition column. A collections manager scans this sheet for
    # outcomes, not for call ids, so the outcome is what should be visible without
    # reading. Green = money committed, amber = still open, red = no outcome.
    for row in detail.iter_rows(min_row=2, max_row=detail.max_row):
        disp = str(row[4].value or "").upper()
        fill = DISPOSITION_FILL.get(disp)
        if fill:
            row[4].fill = PatternFill("solid", fgColor=fill)
            row[4].font = Font(bold=True, color=DISPOSITION_FONT.get(disp, "1F3864"))
        # A PTP with no date is a disposition that overstates itself - flag it.
        if disp == "PTP" and not row[5].value:
            row[5].fill = PatternFill("solid", fgColor="FFF2CC")
            row[5].value = "MISSING DATE"
    detail.freeze_panes = "A2"
    detail.auto_filter.ref = detail.dimensions
    _autosize(detail)

    # A legend, so nobody has to guess what the colours mean.
    legend = wb.create_sheet("Legend")
    legend.append(["Disposition", "Meaning", "Colour"])
    for disp, meaning in DISPOSITION_MEANING.items():
        legend.append([disp, meaning, ""])
        cell = legend.cell(row=legend.max_row, column=3)
        f = DISPOSITION_FILL.get(disp)
        if f:
            cell.fill = PatternFill("solid", fgColor=f)
    _autosize(legend)

    breaches_ws = wb.create_sheet("Breaches")
    breaches_ws.append(["call_id", "loan_id", "breach"])
    for r in rows:
        for b in r["breaches"]:
            breaches_ws.append([r["call_id"], r["loan_id"], str(b)])
    _autosize(breaches_ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=collections-analytics.xlsx"},
    )


@router.get("/export/analytics.csv")
def export_analytics_csv(limit: int = Query(500, le=5000), s: Session = Depends(get_session)):
    """Same data as CSV, for anything that would rather not parse a workbook."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["call_id", "loan_id", "language", "disposition", "promised_date",
                "promised_amount", "duration_s", "started_at", "qa_score",
                "sentiment", "breaches"])
    for r in _export_rows(s, limit):
        w.writerow([r["call_id"], r["loan_id"], r["language"], r["disposition"],
                    r["promised_date"], r["promised_amount"],
                    r["duration_s"], r["started_at"], r["qa_score"], r["sentiment"],
                    "; ".join(map(str, r["breaches"]))])
    # utf-8-sig so Excel opens Devanagari correctly instead of showing mojibake.
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=collections-analytics.csv"},
    )
