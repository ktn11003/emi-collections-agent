"""Tool execution — exactly once, with retries and a dead-letter path.

When sarvam-105b decides "send a payment link", something real has to happen
reliably. This module is the boundary between a language model's intention and a
side effect the business is accountable for:

* **Idempotency.** A stable key is derived from (call, tool, business fields).
  ``tool_invocations.idempotency_key`` is UNIQUE, so a repeated tool call — the
  model restating itself, a client retry, a socket reconnect — returns the cached
  result instead of sending a second link.
* **Retries.** Transient downstream failures are retried with backoff; permanent
  ones are dead-lettered with the error preserved.
* **Every effect is a row.** Payment links, PTPs, escalations and CRM writes are
  persisted before being reported back to the model, so the transcript and the
  database can never disagree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.agent.tools import IDEMPOTENT_ON, TOOL_NAMES
from app.config import settings
from app.db.base import session_scope
from app.db.models import Disposition, ToolStatus
from app.db.repo import (
    add_audit,
    add_compliance_event,
    add_crm_writeback,
    add_escalation,
    add_event,
    add_payment_link,
    add_ptp,
    begin_tool_invocation,
    complete_tool_invocation,
    find_tool_invocation,
    get_borrower,
)
from app.orchestrator.adapters import human_queue, lms, messaging, now_utc, payments
from app.steplog import step

logger = logging.getLogger("emi.orchestrator")

MAX_TOOL_ATTEMPTS = 3


@dataclass(slots=True)
class ToolResult:
    name: str
    ok: bool
    data: dict[str, Any]
    replayed: bool = False       # served from the idempotency cache
    error: str | None = None
    # Short natural-language line fed back to the model as the tool result.
    speech_hint: str | None = None

    def as_tool_message(self, tool_call_id: str) -> dict[str, Any]:
        payload = {"ok": self.ok, **self.data}
        if self.error:
            payload["error"] = self.error
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": self.name,
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        }


def idempotency_key(call_id: str | None, name: str, args: dict) -> str:
    """Stable key over the *business* fields, not the whole argument blob.

    Two calls to ``send_payment_link`` for the same loan and amount within one
    call are the same intent, even if the model phrased the arguments slightly
    differently.
    """
    fields = IDEMPOTENT_ON.get(name, tuple(sorted(args)))
    parts = [call_id or "-", name] + [f"{k}={args.get(k)!r}" for k in fields]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:48]


def _rupees_to_paise(amount: Any) -> int:
    return int(round(float(amount) * 100))


# --- individual tools --------------------------------------------------------
async def _send_payment_link(call_id: str | None, args: dict) -> ToolResult:
    loan_id = str(args["loan_id"])
    amount_paise = _rupees_to_paise(args["amount"])
    channel = str(args.get("channel") or "WHATSAPP").upper()

    with session_scope() as s:
        borrower = get_borrower(s, loan_id)
        phone = borrower.phone if borrower else ""
        name = borrower.name if borrower else ""

    link = await payments.create_link(loan_id=loan_id, amount_paise=amount_paise, phone=phone)
    delivery = await messaging.send_template(
        phone=phone,
        template="emi_payment_link",
        params={
            "name": name,
            "lender": settings.lender_name,
            # Grouped for reading, since this is text a person sees.
            "amount": f"{amount_paise / 100.0:,.0f}",
            "url": link.url,
        },
        channel=channel,
    )

    with session_scope() as s:
        add_payment_link(
            s,
            loan_id=loan_id,
            call_id=call_id,
            amount_paise=amount_paise,
            url=link.url,
            provider_ref=link.provider_ref,
            status="SENT" if delivery.accepted else "CREATED",
            channel_sent=channel,
        )
        add_event(s, call_id, "tool.payment_link", {"ref": link.provider_ref, "channel": channel})

    return ToolResult(
        name="send_payment_link",
        ok=delivery.accepted,
        data={"url": link.url, "reference": link.provider_ref, "channel": channel},
        speech_hint=f"Payment link sent on {channel.title()}.",
    )


async def _schedule_ptp(call_id: str | None, args: dict) -> ToolResult:
    loan_id = str(args["loan_id"])
    amount_paise = _rupees_to_paise(args.get("amount") or 0)
    raw_date = str(args["promised_date"])
    try:
        promised = date.fromisoformat(raw_date[:10])
    except ValueError:
        return ToolResult(
            name="schedule_ptp", ok=False, data={},
            error=f"unparseable promised_date {raw_date!r}; ask the borrower for a specific date",
        )

    # A promise more than 30 days out is not a promise; keep the data honest.
    if promised > date.today() + timedelta(days=30):
        return ToolResult(
            name="schedule_ptp", ok=False, data={"promised_date": promised.isoformat()},
            error="date is more than 30 days away; ask for a date within the next month",
        )

    with session_scope() as s:
        borrower = get_borrower(s, loan_id)
        if amount_paise == 0 and borrower:
            amount_paise = borrower.emi_amount_paise
        add_ptp(
            s,
            loan_id=loan_id,
            call_id=call_id,
            promised_date=promised,
            amount_paise=amount_paise,
            captured_language=(borrower.language if borrower else None),
        )
        add_crm_writeback(
            s, call_id=call_id, loan_id=loan_id, system="LMS",
            payload={"disposition": "PTP", "ptp_date": promised.isoformat(),
                     "amount": amount_paise / 100.0, "next_action_date": promised.isoformat()},
        )
        add_event(s, call_id, "tool.ptp", {"date": promised.isoformat()})

    await lms.update_disposition(
        loan_id=loan_id,
        payload={"disposition": "PTP", "ptp_date": promised.isoformat(), "amount": amount_paise / 100.0},
    )
    return ToolResult(
        name="schedule_ptp", ok=True,
        data={"promised_date": promised.isoformat(), "amount": amount_paise / 100.0},
        speech_hint=f"Promise to pay recorded for {promised.strftime('%d %B')}.",
    )


async def _escalate_to_human(call_id: str | None, args: dict) -> ToolResult:
    loan_id = str(args["loan_id"])
    reason = str(args.get("reason") or "COMPLEX_QUERY").upper()
    context = str(args.get("context") or "")
    queue = "COLLECTIONS_GRIEVANCE" if reason in ("GRIEVANCE", "DISPUTE") else "COLLECTIONS_TIER2"

    ticket = await human_queue.enqueue(loan_id=loan_id, reason=reason, context=context, queue=queue)
    with session_scope() as s:
        add_escalation(s, call_id=call_id, loan_id=loan_id, reason=reason,
                       context_summary=context, queue=queue)
        add_crm_writeback(s, call_id=call_id, loan_id=loan_id, system="CRM",
                          payload={"case_type": reason, "queue": queue, "ticket": ticket})
        add_event(s, call_id, "tool.escalation", {"reason": reason, "queue": queue})

    return ToolResult(
        name="escalate_to_human", ok=True,
        data={"ticket": ticket, "queue": queue, "reason": reason},
        speech_hint="Transferring to a human agent now.",
    )


async def _mark_disposition(call_id: str | None, args: dict) -> ToolResult:
    loan_id = str(args["loan_id"])
    raw = str(args["disposition"]).upper()
    try:
        disposition = Disposition(raw)
    except ValueError:
        return ToolResult(name="mark_disposition", ok=False, data={},
                          error=f"unknown disposition {raw!r}")

    notes = str(args.get("notes") or "")
    suppressed = False
    with session_scope() as s:
        # WRONG_NUMBER is not just an outcome, it is an instruction: this number
        # does not belong to the borrower, so it must never be dialled again.
        # Setting the DND flag is what the pre-call gate already reads, so one
        # write closes the loop without a second suppression list.
        if disposition is Disposition.WRONG_NUMBER:
            borrower = get_borrower(s, loan_id)
            if borrower is not None and not borrower.dnd_registered:
                borrower.dnd_registered = True
                suppressed = True
                add_compliance_event(
                    s, call_id, "number_suppressed", passed=True,
                    detail=f"{loan_id} reported wrong number; suppressed from future dialling",
                    regulation="TRAI DND / UCC",
                )
                add_audit(s, "borrower.suppressed", entity="borrower", entity_id=loan_id,
                          payload={"reason": "WRONG_NUMBER", "call_id": call_id})

        add_crm_writeback(s, call_id=call_id, loan_id=loan_id, system="LMS",
                          payload={"disposition": disposition.value, "notes": notes,
                                   "suppress": suppressed})
        add_event(s, call_id, "tool.disposition",
                  {"disposition": disposition.value, "suppressed": suppressed})
    await lms.update_disposition(
        loan_id=loan_id, payload={"disposition": disposition.value, "notes": notes}
    )
    return ToolResult(
        name="mark_disposition", ok=True,
        data={"disposition": disposition.value, "suppressed": suppressed},
        speech_hint="Outcome recorded.",
    )


async def _schedule_callback(call_id: str | None, args: dict) -> ToolResult:
    loan_id = str(args["loan_id"])
    raw = str(args["callback_at"])
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ToolResult(name="schedule_callback", ok=False, data={},
                          error=f"unparseable callback_at {raw!r}")

    with session_scope() as s:
        add_crm_writeback(s, call_id=call_id, loan_id=loan_id, system="DIALER",
                          payload={"action": "CALLBACK", "callback_at": when.isoformat()})
        add_event(s, call_id, "tool.callback", {"callback_at": when.isoformat()})
    return ToolResult(
        name="schedule_callback", ok=True, data={"callback_at": when.isoformat()},
        speech_hint=f"Callback scheduled for {when.strftime('%d %B, %I:%M %p')}.",
    )


async def _end_call(call_id: str | None, args: dict) -> ToolResult:
    """Agent-initiated hangup.

    The side effect lives in the session, not here: this handler only records
    that the agent asked to end, so the CDR shows the call was concluded rather
    than dropped. Returning no speech_hint is deliberate -- a narration pass
    after a hangup is exactly the loop this tool exists to stop.
    """
    loan_id = str(args["loan_id"])
    reason = str(args.get("reason") or "")[:200]
    with session_scope() as s:
        add_event(s, call_id, "tool.end_call", {"loan_id": loan_id, "reason": reason})
    return ToolResult(name="end_call", ok=True, data={"reason": reason})


_HANDLERS = {
    "send_payment_link": _send_payment_link,
    "schedule_ptp": _schedule_ptp,
    "escalate_to_human": _escalate_to_human,
    "mark_disposition": _mark_disposition,
    "schedule_callback": _schedule_callback,
    "end_call": _end_call,
}


# --- the executor ------------------------------------------------------------
async def execute(
    name: str, args: dict, *, call_id: str | None = None, explicit_key: str | None = None
) -> ToolResult:
    """Run one tool exactly once, with retries and a dead-letter fallback."""
    if name not in TOOL_NAMES or name not in _HANDLERS:
        return ToolResult(name=name, ok=False, data={}, error=f"unknown tool {name!r}")

    key = explicit_key or idempotency_key(call_id, name, args)

    # 1. Already done? Replay the stored result — this is the exactly-once path.
    with session_scope() as s:
        existing = find_tool_invocation(s, key)
        if existing is not None and existing.status == ToolStatus.SUCCEEDED:
            step("tool.executed", call_id, tool=name, replayed=True, idem=key[:12])
            return ToolResult(name=name, ok=True, data=existing.result or {}, replayed=True)

    # 2. Claim the key. A concurrent duplicate loses the race on the UNIQUE
    #    index and replays instead.
    try:
        with session_scope() as s:
            ti = begin_tool_invocation(
                s, call_id=call_id, name=name, arguments=args, idempotency_key=key
            )
            ti_id = ti.id
    except IntegrityError:
        await asyncio.sleep(0.15)
        with session_scope() as s:
            existing = find_tool_invocation(s, key)
            if existing and existing.status == ToolStatus.SUCCEEDED:
                return ToolResult(name=name, ok=True, data=existing.result or {}, replayed=True)
        return ToolResult(name=name, ok=False, data={}, error="duplicate tool invocation in flight")

    # 3. Execute, with backoff on transient failure.
    handler = _HANDLERS[name]
    last_error: str | None = None
    for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
        try:
            result = await handler(call_id, args)
            with session_scope() as s:
                ti = find_tool_invocation(s, key)
                if ti is not None:
                    ti.attempts = attempt
                    complete_tool_invocation(
                        s, ti,
                        result=result.data,
                        status=ToolStatus.SUCCEEDED if result.ok else ToolStatus.FAILED,
                        error=result.error,
                    )
                add_audit(s, "tool.executed", entity="tool_invocation", entity_id=ti_id,
                          payload={"name": name, "ok": result.ok, "attempt": attempt})
            step("tool.executed", call_id, tool=name, ok=result.ok, attempt=attempt, idem=key[:12])
            return result
        except Exception as exc:  # noqa: BLE001 - downstream can fail any way
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("tool %s attempt %d failed: %s", name, attempt, last_error)
            if attempt < MAX_TOOL_ATTEMPTS:
                await asyncio.sleep(0.3 * (2 ** (attempt - 1)))

    # 4. Dead-letter. The intent is preserved for reconciliation, and the model
    #    is told so it can tell the borrower something honest.
    with session_scope() as s:
        ti = find_tool_invocation(s, key)
        if ti is not None:
            ti.attempts = MAX_TOOL_ATTEMPTS
            complete_tool_invocation(s, ti, result=None, status=ToolStatus.DEAD_LETTERED, error=last_error)
        add_event(s, call_id, "tool.dead_letter", {"name": name, "error": last_error})
    step("error", call_id, tool=name, dead_lettered=True, error=last_error)
    return ToolResult(
        name=name, ok=False, data={}, error=last_error,
        speech_hint="I could not complete that right now; a colleague will follow up.",
    )


async def replay_dead_letters(limit: int = 50) -> int:
    """Reconcile: retry dead-lettered effects after the downstream recovers."""
    from sqlalchemy import select

    from app.db.models import ToolInvocation

    replayed = 0
    with session_scope() as s:
        rows = s.scalars(
            select(ToolInvocation)
            .where(ToolInvocation.status == ToolStatus.DEAD_LETTERED)
            .limit(limit)
        ).all()
        pending = [(r.id, r.call_id, r.name, dict(r.arguments or {})) for r in rows]

    for row_id, call_id, name, args in pending:
        # Drop the dead row so execute() can re-claim the same idempotency key.
        # Safe because the effect never landed: the downstream call is what
        # failed, and each adapter also carries its own reference id.
        with session_scope() as s:
            ti = s.get(ToolInvocation, row_id)
            if ti is not None:
                s.delete(ti)
        result = await execute(name, args, call_id=call_id)
        replayed += 1 if result.ok else 0
    return replayed
