"""Downstream system adapters: payment gateway, LMS/CRM, WhatsApp.

Each is mocked behind the interface the real thing would expose, so swapping in
a live endpoint is a change to one class, not to the agent. The mocks still write
to the database, so the demo shows a genuine end-to-end chain — the call
produces a payment-link row, a PTP row and a CRM outbox row you can query.

`mock://` in .env selects the mock; an `https://` base makes the same adapter
issue real HTTP calls.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger("emi.downstream")


@dataclass(slots=True)
class PaymentLinkResult:
    url: str
    provider_ref: str
    amount_paise: int
    expires_at: datetime | None = None


@dataclass(slots=True)
class DeliveryResult:
    channel: str
    provider_ref: str
    accepted: bool


class PaymentGateway:
    """Creates short-lived payment links.

    Note what is *not* here: no card number, no CVV, no UPI PIN ever enters this
    system. We hand out a link and the gateway's own hosted page takes the
    payment — which keeps the whole platform out of PCI-DSS scope.
    """

    def __init__(self, base: str | None = None) -> None:
        self.base = base or settings.payment_gateway_base
        self.mock = self.base.startswith("mock://")

    async def create_link(self, *, loan_id: str, amount_paise: int, phone: str) -> PaymentLinkResult:
        ref = "pl_" + secrets.token_hex(8)
        if self.mock:
            # Deterministic-looking short code, as a real gateway would return.
            code = hashlib.sha256(f"{loan_id}{amount_paise}{ref}".encode()).hexdigest()[:10]
            logger.info("mock payment link %s for %s (%d paise)", ref, loan_id, amount_paise)
            return PaymentLinkResult(
                url=f"https://pay.lender.example/l/{code}", provider_ref=ref, amount_paise=amount_paise
            )

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self.base.rstrip('/')}/v1/payment_links",
                json={
                    "reference_id": ref,
                    "amount": amount_paise,
                    "currency": "INR",
                    "customer": {"contact": phone},
                    "notes": {"loan_id": loan_id},
                },
            )
            r.raise_for_status()
            data = r.json()
        return PaymentLinkResult(
            url=data["short_url"], provider_ref=data.get("id", ref), amount_paise=amount_paise
        )


class LoanManagementSystem:
    """LMS / LOS / CRM write-back.

    Real deployments are usually a REST call or a Kafka topic. Either way the
    write is recorded in ``crm_writebacks`` first, so an outage means "retry
    later", never "outcome lost".
    """

    def __init__(self, base: str | None = None) -> None:
        self.base = base or settings.crm_base
        self.mock = self.base.startswith("mock://")

    async def update_disposition(self, *, loan_id: str, payload: dict) -> bool:
        if self.mock:
            logger.info("mock LMS write %s: %s", loan_id, payload)
            return True
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.base.rstrip('/')}/loans/{loan_id}/dispositions", json=payload)
            return r.status_code < 300


class MessagingGateway:
    """WhatsApp / SMS delivery of the payment link and confirmations."""

    def __init__(self, base: str | None = None) -> None:
        self.base = base or settings.whatsapp_base
        self.mock = self.base.startswith("mock://")

    async def send_template(
        self, *, phone: str, template: str, params: dict, channel: str = "WHATSAPP"
    ) -> DeliveryResult:
        ref = "msg_" + secrets.token_hex(6)
        if self.mock:
            logger.info("mock %s -> %s template=%s params=%s", channel, phone[-4:], template, params)
            return DeliveryResult(channel=channel, provider_ref=ref, accepted=True)

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self.base.rstrip('/')}/messages",
                json={"to": phone, "type": "template", "template": {"name": template, "params": params}},
            )
            return DeliveryResult(
                channel=channel, provider_ref=r.json().get("id", ref), accepted=r.status_code < 300
            )


class HumanAgentQueue:
    """Warm transfer target. In production: the dialer's agent queue."""

    async def enqueue(self, *, loan_id: str, reason: str, context: str, queue: str) -> str:
        ticket = "esc_" + secrets.token_hex(5)
        logger.info("escalation %s -> queue=%s reason=%s", ticket, queue, reason)
        return ticket


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


payments = PaymentGateway()
lms = LoanManagementSystem()
messaging = MessagingGateway()
human_queue = HumanAgentQueue()
