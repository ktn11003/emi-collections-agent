"""Tool (function-calling) schemas exposed to sarvam-105b.

Deliberately small. Every tool is a *real* side effect with a row in the
database, an idempotency key, and a compliance rationale. Tools the bot must not
have (waivers, settlements, taking card details) are absent by construction —
the model cannot call what it was never given.
"""

from __future__ import annotations

from typing import Any

SEND_PAYMENT_LINK = {
    "type": "function",
    "function": {
        "name": "send_payment_link",
        "description": (
            "Generate a secure payment link for the borrower's overdue EMI and send it "
            "on WhatsApp or SMS. Use when the borrower wants to pay now or asks for a link. "
            "Never ask for card, UPI PIN or OTP details — the link handles payment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string", "description": "The borrower's loan account id."},
                "amount": {"type": "number", "description": "Amount in rupees. Use the EMI amount unless the borrower states a different agreed amount."},
                "channel": {"type": "string", "enum": ["WHATSAPP", "SMS"], "description": "Delivery channel. Default WHATSAPP."},
            },
            "required": ["loan_id", "amount"],
        },
    },
}

SCHEDULE_PTP = {
    "type": "function",
    "function": {
        "name": "schedule_ptp",
        "description": (
            "Record a promise-to-pay when the borrower commits to a specific date. "
            "This is the primary success outcome of the call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "promised_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) the borrower committed to."},
                "amount": {"type": "number", "description": "Amount in rupees the borrower committed to pay."},
            },
            "required": ["loan_id", "promised_date", "amount"],
        },
    },
}

ESCALATE_TO_HUMAN = {
    "type": "function",
    "function": {
        "name": "escalate_to_human",
        "description": (
            "Transfer to a human collections agent. Call this immediately if the borrower "
            "disputes the amount, is distressed or upset, asks for a human, reports a "
            "bereavement or hardship, or raises a grievance. Do not argue first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": ["DISPUTE", "DISTRESS", "REQUESTED_HUMAN", "HARDSHIP", "GRIEVANCE", "COMPLEX_QUERY"],
                },
                "context": {"type": "string", "description": "One-line summary for the human agent."},
            },
            "required": ["loan_id", "reason"],
        },
    },
}

MARK_DISPOSITION = {
    "type": "function",
    "function": {
        "name": "mark_disposition",
        "description": (
            "Record the final outcome of the call in the loan management system. "
            "Call this before the call ends, always."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "disposition": {
                    "type": "string",
                    # Mirrors db.models.Disposition minus INCOMPLETE, which is the
                    # absence of an outcome and is never something the model picks.
                    "enum": ["PTP", "PAID", "LINK_SENT", "DISPUTE", "WRONG_NUMBER",
                             "NO_ANSWER", "CALLBACK", "REFUSED", "ESCALATED"],
                },
                "notes": {"type": "string", "description": "Short free-text note for the collections officer."},
            },
            "required": ["loan_id", "disposition"],
        },
    },
}

SCHEDULE_CALLBACK = {
    "type": "function",
    "function": {
        "name": "schedule_callback",
        "description": (
            "Schedule a callback when the borrower asks to be called at another time. "
            "Respect the 08:00-19:00 calling window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "callback_at": {"type": "string", "description": "ISO datetime for the callback."},
            },
            "required": ["loan_id", "callback_at"],
        },
    },
}

END_CALL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": (
            "Hang up and end the conversation. Call this immediately after mark_disposition, "
            "once the outcome is recorded and the closing line has been said. This is the ONLY "
            "way to end a call: without it the line stays open and the conversation restarts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "reason": {"type": "string", "description": "One short line: why the call is ending."},
            },
            "required": ["loan_id"],
        },
    },
}

ALL_TOOLS: list[dict[str, Any]] = [
    SEND_PAYMENT_LINK,
    SCHEDULE_PTP,
    ESCALATE_TO_HUMAN,
    MARK_DISPOSITION,
    SCHEDULE_CALLBACK,
    END_CALL,
]

TOOL_NAMES: tuple[str, ...] = tuple(t["function"]["name"] for t in ALL_TOOLS)

# Tools whose effect must never be duplicated, even if the model repeats itself
# or a client retries. The executor derives a stable idempotency key from these.
IDEMPOTENT_ON: dict[str, tuple[str, ...]] = {
    "send_payment_link": ("loan_id", "amount"),
    "schedule_ptp": ("loan_id", "promised_date"),
    "escalate_to_human": ("loan_id", "reason"),
    "mark_disposition": ("loan_id", "disposition"),
    "schedule_callback": ("loan_id", "callback_at"),
    "end_call": ("loan_id",),
}
