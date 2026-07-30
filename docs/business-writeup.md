# Multilingual voice collections for Piramal Finance, on Sarvam AI

**A working PoC: EMI-reminder voice agent across six Indian languages, with an
agentic backend and 100% call QA.**

---

## ① The problem

Indian lenders make **tens of millions of collections calls every month**. For an
NBFC running early-delinquency EMI reminders, the economics and the operational
reality both work against the current model:

* **Cost.** A human agent touch costs roughly **₹40–80** all-in. Early-bucket
  reminders are the highest-volume, most repetitive, least judgement-intensive
  calls in the book — and they consume the same expensive capacity as genuinely
  difficult negotiations.
* **Language fragmentation.** Piramal's borrower base spans Hindi, English,
  Tamil, Telugu, Malayalam and Kannada. Staffing native speakers for every
  language in every shift is not practical, so borrowers get called in a language
  they are not comfortable in — or not called on time.
* **Timeliness is the whole game.** Recovery on a missed EMI decays sharply with
  days-past-due. A reminder on day 3 is worth far more than the same reminder on
  day 25, but human capacity is finite and gets allocated to the loudest cases.
* **Compliance risk is asymmetric.** RBI recovery-agent norms constrain calling
  hours, tone and conduct. Human QA samples perhaps **2%** of calls, so a
  systematic behaviour problem is discovered late, in an audit, at maximum cost.

**The specific brief.** A real Scope of Work exists: deploy two generative-AI
voice bots for Piramal Finance — **EMI Reminder** and **Lead Qualification** —
across six Indian languages, up to **20,000 outbound calls** (10k per use case),
in four weeks.

---

## ② Why AI voice, and why now

Voice specifically, not another channel:

* **Reach beats convenience.** In the tail of the portfolio, digital literacy is
  low. SMS and app nudges are ignored; a phone call in the borrower's own language
  is answered. Voice is the channel that actually converts for this segment.
* **Cost becomes elastic instead of linear.** Capacity stops being a hiring
  decision. 500 calls or 50,000 costs the same per call.
* **It never gets frustrated.** Turn 400 of the day is delivered with the same
  patience as turn 1 — which, in collections, is a compliance asset.
* **Structured output, not just a conversation.** Every call yields a machine-
  readable outcome: a promise-to-pay with a date, a dispute flag, a wrong-number
  correction, a payment link delivered. Those feed the LMS directly.
* **Humans get the work that needs humans.** Genuine hardship, disputes and
  distress are escalated on detection. The bot handles Tier-1 volume so the
  recovery team handles judgement.

---

## ③ Why Sarvam

Three things a BFSI buyer actually cares about — accuracy, language, sovereignty
— and Sarvam is the only stack that clears all three for this use case.

**Indian-language accuracy.** Generic ASR is trained predominantly on Western
English. Saaras v3 covers 23 languages and, critically, **handles code-mixing
natively**. In live testing it transcribed conversational Hinglish and Tamil-with-
English-nouns correctly, and returned a detected language with a confidence score
(0.99 on clear Hindi) that the agent uses to decide whether to act or re-prompt.

**Natural Indian voices.** Bulbul v3 gives 30+ voices across 11 languages with
prosody control. For collections this is not cosmetic — a warm, unhurried voice
recovers more than a robotic one, and the same script delivered badly reads as
harassment.

**Data sovereignty — the decisive one.** When a bank's security team says *"we
cannot send borrower PII to a US-hosted LLM,"* that is normally where the
procurement conversation ends. Sarvam runs in Indian regions and offers
**on-prem / VPC private model deployment**: inference without data egress. That
single capability clears the largest blocker in the RBI/DPDP environment.

**One vendor, four capabilities.** STT, LLM, TTS and translation from one API and
one key — fewer contracts, one security review, one support path.

### How the SoW maps onto Sarvam

| SoW element | Sarvam-native equivalent |
|---|---|
| Generic multi-language STT | **Saaras v3** — 23 languages, streaming, code-mix aware, server-side VAD |
| Generic TTS | **Bulbul v3** — 30+ Indian voices; μ-law 8 kHz output for telephony |
| Vendor "bot logic" module | **sarvam-105b** chat completions with function calling |
| 6-language coverage | Saaras/Bulbul + **Mayura** translate to expand beyond the six |
| Asterisk gateway, SIP/RTP | unchanged — Sarvam sits behind your existing telephony |
| SFTP CSV personalisation | unchanged — CSV → prompt variables |
| Recording/CDR to customer S3 | unchanged — plus an auditable purge event |
| Transcription summary, sentiment, disposition | batch Saaras + sarvam-105b analytics pipeline |
| Cloud-only data path | **India residency, on-prem/VPC option** |

**"We respect your existing dialer."** Sarvam is not a rip-and-replace. It plugs
in over SIP as the voice agent the current dialer connects to. That single
sentence removes most of the buy-in friction.

---

## ④ What was actually built

A running vertical slice, not a slide deck.

**The live call.** Browser-mic demo (a real phone call is one adapter away):
pre-call compliance gate → grounded greeting → streaming Saaras STT with
server-side VAD → sarvam-105b with tools → guardrail screen → sentence-streamed
Bulbul TTS → barge-in handling. Instrumented per stage.

**The agentic backend.** When the model decides to act, something real happens
**exactly once**: `send_payment_link`, `schedule_ptp`, `escalate_to_human`,
`mark_disposition`, `schedule_callback`. Each writes a row, carries an idempotency
key enforced by a UNIQUE database constraint, retries with backoff, and
dead-letters for reconciliation rather than losing the outcome.

**A proper database.** 15 tables under Alembic migrations: borrowers, CDRs,
per-turn transcripts with latency, tool ledger, promises-to-pay, payment links,
escalations, CRM outbox, analytics reports, compliance events, immutable audit
log. SQLite for the PoC; one `DATABASE_URL` change moves it to Postgres.

**Post-call analytics — 100% QA.** Every completed call is summarised,
sentiment-scored and disposition-classified by sarvam-105b, the summary is
translated to English via Mayura so one reviewer can read all six languages, and
compliance is scored **deterministically** (not by asking the model): was the
recording disclosed, was the agent identified, was it in-window, was there any
threat, was any credential requested.

**Verified, not asserted.** 8/8 live Sarvam API checks, 106 tests, and a headless
call simulator that drives the whole loop with a synthetic borrower and asserts
that a transcript, a tool call and a CDR were all produced.

### Evidence from a real run

```
CALLER  देखिए, अभी मेरे पास पैसे नहीं हैं, सैलरी लेट हो गई है इस महीने।
BOT     मैं समझती हूँ, सैलरी लेट होने से दिक्कत हो सकती है।
BOT     क्या आप कोई तारीख बता सकते हैं जब आप यह ₹4,500 की EMI जमा कर पाएंगे?
CALLER  ठीक है, मैं आठ तारीख़ को पेमेंट कर दूँगा पक्का।
TOOL    schedule_ptp({"loan_id":"PL0098","promised_date":"2026-08-08","amount":4500})
BARGE-IN  caller interrupted; TTS cancelled and buffer flushed
→ disposition PTP · compliance 100/100 · p50 end-of-speech→audio 1142 ms
```

The bot acknowledged hardship before asking again, captured a dated commitment,
and handled being interrupted mid-sentence.

---

## ⑤ The business case

**Per-call cost.** The PoC instruments its own cost per call and stores it in the
CDR. For a ~90-second production call:

| Line | Cost |
|---|---|
| Telephony, 1.5 min @ ₹0.30/min | ₹0.45 |
| Saaras STT (streaming) | ₹0.38 |
| sarvam-105b (~12 turns) | ₹1.20 |
| Bulbul TTS (~12 utterances) | ₹1.80 |
| **Automated call** | **≈ ₹3.80** (range ₹3–6) |
| Human agent touch | **₹40–80** |

**Saving per automated call: ≈ ₹35–75**, before any recovery-rate effect.

> **Caveat, stated plainly:** the AI per-unit rates are **placeholders**, not
> quoted Sarvam pricing — they need replacing with the real rate card
> ([`docs/cost.md`](cost.md) is the single place to change them). The measured
> figure on the PoC's own short test calls is ≈₹1.30; ₹3.80 is that model
> extrapolated to production call length. The case is not sensitive to this: it is
> dominated by the ₹40–80 human baseline and survives the AI rates being off by 2×.

**Illustrative monthly model** — assumptions stated so they can be challenged:

| Assumption | Value |
|---|---|
| Tier-1 EMI reminders / month | 10,000 |
| Automated share | 70% (7,000 calls) |
| Human cost avoided | ₹60/call |
| Automated cost | ₹5/call |
| **Direct saving** | **7,000 × ₹55 ≈ ₹3.85 lakh / month** |

Direct cost saving is the *smaller* half of the case. The larger half:

* **Recovery-rate lift from timeliness.** Every due borrower gets contacted inside
  the window, on day 3 rather than day 25. On an early-bucket book, a 1–2 point
  improvement in cure rate dwarfs the call-cost saving.
* **100% call QA instead of ~2%.** Every call in every language scored for
  compliance. That converts an unquantified regulatory exposure into a measured,
  managed one — and it is simply impossible with human reviewers across six
  languages.
* **Capacity reallocation.** The recovery team stops making reminder calls and
  starts working the cases where negotiation changes the outcome.
* **Audit defensibility.** Every call carries a compliance record and an immutable
  audit trail. In an RBI examination, that is the difference between a finding and
  a conversation.

**The number to remember:** present cost as **₹ per successful contact**, not per
minute. That is the metric a VP of Operations is measured on.

---

## ⑥ Limitations, honestly

What this PoC does **not** do:

* **No real phone call yet.** The demo runs over a browser microphone. The Twilio
  Media Streams handler is written and the μ-law 8 kHz path is verified against
  Bulbul, but it has never run against a carrier — that needs a paid CPaaS account
  and a public URL.
* **Downstream systems are mocked.** Payment gateway, LMS/CRM and WhatsApp are
  adapter classes writing to the local database. Each is one class to repoint.
* **Perceived latency is ~1.1 s, above the 800 ms target.** The pipeline itself is
  ~540 ms; the gap is a deliberate 250 ms utterance-coalescing window and ~220 ms
  of Saaras finalisation. Both have identified fixes (§Architecture) that were not
  worth the correctness risk inside this build.
* **One use case, one bot.** EMI Reminder only. Lead Qualification is scoped but
  not built.
* **sarvam-105b occasionally emits tool calls as text** instead of using the tool
  field. Handled defensively — suppressed from speech and recovered — but it is a
  model behaviour to watch, and it is why the guardrail sits between the model and
  the speaker.
* **No load testing.** The concurrency model is designed and documented; it has
  not been proven at 20,000 calls.

### 90-day rollout

| Phase | Weeks | Outcome |
|---|---|---|
| **1 · Harden** | 1–3 | Real SIP trunk + SBC, live CPaaS number, Postgres, recording capture to Piramal S3 with purge proof, InfoSec review |
| **2 · Shadow** | 4–5 | Bot listens and scores real agent calls. **Zero risk to recovery numbers** while accuracy and compliance scoring are validated against human QA |
| **3 · Pilot** | 6–8 | 10–20% of Tier-1 reminders, 2 languages, parallel run against a control cohort. Measure cure rate, PTP rate, ₹ recovered |
| **4 · Scale** | 9–11 | All six languages, predictive pacing, live payment reconciliation, WhatsApp follow-up, HA/DR, VAPT sign-off |
| **5 · Production** | 12–13 | Full volume, omni-channel, 100% QA dashboards to the collections leadership |

**How this is de-risked.** Nothing reaches production without a parallel run and a
rollback at every step. We start in **shadow mode**, so recovery numbers are never
at risk while the lift is proved. In BFSI, risk-aversion *is* the sale.

---

## The one-slide summary

> Piramal makes tens of thousands of EMI-reminder calls a month at ₹40–80 each,
> across six languages, under RBI conduct rules, with 2% QA coverage.
>
> This agent makes the same call for ₹3–6, in the borrower's own language,
> handling code-mixing and interruption, capturing a dated promise-to-pay and
> sending a payment link — and it scores **100% of calls** for compliance.
>
> It runs on Sarvam's Indian-language models, and it can run **inside Piramal's
> own VPC**, so borrower PII never leaves the perimeter. That is the part a
> US-hosted LLM cannot offer at any price.
