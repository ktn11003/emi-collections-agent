"""Compliance guardrail tests.

These encode the actual regulatory requirements. If one of these fails, the bot
is capable of saying something that breaches RBI recovery-agent norms — which is
a deal-losing, not a cosmetic, defect.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent.guardrails import (
    compliance_score,
    compliance_summary,
    in_calling_window,
    precall_check,
    screen_utterance,
)
from app.agent.prompt import build_system_prompt, opening_line
from app.db.models import Borrower


def make_borrower(**over) -> Borrower:
    fields = dict(
        loan_id="PL0001", name="Rahul", phone="+919800000001", language="hi-IN",
        emi_amount_paise=450000, due_date=date(2026, 7, 5), dpd=20,
        product="PERSONAL_LOAN", consent=True, dnd_registered=False, attempts_today=0,
    )
    fields.update(over)
    return Borrower(**fields)


class TestUtteranceScreening:
    @pytest.mark.parametrize("line,tag", [
        ("Agar aaj payment nahi kiya to police case ho jayega", "threat_of_police_action"),
        ("Hum aapke ghar aa rahe hain recovery ke liye", "threat_of_visit"),
        ("Legal action lene padega aapke against", "threat_of_legal_action"),
        ("Aapka CIBIL kharab kar denge", "coercive_credit_threat"),
        ("Aap bilkul bewakoof hain", "abusive_language"),
        ("Yeh last warning hai, consequences bhugatne padenge", "intimidation"),
    ])
    def test_blocks_harassment(self, line, tag):
        """RBI norms forbid threats, coercion and abuse outright."""
        result = screen_utterance(line)
        assert not result.allowed
        assert tag in result.tags
        # Must substitute something sayable — silence mid-call reads as a drop.
        assert result.safe_text

    @pytest.mark.parametrize("line", [
        "Aapka CVV number bata dijiye",
        "OTP share kar dijiye please",
        "Apna UPI PIN batayein",
        "Card number aur expiry date chahiye",
    ])
    def test_blocks_credential_requests(self, line):
        """No credential ever travels over the voice channel (PCI-DSS scope)."""
        result = screen_utterance(line)
        assert not result.allowed
        assert any(t in result.tags for t in ("credential_request", "card_data_request"))

    @pytest.mark.parametrize("line", [
        "Main aapko settlement de sakti hoon",
        "Interest kam kar denge aapke liye",
        "50% waiver mil jayega",
    ])
    def test_blocks_unauthorised_commercial_offers(self, line):
        """The bot has no authority to discount; the model must not invent one."""
        assert not screen_utterance(line).allowed

    def test_third_party_disclosure(self):
        assert not screen_utterance("Aapke boss ko bata denge iske baare mein").allowed

    @pytest.mark.parametrize("line", [
        "Namaste Rahul ji, main Priya bol rahi hoon Generic Finance se.",
        "Aapki 4,500 rupees ki EMI 5 tareekh ko due thi.",
        "Bilkul samajh sakti hoon, koi baat nahi. Kis tareekh tak kar payenge?",
        "Main aapko payment link WhatsApp par bhej deti hoon.",
        "Yeh call quality ke liye record ki ja rahi hai.",
    ])
    def test_allows_normal_collections_speech(self, line):
        """The screen must not be so broad it blocks the actual script."""
        result = screen_utterance(line)
        assert result.allowed, f"false positive on: {line}"
        assert result.safe_text == line

    def test_empty_is_allowed(self):
        assert screen_utterance("").allowed


class TestCallingWindow:
    def test_inside_window(self):
        ist = ZoneInfo("Asia/Kolkata")
        assert in_calling_window(datetime(2026, 7, 30, 10, 30, tzinfo=ist))
        assert in_calling_window(datetime(2026, 7, 30, 8, 0, tzinfo=ist))
        assert in_calling_window(datetime(2026, 7, 30, 19, 0, tzinfo=ist))

    def test_outside_window(self):
        ist = ZoneInfo("Asia/Kolkata")
        assert not in_calling_window(datetime(2026, 7, 30, 7, 59, tzinfo=ist))
        assert not in_calling_window(datetime(2026, 7, 30, 19, 1, tzinfo=ist))
        assert not in_calling_window(datetime(2026, 7, 30, 2, 0, tzinfo=ist))


class TestPrecallGate:
    def test_allows_a_clean_borrower(self):
        ist = ZoneInfo("Asia/Kolkata")
        gate = precall_check(make_borrower(), now=datetime(2026, 7, 30, 11, 0, tzinfo=ist))
        assert gate.allowed
        assert all(gate.checks.values())

    def test_blocks_without_consent(self):
        """DPDP Act 2023: no consent, no outbound contact. Fails closed."""
        gate = precall_check(make_borrower(consent=False))
        assert not gate.allowed
        assert any("consent" in r for r, _ in gate.reasons)
        assert any(reg == "DPDP Act 2023" for _, reg in gate.reasons)

    def test_blocks_dnd_registered(self):
        gate = precall_check(make_borrower(dnd_registered=True))
        assert not gate.allowed
        assert any("DND" in r for r, _ in gate.reasons)

    def test_blocks_over_attempt_cap(self):
        gate = precall_check(make_borrower(attempts_today=3))
        assert not gate.allowed
        assert any("attempt cap" in r for r, _ in gate.reasons)

    def test_blocks_outside_window(self):
        ist = ZoneInfo("Asia/Kolkata")
        gate = precall_check(make_borrower(), now=datetime(2026, 7, 30, 22, 0, tzinfo=ist))
        assert not gate.allowed
        assert any("calling window" in r for r, _ in gate.reasons)


class TestComplianceSummary:
    def test_clean_call_scores_100(self):
        lines = [
            "Namaste Rahul ji, main Priya bol rahi hoon Generic Finance se. "
            "Yeh call quality ke liye record ki ja rahi hai.",
            "Aapki EMI pending hai. Kya aap aaj payment kar sakte hain?",
        ]
        summary = compliance_summary(lines, borrower=make_borrower(), in_window=True)
        assert summary["disclosed_recording"]
        assert summary["identified_self"]
        assert summary["no_threat"]
        assert compliance_score(summary) == 100.0

    def test_missing_disclosure_is_detected(self):
        summary = compliance_summary(
            ["Aapki EMI pending hai."], borrower=make_borrower(), in_window=True
        )
        assert not summary["disclosed_recording"]
        assert compliance_score(summary) < 100.0

    def test_violation_tags_lower_the_score(self):
        summary = compliance_summary(
            ["Generic Finance se, record ki ja rahi hai."],
            borrower=make_borrower(), in_window=True,
            violation_tags=["threat_of_police_action"],
        )
        assert not summary["no_threat"]
        assert compliance_score(summary) < 100.0


class TestGroundedPrompt:
    def test_prompt_contains_the_real_numbers(self):
        """Grounding is what stops the bot inventing an amount."""
        prompt = build_system_prompt(make_borrower(), language="hi-IN")
        assert "Rahul" in prompt
        assert "4,500" in prompt          # Indian digit grouping, for TTS
        assert "2026-07-05" in prompt
        assert "PL0001" in prompt

    def test_prompt_states_the_hard_rules(self):
        prompt = build_system_prompt(make_borrower())
        for required in ("never threaten", "escalate_to_human", "RBI", "CVV"):
            assert required.lower() in prompt.lower()

    def test_opening_line_always_discloses_recording(self):
        """The disclosure is deterministic, not model-generated, so it is auditable."""
        for language in ("hi-IN", "en-IN"):
            line = opening_line(make_borrower(), language=language)
            assert screen_utterance(line).allowed
            summary = compliance_summary([line], borrower=make_borrower(), in_window=True)
            assert summary["disclosed_recording"], language
            assert summary["identified_self"], language

    def test_indian_digit_grouping(self):
        prompt = build_system_prompt(make_borrower(emi_amount_paise=1250000), language="hi-IN")
        assert "12,500" in prompt
        prompt = build_system_prompt(make_borrower(emi_amount_paise=125000000), language="hi-IN")
        assert "12,50,000" in prompt      # lakh grouping, not 1,250,000
