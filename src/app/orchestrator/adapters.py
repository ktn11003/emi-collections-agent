"""Downstream system adapters: payment gateway, LMS/CRM, WhatsApp.

Each is mocked behind the interface the real thing would expose, so swapping in
a live endpoint is a change to one class, not to the agent. The mocks still write
to the database, so the demo shows a genuine end-to-end chain — the call
produces a payment-link row, a PTP row and a CRM outbox row you can query.

`mock://` in .env selects the mock; an `https://` base makes the same adapter
issue real HTTP calls.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import settings

def _public_base() -> str:
    """Where the mock payment page is reachable.

    PUBLIC_BASE_URL when set (a tunnel, or a real host, so a phone can open it);
    otherwise the local server, which is what the browser demo needs.
    """
    base = (settings.public_base_url or "").strip().rstrip("/")
    if base:
        return base
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "127.0.0.1") else settings.host
    return f"http://{host}:{settings.port}"


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
                # Served by app/routers/pay.py so the link in the WhatsApp message
                # actually opens. The old value was on a domain that does not
                # resolve, which is fine in a log and useless in a demo - the first
                # thing anyone does with a payment link is click it.
                url=f"{_public_base()}/pay/{ref}", provider_ref=ref, amount_paise=amount_paise
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


# What the borrower actually receives. Kept here, not in the prompt: the model
# must never compose a message containing a payment URL.
_TEMPLATE_BODIES = {
    "emi_payment_link": (
        "Namaste {name}, {lender} se.\n\n"
        "Aapki EMI Rs {amount} pending hai.\n"
        "Payment link: {url}\n\n"
        "Yeh link sirf aapke liye hai. Kisi ko OTP ya PIN na batayein."
    ),
}


def render_template(template: str, params: dict) -> str:
    """Fill a message body. Unknown templates fall back to the raw params."""
    body = _TEMPLATE_BODIES.get(template)
    if body is None:
        return " ".join(f"{k}: {v}" for k, v in params.items())
    try:
        return body.format(**params)
    except KeyError as exc:
        logger.warning("template %s missing param %s", template, exc)
        return body


def _digits(phone: str) -> str:
    """Gupshup wants a bare msisdn: country code, no '+', no separators."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


class MessagingGateway:
    """WhatsApp / SMS delivery of the payment link and confirmations.

    Three providers, chosen by ``WHATSAPP_PROVIDER``:

    * ``mock``    - logs and reports success. The default, so the repo runs and
      the tests pass with no credentials anywhere.
    * ``gupshup`` - ``POST api.gupshup.io/wa/api/v1/msg``, form-encoded, apikey
      header. The usual choice for Indian volume.
    * ``twilio``  - reuses the ``twilio_*`` credentials already configured for
      telephony, so a deployment using Twilio for voice needs no new account.

    Delivery failures never raise: a borrower who agreed to pay must not lose the
    promise because a message gateway had a bad minute. ``accepted=False`` is
    returned and the executor records it.
    """

    def __init__(self, base: str | None = None) -> None:
        self.base = base or settings.whatsapp_base
        self.provider = settings.whatsapp_provider
        # Naming a real provider selects it. The default whatsapp_base is
        # "mock://whatsapp", so letting the base veto the provider meant setting
        # WHATSAPP_PROVIDER=gupshup silently kept mocking -- a trap, not a safety
        # net. The base only decides for the generic passthrough.
        if self.provider in ("gupshup", "twilio"):
            self.mock = False
        else:
            self.mock = self.base.startswith("mock://")

    async def send_template(
        self, *, phone: str, template: str, params: dict, channel: str = "WHATSAPP"
    ) -> DeliveryResult:
        ref = "msg_" + secrets.token_hex(6)
        body = render_template(template, params)

        override = settings.whatsapp_override_to.strip()
        if override and override != phone:
            logger.warning(
                "WhatsApp override active: message for %s redirected to %s",
                phone or "<no number>", override,
            )
            phone = override

        if self.mock:
            logger.info("mock %s -> ...%s\n%s", channel, phone[-4:], body)
            return DeliveryResult(channel=channel, provider_ref=ref, accepted=True)

        try:
            if self.provider == "gupshup":
                return await self._send_gupshup(phone, body, channel, ref)
            if self.provider == "twilio":
                return await self._send_twilio(phone, body, channel, ref)
            return await self._send_generic(phone, template, params, channel, ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s delivery failed via %s: %s", channel, self.provider, exc)
            return DeliveryResult(channel=channel, provider_ref=ref, accepted=False)

    async def _send_gupshup(self, phone: str, body: str, channel: str, ref: str) -> DeliveryResult:
        if not settings.gupshup_api_key or not settings.gupshup_source:
            logger.warning("gupshup selected but GUPSHUP_API_KEY/SOURCE are unset")
            return DeliveryResult(channel=channel, provider_ref=ref, accepted=False)

        form = {
            "channel": "whatsapp",
            "source": _digits(settings.gupshup_source),
            "destination": _digits(phone),
            "message": json.dumps({"type": "text", "text": body}),
        }
        if settings.gupshup_app_name:
            form["src.name"] = settings.gupshup_app_name

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.gupshup.io/wa/api/v1/msg",
                data=form,
                headers={"apikey": settings.gupshup_api_key,
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
        ok = r.status_code < 300
        if not ok:
            logger.warning("gupshup rejected the message: %s %s", r.status_code, r.text[:200])
        with contextlib.suppress(Exception):
            ref = r.json().get("messageId") or ref
        return DeliveryResult(channel=channel, provider_ref=ref, accepted=ok)

    async def _send_twilio(self, phone: str, body: str, channel: str, ref: str) -> DeliveryResult:
        sid, token = settings.twilio_account_sid, settings.twilio_auth_token
        sender = settings.twilio_whatsapp_from
        if not (sid and token and sender):
            logger.warning("twilio selected but TWILIO_ACCOUNT_SID/AUTH_TOKEN/WHATSAPP_FROM are unset")
            return DeliveryResult(channel=channel, provider_ref=ref, accepted=False)

        to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data={"From": sender, "To": to, "Body": body},
                auth=(sid, token),
            )
        ok = r.status_code < 300
        if not ok:
            logger.warning("twilio rejected the message: %s %s", r.status_code, r.text[:200])
        with contextlib.suppress(Exception):
            ref = r.json().get("sid") or ref
        return DeliveryResult(channel=channel, provider_ref=ref, accepted=ok)

    async def _send_generic(
        self, phone: str, template: str, params: dict, channel: str, ref: str
    ) -> DeliveryResult:
        """Whatever is at ``whatsapp_base`` -- the original placeholder shape."""
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self.base.rstrip('/')}/messages",
                json={"to": phone, "type": "template",
                      "template": {"name": template, "params": params}},
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
