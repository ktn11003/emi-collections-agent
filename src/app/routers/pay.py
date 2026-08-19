"""A mock payment page, so the link the agent sends actually opens.

The real integration is a Razorpay / Cashfree / PayU payment-link API. Until that
exists, `send_payment_link` produced a URL on a domain that does not resolve -
fine for a log, useless in a demo, because the most natural thing for anyone
watching is to click it.

This serves a page at ``/pay/{ref}`` that renders the amount, the loan account and
the expiry from the reference itself. It takes no card details and moves no money;
the page says so, in the page, so nobody can mistake it for a live gateway.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.config import settings
from app.db.base import session_scope
from app.db.models import Borrower, PaymentLink

router = APIRouter()

LINK_TTL_HOURS = 48

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pay {amount} · {lender}</title>
<style>
  :root {{ --ink:#0f1e3c; --muted:#64748b; --line:#e2e8f0; --brand:#1f5fa8; --ok:#1e7a3c; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#f1f5f9; color:var(--ink);
         font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }}
  .card {{ width:min(420px,92vw); background:#fff; border:1px solid var(--line);
          border-radius:16px; padding:28px; box-shadow:0 12px 40px rgba(15,30,60,.10) }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom:22px }}
  .dot {{ width:34px; height:34px; border-radius:9px; background:var(--brand);
         display:grid; place-items:center; color:#fff; font-weight:700; font-size:15px }}
  .brand b {{ font-size:15px }}
  .amt {{ font-size:40px; font-weight:750; letter-spacing:-.5px; margin:0 0 2px }}
  .sub {{ color:var(--muted); font-size:13px; margin:0 0 22px }}
  dl {{ margin:0 0 22px; display:grid; grid-template-columns:auto 1fr; gap:9px 16px; font-size:14px }}
  dt {{ color:var(--muted) }}
  dd {{ margin:0; text-align:right; font-weight:600 }}
  .pay {{ width:100%; padding:14px; border:0; border-radius:10px; background:var(--brand);
         color:#fff; font-size:15px; font-weight:650; cursor:pointer }}
  .pay:hover {{ background:#1a4f8c }}
  .methods {{ display:flex; gap:8px; margin:14px 0 0; justify-content:center;
             color:var(--muted); font-size:12px }}
  .note {{ margin-top:22px; padding:12px 14px; border-radius:9px;
          background:#fff8e6; border:1px solid #f0dca8; color:#7a5a12; font-size:12px }}
  .done {{ display:none; text-align:center }}
  .tick {{ width:56px; height:56px; margin:0 auto 14px; border-radius:50%;
          background:#e8f6ed; color:var(--ok); display:grid; place-items:center; font-size:28px }}
</style>
<div class="card">
  <div id="form">
    <div class="brand"><div class="dot">{initial}</div><b>{lender}</b></div>
    <p class="amt">{amount}</p>
    <p class="sub">EMI payment · {product}</p>
    <dl>
      <dt>Loan account</dt><dd>{loan_id}</dd>
      <dt>Reference</dt><dd>{ref}</dd>
      <dt>Link expires</dt><dd>{expires}</dd>
    </dl>
    <button class="pay" onclick="
      document.getElementById('form').style.display='none';
      document.getElementById('done').style.display='block';">Pay {amount}</button>
    <div class="methods"><span>UPI</span>·<span>Cards</span>·<span>Net banking</span></div>
    <div class="note"><b>Demonstration page.</b> No card details are collected and no
      money moves. In production this URL is issued by the payment gateway, not by
      this application.</div>
  </div>
  <div class="done" id="done">
    <div class="tick">✓</div>
    <p class="amt" style="font-size:24px">Payment recorded</p>
    <p class="sub">{amount} · {loan_id}<br>Reference {ref}</p>
    <div class="note" style="text-align:left"><b>Nothing was actually charged.</b>
      This is the mock gateway that stands in for Razorpay / Cashfree until the
      real integration is wired up.</div>
  </div>
</div>
"""


def _fmt_inr(paise: int) -> str:
    rupees = paise // 100
    s = str(rupees)
    if len(s) <= 3:
        body = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        body = ",".join(parts) + "," + tail
    return f"₹{body}"


@router.get("/pay/{ref}", response_class=HTMLResponse)
def payment_page(ref: str) -> HTMLResponse:
    """Render the mock payment page for a link reference.

    The amount is read from the stored link and the borrower record, never from
    the URL: a link whose amount could be edited by the person paying it would be
    an obvious flaw to anyone technical watching the demo.

    Resolution is by ``payment_links.provider_ref``, which is what the gateway
    actually issues ("pl_<hex>"). An older form embedded the loan id as a prefix
    ("<loan_id>-<suffix>") and is still accepted as a fallback.
    """
    ref = (ref or "").strip()
    if not ref:
        raise HTTPException(404, "unknown payment reference")

    with session_scope() as s:
        link = s.scalar(select(PaymentLink).where(PaymentLink.provider_ref == ref))
        loan_id = link.loan_id if link else ref.split("-")[0].strip().upper()

        b = s.get(Borrower, loan_id)
        if b is None:
            raise HTTPException(404, f"no borrower for reference {ref}")
        # The agreed amount, which may be a part payment, not always the full EMI.
        amount_paise = link.amount_paise if link else b.emi_amount_paise
        product = (b.product or "PERSONAL_LOAN").replace("_", " ").title()

    expires = (datetime.now(timezone.utc) + timedelta(hours=LINK_TTL_HOURS)).strftime("%d %b, %H:%M UTC")
    lender = settings.lender_name
    return HTMLResponse(PAGE.format(
        amount=_fmt_inr(amount_paise),
        lender=lender,
        initial=(lender.strip()[:1] or "G").upper(),
        loan_id=loan_id,
        product=product,
        ref=ref,
        expires=expires,
    ))
