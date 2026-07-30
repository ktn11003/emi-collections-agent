"""Relational schema for the collections voice agent.

Design notes
------------
* One row per *call* (the CDR), one row per *turn*, one row per *tool
  invocation* — so a call can be reconstructed and audited end to end.
* ``tool_invocations.idempotency_key`` is UNIQUE: that constraint is what makes
  "send the payment link exactly once" true even if the LLM repeats a tool call
  or the client retries.
* Money is stored in paise (integer) — never float.
* Types are chosen to work unchanged on SQLite and PostgreSQL. ``JSONB`` is used
  automatically on Postgres via ``JSON().with_variant``.
* Nothing here stores raw card/UPI credentials; payment links are references
  only (PCI-DSS scope reduction).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres, plain JSON on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- enums -------------------------------------------------------------------
class Disposition(str, enum.Enum):
    """Call outcome. Mirrors the collections dispositions an LMS expects."""

    PTP = "PTP"                    # promise to pay captured (a date was given)
    PAID = "PAID"                  # already paid / paying now
    LINK_SENT = "LINK_SENT"        # payment link delivered, no date committed
    DISPUTE = "DISPUTE"            # borrower disputes the amount
    WRONG_NUMBER = "WRONG_NUMBER"
    NO_ANSWER = "NO_ANSWER"
    CALLBACK = "CALLBACK"          # asked to be called later
    ESCALATED = "ESCALATED"        # handed to a human agent
    REFUSED = "REFUSED"
    INCOMPLETE = "INCOMPLETE"      # call dropped before an outcome


class Channel(str, enum.Enum):
    BROWSER = "BROWSER"            # web-mic demo
    PSTN = "PSTN"                  # real phone call via SIP/CPaaS


class Speaker(str, enum.Enum):
    BOT = "BOT"
    BORROWER = "BORROWER"
    SYSTEM = "SYSTEM"


class ToolStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


# --- data plane: who to call ------------------------------------------------
class Borrower(Base):
    """One row per loan account, loaded from the SFTP call-list CSV."""

    __tablename__ = "borrowers"

    loan_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="hi-IN")
    emi_amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    dpd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    product: Mapped[str] = mapped_column(String(40), default="PERSONAL_LOAN")
    outstanding_paise: Mapped[int | None] = mapped_column(Integer)

    # DPDP Act 2023: consent is a precondition, not an afterthought.
    consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # TRAI DND / UCC scrub result.
    dnd_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    calls: Mapped[list["Call"]] = relationship(back_populates="borrower")

    @property
    def emi_rupees(self) -> float:
        return self.emi_amount_paise / 100.0


class Campaign(Base):
    """A dialing campaign — the SoW's two bots are two campaigns."""

    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    use_case: Mapped[str] = mapped_column(String(40), default="EMI_REMINDER")
    languages: Mapped[list] = mapped_column(JSONType, default=list)
    dialer_mode: Mapped[str] = mapped_column(String(20), default="PROGRESSIVE")
    max_attempts_per_day: Mapped[int] = mapped_column(Integer, default=3)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    calls: Mapped[list["Call"]] = relationship(back_populates="campaign")


# --- the CDR ----------------------------------------------------------------
class Call(Base):
    """Call Detail Record. One row per call attempt; the analytics unit of work."""

    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # For a PSTN call this is the SIP Call-ID; for the browser demo a synthetic
    # id. Either way it is *the* correlation key across logs, CDR and analytics.
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    loan_id: Mapped[str] = mapped_column(ForeignKey("borrowers.loan_id"), nullable=False, index=True)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)

    channel: Mapped[Channel] = mapped_column(Enum(Channel), default=Channel.BROWSER)
    direction: Mapped[str] = mapped_column(String(10), default="OUTBOUND")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float | None] = mapped_column(Float)

    disposition: Mapped[Disposition] = mapped_column(
        Enum(Disposition), default=Disposition.INCOMPLETE, index=True
    )
    # Languages actually detected per turn — proves code-switching handling.
    languages_detected: Mapped[list] = mapped_column(JSONType, default=list)
    script_language: Mapped[str] = mapped_column(String(10), default="hi-IN")

    sentiment: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)

    recording_uri: Mapped[str | None] = mapped_column(String(400))
    transcript_uri: Mapped[str | None] = mapped_column(String(400))

    # {disclosed_recording, in_window, no_threat, identified_self, consent_on_file}
    compliance: Mapped[dict] = mapped_column(JSONType, default=dict)
    # p50/p95 of the end-of-speech -> first-audio budget for this call.
    latency_stats: Mapped[dict] = mapped_column(JSONType, default=dict)
    cost_paise: Mapped[int | None] = mapped_column(Integer)

    borrower: Mapped[Borrower] = relationship(back_populates="calls")
    campaign: Mapped[Campaign | None] = relationship(back_populates="calls")
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="Turn.idx"
    )
    tool_invocations: Mapped[list["ToolInvocation"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(back_populates="call", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_calls_loan_started", "loan_id", "started_at"),)


class Turn(Base):
    """One conversational turn, with the per-stage latency that produced it."""

    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False, index=True)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)

    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10))
    language_confidence: Mapped[float | None] = mapped_column(Float)
    barged_in: Mapped[bool] = mapped_column(Boolean, default=False)

    # {stt_ms, llm_ttft_ms, tts_ttfa_ms, end_of_speech_to_first_audio_ms}
    latency_ms: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")

    __table_args__ = (UniqueConstraint("call_id", "idx", name="uq_turn_call_idx"),)


# --- agentic side effects ---------------------------------------------------
class ToolInvocation(Base):
    """An LLM-triggered side effect, executed exactly once.

    The UNIQUE on ``idempotency_key`` is the enforcement point: a duplicate
    insert fails, the executor returns the cached result, and the borrower does
    not get two payment links.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    arguments: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONType)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[ToolStatus] = mapped_column(Enum(ToolStatus), default=ToolStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    call: Mapped[Call | None] = relationship(back_populates="tool_invocations")


class PaymentLink(Base):
    __tablename__ = "payment_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    loan_id: Mapped[str] = mapped_column(ForeignKey("borrowers.loan_id"), index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(String(400), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="CREATED")  # CREATED|SENT|PAID|EXPIRED
    channel_sent: Mapped[str | None] = mapped_column(String(20))       # WHATSAPP|SMS
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromiseToPay(Base):
    """The commercial output of a collections call."""

    __tablename__ = "promises_to_pay"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    loan_id: Mapped[str] = mapped_column(ForeignKey("borrowers.loan_id"), index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_language: Mapped[str | None] = mapped_column(String(10))
    honoured: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CrmWriteback(Base):
    """Outbox for LMS/CRM updates — queue, retry, dead-letter, reconcile later."""

    __tablename__ = "crm_writebacks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    loan_id: Mapped[str] = mapped_column(ForeignKey("borrowers.loan_id"), index=True)
    system: Mapped[str] = mapped_column(String(30), default="LMS")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    loan_id: Mapped[str] = mapped_column(ForeignKey("borrowers.loan_id"), index=True)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    context_summary: Mapped[str | None] = mapped_column(Text)
    queue: Mapped[str] = mapped_column(String(40), default="COLLECTIONS_TIER2")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- analytics & audit ------------------------------------------------------
class AnalyticsReport(Base):
    """Output of the post-call batch pipeline (batch STT -> diarize -> LLM)."""

    __tablename__ = "analytics_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False, unique=True)

    summary: Mapped[str | None] = mapped_column(Text)
    summary_english: Mapped[str | None] = mapped_column(Text)  # via Mayura, for HQ reporting
    sentiment: Mapped[str | None] = mapped_column(String(20))
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    disposition_predicted: Mapped[str | None] = mapped_column(String(30))
    intents: Mapped[list] = mapped_column(JSONType, default=list)
    objections: Mapped[list] = mapped_column(JSONType, default=list)

    qa_score: Mapped[float | None] = mapped_column(Float)          # 0-100, 100% call QA
    compliance_flags: Mapped[dict] = mapped_column(JSONType, default=dict)
    diarized_turns: Mapped[list] = mapped_column(JSONType, default=list)
    model_versions: Mapped[dict] = mapped_column(JSONType, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComplianceEvent(Base):
    """Every guardrail decision, so an auditor can be shown the trail."""

    __tablename__ = "compliance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str | None] = mapped_column(Text)
    regulation: Mapped[str | None] = mapped_column(String(60))  # e.g. "RBI recovery-agent norms"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Append-only event stream. Stands in for Kafka/SQS in the PoC."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    call: Mapped[Call | None] = relationship(back_populates="events")


class AuditLog(Base):
    """Immutable audit trail — includes the vendor-copy purge proof auditors ask for."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(60), default="system")
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TranslationCache(Base):
    """Caches Mayura output so a compliance-approved line is translated once.

    Keeps the mandatory disclosure identical across every call in a language
    (auditable) and removes a network hop from the hot path.
    """

    __tablename__ = "translation_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_language: Mapped[str] = mapped_column(String(10), nullable=False)
    target_language: Mapped[str] = mapped_column(String(10), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(40), default="mayura:v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_text", "source_language", "target_language", name="uq_translation"),
    )
