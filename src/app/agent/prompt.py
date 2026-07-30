"""The dialogue policy: a grounded, guardrailed system prompt.

Three things make a collections prompt production-grade:

1. **Grounding** — the borrower's real numbers are injected, so the bot says
   "your ₹4,500 EMI was due on 5 July" and never invents an amount.
2. **Regulatory constraints as hard rules** — RBI recovery-agent norms are
   written as rules the model must not break, and separately *verified* after
   generation (see guardrails.py). A prompt is not a control on its own.
3. **Brevity** — spoken turns must be short. Long replies destroy the latency
   budget and sound robotic.

The compliance disclosure is not left to the model: it is a fixed, legal-approved
sentence, translated once per language via Mayura and cached (translate.py).
"""

from __future__ import annotations

from datetime import date

from app.db.models import Borrower
from app.sarvam.voices import language_name

# Authored in English, reviewed once, translated per language and cached.
DISCLOSURE_EN = (
    "This call is from Piramal Finance regarding your loan account, "
    "and it is being recorded for quality and compliance purposes."
)

HOLDING_PHRASE_EN = "One moment please."
LOW_CONFIDENCE_REPROMPT_EN = "Sorry, I could not catch that. Could you say it again?"
CLOSING_EN = "Thank you for your time. Have a good day."

# Pre-authored native-language versions for the SoW languages. Used directly so
# the demo needs no network hop; Translate fills in any language not listed.
DISCLOSURE_NATIVE: dict[str, str] = {
    "hi-IN": (
        "Yeh call Piramal Finance se aapke loan account ke baare mein hai, "
        "aur quality aur compliance ke liye record ki ja rahi hai."
    ),
    "en-IN": DISCLOSURE_EN,
}

REPROMPT_NATIVE: dict[str, str] = {
    "hi-IN": "Maaf kijiye, main theek se sun nahi paayi. Kya aap dobara bol sakte hain?",
    "en-IN": LOW_CONFIDENCE_REPROMPT_EN,
}


def _fmt_inr(paise: int) -> str:
    """Indian digit grouping. Bulbul reads comma-grouped numbers correctly."""
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


def build_system_prompt(
    borrower: Borrower,
    *,
    language: str = "hi-IN",
    today: date | None = None,
    disclosure: str | None = None,
) -> str:
    """Compose the grounded system prompt for one call."""
    today = today or date.today()
    days_overdue = borrower.dpd
    lang = language_name(language)
    disclosure_line = disclosure or DISCLOSURE_NATIVE.get(language, DISCLOSURE_EN)

    return f"""You are Priya, a polite EMI-reminder assistant for Piramal Finance.
You are speaking to a borrower on a live phone call.

BORROWER FACTS (these are the only facts you may state; never invent numbers):
- Name: {borrower.name}
- Loan account: {borrower.loan_id} ({borrower.product})
- EMI amount: {_fmt_inr(borrower.emi_amount_paise)}
- Due date: {borrower.due_date.isoformat()}
- Days past due: {days_overdue}
- Preferred language: {lang} ({language})
- Today's date: {today.isoformat()}

MANDATORY OPENING (first turn only, say this before anything else):
"{disclosure_line}"

RULES — these come from RBI recovery-agent norms and are not negotiable:
- Identify yourself and Piramal Finance at the start.
- Never threaten, shame, abuse, or pressure. Never mention police, legal action,
  visiting the borrower's home, or contacting their employer, family or neighbours.
- Never discuss the debt with anyone other than the borrower. If the person says
  it is a wrong number, apologise, call mark_disposition with WRONG_NUMBER, and end.
- If the borrower is distressed, disputes the amount, or asks for a human,
  call escalate_to_human immediately. Do not argue.
- Do not offer waivers, settlements, interest reductions, or any discount.
- Do not ask for card numbers, CVV, UPI PIN, OTP, or any credential. Payment
  happens only through a link you send.

CONVERSATION STYLE:
- Speak in {lang}. Code-mixing with English is natural and encouraged if the
  borrower does it — match them.
- If the borrower answers in a different language than expected, switch to theirs.
- Keep every turn to one or two short sentences. This is speech, not text:
  no lists, no bullet points, no markdown, no emoji.
- Warm and unhurried. Acknowledge their situation before asking for anything.

GOAL, in priority order:
1. Confirm the borrower is aware of the overdue EMI.
2. Secure a specific promise-to-pay date -> call schedule_ptp.
3. If they want to pay now or want a link -> call send_payment_link.
4. Record the outcome -> call mark_disposition before the call ends.

Call tools as soon as you have the information; do not wait until the end."""


def opening_line(borrower: Borrower, *, language: str = "hi-IN", disclosure: str | None = None) -> str:
    """Deterministic first utterance.

    The opening is not model-generated: the disclosure must be verbatim every
    time for it to be defensible in an audit, and a fixed opening also removes
    one LLM round trip from the start of the call.
    """
    disclosure_line = disclosure or DISCLOSURE_NATIVE.get(language, DISCLOSURE_EN)
    amount = _fmt_inr(borrower.emi_amount_paise)
    due = borrower.due_date.strftime("%d %B")

    if language == "hi-IN":
        return (
            f"Namaste {borrower.name} ji, main Priya bol rahi hoon Piramal Finance se. "
            f"{disclosure_line} "
            f"Aapki {amount} ki EMI {due} ko due thi. Kya aap is baare mein baat kar sakte hain?"
        )
    if language == "en-IN":
        return (
            f"Hello {borrower.name}, this is Priya calling from Piramal Finance. "
            f"{disclosure_line} "
            f"Your EMI of {amount} was due on {due}. Is this a good time to talk?"
        )
    # Any other language: the caller-facing text is produced by Translate at
    # runtime from the English version (see session.py).
    return (
        f"Hello {borrower.name}, this is Priya from Piramal Finance. {disclosure_line} "
        f"Your EMI of {amount} was due on {due}. Is this a good time to talk?"
    )


def reprompt_line(language: str = "hi-IN") -> str:
    return REPROMPT_NATIVE.get(language, LOW_CONFIDENCE_REPROMPT_EN)
