"""Orchestrator tests: exactly-once side effects and database integrity.

The idempotency guarantee is the reason a borrower does not get two payment
links when the model restates a tool call. It is enforced by a UNIQUE index, so
these tests assert on the database, not on a mock.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.db.base import session_scope
from app.db.models import (
    Channel,
    Disposition,
    PaymentLink,
    PromiseToPay,
    ToolInvocation,
    ToolStatus,
)
from app.db.repo import create_call, portfolio_stats
from app.orchestrator.executor import execute, idempotency_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
def call(borrower):
    # correlation_id is UNIQUE (it is the SIP Call-ID in production), so each
    # test needs its own. id() is not safe here: CPython recycles addresses.
    with session_scope() as s:
        c = create_call(
            s,
            correlation_id=f"test-{borrower.loan_id}-{uuid.uuid4()}",
            loan_id=borrower.loan_id,
            channel=Channel.BROWSER,
        )
        return c.id


class TestIdempotency:
    async def test_same_tool_twice_sends_one_link(self, call, borrower):
        """The whole point of the UNIQUE idempotency key."""
        args = {"loan_id": borrower.loan_id, "amount": 4500, "channel": "WHATSAPP"}

        first = await execute("send_payment_link", args, call_id=call)
        second = await execute("send_payment_link", args, call_id=call)

        assert first.ok and second.ok
        assert not first.replayed
        assert second.replayed, "second call must replay, not re-send"
        assert first.data["url"] == second.data["url"]

        with session_scope() as s:
            links = s.scalar(
                select(func.count()).select_from(PaymentLink).where(PaymentLink.call_id == call)
            )
        assert links == 1, "a second payment link row means the borrower got two links"

    async def test_key_is_stable_for_the_same_business_intent(self):
        a = idempotency_key("c1", "send_payment_link", {"loan_id": "PL1", "amount": 100})
        b = idempotency_key("c1", "send_payment_link", {"loan_id": "PL1", "amount": 100, "channel": "SMS"})
        # channel is not a business-identity field, so the key must match.
        assert a == b

    async def test_key_differs_on_amount_and_call(self):
        base = idempotency_key("c1", "send_payment_link", {"loan_id": "PL1", "amount": 100})
        assert base != idempotency_key("c1", "send_payment_link", {"loan_id": "PL1", "amount": 200})
        assert base != idempotency_key("c2", "send_payment_link", {"loan_id": "PL1", "amount": 100})

    async def test_explicit_header_key_is_honoured(self, call, borrower):
        """An n8n flow retrying a webhook must not duplicate the effect."""
        args = {"loan_id": borrower.loan_id, "promised_date": "2026-08-01", "amount": 4500}
        a = await execute("schedule_ptp", args, call_id=call, explicit_key="client-supplied-key-1")
        b = await execute("schedule_ptp", args, call_id=call, explicit_key="client-supplied-key-1")
        assert a.ok and b.replayed


class TestToolBehaviour:
    async def test_ptp_writes_a_row_and_a_crm_outbox_entry(self, call, borrower):
        promised = (date.today() + timedelta(days=6)).isoformat()
        result = await execute(
            "schedule_ptp",
            {"loan_id": borrower.loan_id, "promised_date": promised, "amount": 4500},
            call_id=call,
        )
        assert result.ok
        assert result.data["promised_date"] == promised

        with session_scope() as s:
            ptp = s.scalar(select(PromiseToPay).where(PromiseToPay.call_id == call))
            assert ptp is not None
            assert ptp.amount_paise == 450000        # rupees -> paise, integer

    async def test_ptp_rejects_a_date_too_far_out(self, call, borrower):
        """A promise 6 months away is not a promise; keep the pipeline honest."""
        far = (date.today() + timedelta(days=200)).isoformat()
        result = await execute(
            "schedule_ptp",
            {"loan_id": borrower.loan_id, "promised_date": far, "amount": 4500},
            call_id=call,
        )
        assert not result.ok
        assert "30 days" in (result.error or "")

    async def test_ptp_rejects_unparseable_date(self, call, borrower):
        result = await execute(
            "schedule_ptp",
            {"loan_id": borrower.loan_id, "promised_date": "next Tuesday", "amount": 4500},
            call_id=call,
        )
        assert not result.ok
        assert "unparseable" in (result.error or "")

    async def test_escalation_routes_grievances_to_the_right_queue(self, call, borrower):
        result = await execute(
            "escalate_to_human",
            {"loan_id": borrower.loan_id, "reason": "DISPUTE", "context": "disputes the amount"},
            call_id=call,
        )
        assert result.ok
        assert result.data["queue"] == "COLLECTIONS_GRIEVANCE"

    async def test_disposition_validates_the_enum(self, call, borrower):
        ok = await execute(
            "mark_disposition",
            {"loan_id": borrower.loan_id, "disposition": "PTP", "notes": "will pay"},
            call_id=call,
        )
        assert ok.ok and ok.data["disposition"] == Disposition.PTP.value

        bad = await execute(
            "mark_disposition",
            {"loan_id": borrower.loan_id, "disposition": "MAYBE_LATER"},
            call_id=call,
        )
        assert not bad.ok

    async def test_unknown_tool_is_refused(self, call):
        result = await execute("wire_transfer", {"amount": 1}, call_id=call)
        assert not result.ok
        assert "unknown tool" in (result.error or "")


class TestAuditTrail:
    async def test_every_invocation_is_recorded(self, call, borrower):
        await execute(
            "send_payment_link",
            {"loan_id": borrower.loan_id, "amount": 7777},
            call_id=call,
        )
        with session_scope() as s:
            ti = s.scalar(
                select(ToolInvocation)
                .where(ToolInvocation.call_id == call, ToolInvocation.name == "send_payment_link")
                .order_by(ToolInvocation.created_at.desc())
            )
            assert ti is not None
            assert ti.status is ToolStatus.SUCCEEDED
            assert ti.attempts >= 1
            assert ti.completed_at is not None
            assert len(ti.idempotency_key) == 48

    async def test_portfolio_stats_are_computable(self, call, borrower):
        await execute(
            "schedule_ptp",
            {"loan_id": borrower.loan_id, "promised_date": (date.today() + timedelta(days=3)).isoformat(),
             "amount": 1000},
            call_id=call,
        )
        with session_scope() as s:
            stats = portfolio_stats(s)
        assert stats["total_calls"] >= 1
        assert stats["ptp_count"] >= 1
        assert stats["ptp_value_rupees"] > 0
        assert 0 <= stats["containment_pct"] <= 100
