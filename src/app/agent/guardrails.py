"""Compliance guardrails, enforced in code — not merely requested in the prompt.

A system prompt is an instruction, not a control. For a regulated collections
call each rule is also *verified*:

* **Pre-call** (:func:`precall_check`) — consent on file, not DND-registered,
  inside the RBI calling window, under the daily attempt cap. Fails closed.
* **Per-utterance** (:func:`screen_utterance`) — every drafted line is screened
  before it reaches TTS. Threats, settlement offers and credential requests are
  blocked at the last possible moment.
* **Per-call** (:func:`compliance_summary`) — did we actually disclose the
  recording and identify ourselves? Written to the CDR and the audit trail.

Screening is a fast deterministic pass (no extra model round trip) so it costs
the latency budget nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from app.config import settings
from app.db.models import Borrower

RBI = "RBI recovery-agent norms"
DPDP = "DPDP Act 2023"
TRAI = "TRAI DND / UCC"
PCI = "PCI-DSS"

# --- prohibited content ------------------------------------------------------
# Harassment / coercion. RBI norms forbid threats and public shaming.
_THREAT_PATTERNS = [
    (r"\b(police|thane|fir|arrest|jail|giraftar)\b", "threat_of_police_action"),
    (r"\b(legal action|court|case|notice bhej|vakil|lawyer|summons)\b", "threat_of_legal_action"),
    (r"\b(ghar (aa|par aa)|home visit|office (aa|par aa)|recovery agent bhej)\b", "threat_of_visit"),
    (r"\b(boss|employer|manager|neighbour|padosi|family|rishtedaar)\b.{0,24}\b(bata|inform|call|batayenge)\b",
     "third_party_disclosure"),
    (r"\b(cibil (kharab|barbaad)|credit score (kharab|barbaad)|blacklist)\b", "coercive_credit_threat"),
    (r"\b(bewakoof|chor|jhoot|nikamma|besharam|stupid|liar|cheat)\b", "abusive_language"),
    (r"\b(dhamki|warning de|last warning|consequences bhugat)\b", "intimidation"),
]

# Commercial authority the bot does not have.
_UNAUTHORISED_OFFERS = [
    (r"\b(waive|waiver|maaf kar|settlement|settle kar|one[- ]time settlement|ots)\b", "unauthorised_waiver"),
    (r"\b(interest (kam|reduce)|discount|chhoot de)\b", "unauthorised_discount"),
]

# Never collect credentials on a voice channel (PCI-DSS scope reduction).
_CREDENTIAL_REQUESTS = [
    (r"\b(cvv|card number|card ka number|expiry date)\b", "card_data_request"),
    (r"\b(otp|one[- ]time password|pin (bata|batao|share)|upi pin|mpin|password)\b", "credential_request"),
    (r"\b(net ?banking (login|password)|account ka password)\b", "credential_request"),
]

_ALL_RULES = (
    [(re.compile(p, re.IGNORECASE), tag, RBI) for p, tag in _THREAT_PATTERNS]
    + [(re.compile(p, re.IGNORECASE), tag, RBI) for p, tag in _UNAUTHORISED_OFFERS]
    + [(re.compile(p, re.IGNORECASE), tag, PCI) for p, tag in _CREDENTIAL_REQUESTS]
)

_DISCLOSURE_MARKERS = re.compile(
    r"(record(ed|ing)?|record ki ja rahi|recorded for quality|" r"रिकॉर्ड)", re.IGNORECASE
)
_IDENTITY_MARKERS = re.compile(
    r"(piramal|priya)", re.IGNORECASE
)


# --- results -----------------------------------------------------------------
@dataclass(slots=True)
class ScreenResult:
    allowed: bool
    violations: list[tuple[str, str]] = field(default_factory=list)  # (tag, regulation)
    safe_text: str | None = None

    @property
    def tags(self) -> list[str]:
        return [t for t, _ in self.violations]


@dataclass(slots=True)
class PrecallResult:
    allowed: bool
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (reason, regulation)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def reason_text(self) -> str:
        return "; ".join(r for r, _ in self.reasons)


# --- pre-call ----------------------------------------------------------------
def in_calling_window(now: datetime | None = None) -> bool:
    """RBI: recovery calls generally 08:00-19:00 in the borrower's local time."""
    tz = ZoneInfo(settings.calling_window_tz)
    local = (now or datetime.now(tz)).astimezone(tz)
    start: dtime = settings.calling_window_start
    end: dtime = settings.calling_window_end
    return start <= local.time() <= end


def precall_check(borrower: Borrower, *, now: datetime | None = None) -> PrecallResult:
    """Gate a call before it is placed. Fails closed."""
    checks = {
        "consent_on_file": bool(borrower.consent),
        "not_dnd_registered": not borrower.dnd_registered,
        "in_calling_window": in_calling_window(now),
        "under_attempt_cap": borrower.attempts_today < 3,
    }
    reasons: list[tuple[str, str]] = []
    if not checks["consent_on_file"]:
        reasons.append(("no consent on file for outbound contact", DPDP))
    if not checks["not_dnd_registered"]:
        reasons.append(("number is on the DND registry", TRAI))
    if not checks["in_calling_window"]:
        window = f"{settings.calling_window_start:%H:%M}-{settings.calling_window_end:%H:%M}"
        reasons.append((f"outside the permitted calling window ({window} {settings.calling_window_tz})", RBI))
    if not checks["under_attempt_cap"]:
        reasons.append(("daily attempt cap reached", RBI))

    # A demo may need to run at midnight; every other check still fails closed.
    if not settings.enforce_calling_window:
        reasons = [(r, reg) for r, reg in reasons if "calling window" not in r]

    return PrecallResult(allowed=not reasons, reasons=reasons, checks=checks)


# --- per-utterance -----------------------------------------------------------
def screen_utterance(text: str) -> ScreenResult:
    """Screen one drafted line before it is spoken."""
    if not text.strip():
        return ScreenResult(allowed=True, safe_text=text)

    violations = [(tag, reg) for rx, tag, reg in _ALL_RULES if rx.search(text)]
    if not violations:
        return ScreenResult(allowed=True, safe_text=text)

    # Fail safe, and keep talking: silence mid-call reads as a dropped line.
    return ScreenResult(
        allowed=False,
        violations=violations,
        safe_text=(
            "Main is baare mein aapko sahi jaankari dene ke liye apne senior se "
            "connect kar deti hoon. Ek minute."
        ),
    )


# --- per-call ----------------------------------------------------------------
def compliance_summary(
    bot_lines: list[str], *, borrower: Borrower, in_window: bool, violation_tags: list[str] | None = None
) -> dict:
    """The ``calls.compliance`` blob — what an auditor gets shown."""
    joined = " ".join(bot_lines)
    tags = violation_tags or []
    return {
        "disclosed_recording": bool(_DISCLOSURE_MARKERS.search(joined)),
        "identified_self": bool(_IDENTITY_MARKERS.search(joined)),
        "in_window": in_window,
        "no_threat": not any(t in tags for t in
                             ("threat_of_police_action", "threat_of_legal_action",
                              "threat_of_visit", "intimidation", "abusive_language",
                              "coercive_credit_threat")),
        "no_third_party_disclosure": "third_party_disclosure" not in tags,
        "no_credential_request": not any(t in tags for t in ("card_data_request", "credential_request")),
        "no_unauthorised_offer": not any(t in tags for t in
                                         ("unauthorised_waiver", "unauthorised_discount")),
        "consent_on_file": bool(borrower.consent),
        "violation_tags": sorted(set(tags)),
    }


def compliance_score(summary: dict) -> float:
    """0-100. Every call gets scored — the "100% QA" claim, made concrete."""
    keys = [
        "disclosed_recording", "identified_self", "in_window", "no_threat",
        "no_third_party_disclosure", "no_credential_request",
        "no_unauthorised_offer", "consent_on_file",
    ]
    passed = sum(1 for k in keys if summary.get(k))
    return round(100.0 * passed / len(keys), 1)
