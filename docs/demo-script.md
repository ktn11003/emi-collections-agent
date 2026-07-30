# Demo script — 4 minutes, in order

What to show, what to say, and where to click. Timings assume a 3–5 minute video.

Setup before recording:

```bash
python scripts/seed_db.py                                  # fresh call list
curl -X POST http://127.0.0.1:8000/api/borrowers/reset-attempts
uvicorn app.main:app --app-dir src
tail -f logs/steps.jsonl                                   # second terminal, visible
```

Have open: `http://127.0.0.1:8000` and `http://127.0.0.1:8000/dashboard`.

---

## 0:00–0:25 · The problem, in numbers

> "Indian lenders make tens of millions of collections calls a month. A human
> agent touch costs ₹40–80. Piramal's borrowers speak six languages, RBI limits
> when and how you can call them, and human QA samples about 2% of calls.
>
> This is an EMI-reminder voice agent on Sarvam's stack. It makes the same call
> for about ₹5, in the borrower's own language, and scores 100% of calls for
> compliance."

Show the call list on the left — 10 borrowers across six languages.

---

## 0:25–0:50 · Compliance before anything else

Show the terminal output of `python scripts/seed_db.py`:

```
call-list ingest (SFTP feed simulation)
  rows read           : 13
  loaded              : 10
  skipped, no consent : 1   (DPDP Act 2023)
  skipped, DND        : 1   (TRAI DND/UCC)
  skipped, invalid    : 1
      ! PL0215: could not convert string to float: ''
```

> "The feed had 13 rows; 10 are callable. One had no consent on file — that's the
> DPDP Act, so it is **never even loaded into the database**. One is on the TRAI
> DND registry, same treatment. One was malformed and rejected without aborting
> the batch, which matters when the real feed is 20,000 rows.
>
> Compliance here is architecture, not a paragraph in a prompt. It's scrubbed at
> ingest, then **re-checked again** before every single call — consent, DND, the
> RBI calling window, and a daily attempt cap. Two layers, and both fail closed."

Worth mentioning, because it happened during development:

> "While testing I called the same borrower four times and the gate refused the
> fourth: *'daily attempt cap reached — RBI recovery-agent norms.'* I didn't have
> to force that; the control just worked."

If it happens to be outside 08:00–19:00 IST, say so — it is a better demo:

> "It's past 7pm IST right now, so the window check is failing. I've set
> `ENFORCE_CALLING_WINDOW=false` to record this — and you'll see the call record
> still says `in_window: false` at the end. The system doesn't flatter itself."

---

## 0:50–2:00 · The live call (the centrepiece)

Select **Rahul Verma · PL0098 · ₹4,500 · 25 DPD**. Press **Start call**.

The bot opens. While it speaks:

> "That opening line is deterministic, not model-generated — the recording
> disclosure has to be word-for-word identical every time to be defensible in an
> audit. But the numbers in it are real, injected from the call list. It will never
> invent an amount."

**Then do these four things, in this order:**

**1. Answer in Hindi.** *"Haan ji boliye, kaun bol raha hai?"*
Point at the transcript showing `hi-IN` and the confidence score.

**2. Give an objection.** *"Abhi paise nahi hain, salary late ho gayi hai."*

> "Watch it acknowledge the hardship before asking again. In collections, tone
> changes recovery — and an aggressive bot is a compliance incident."

**3. Interrupt it mid-sentence.** Start talking while it is still speaking.

> "That's barge-in. Saaras's own VAD told the server I'd started talking, so it
> cancelled the LLM stream, cancelled the TTS socket, and told the browser to throw
> away the audio it had already buffered. Without that last step the bot keeps
> talking out of the browser's buffer."

Point at the ⚡ barge-in line in the transcript and in the step trace.

**4. Commit to a date.** *"Theek hai, aath tareekh ko kar dunga."*

The **Agentic actions** panel fires. Point at it:

> "`schedule_ptp` — that's not a log line, that's a row in the database with an
> idempotency key. If the model repeats that call, or a client retries, the second
> attempt replays the cached result. A borrower never gets two payment links."

Then: *"Payment link WhatsApp par bhej dijiye."* → `send_payment_link` fires.

Point at the **latency HUD** throughout:

> "STT finalisation, LLM first token, TTS first audio, and the total the caller
> actually perceives. The pipeline is about 540 milliseconds."

---

## 2:00–2:30 · Multilingual, and code-mixing

Hang up. Start a call to **Priya Raman · PL0102 · ta-IN**.

> "Same agent, same code. The call list says Tamil, so the voice is Tamil."

Answer with code-mixing: *"Payment link WhatsApp-la anuppunga."*

> "That's Tamil with English nouns — which is how people actually speak, and where
> generic ASR falls over. Saaras handles it natively.
>
> One detail I had to fix: Saaras labelled that fragment `en-IN`, and my first
> version switched the bot to English. Code-mixing is normal, not a language
> change. So English inside an Indic conversation is now ignored, and a real switch
> needs two consistent detections. Answer in a genuinely different language and it
> switches voice mid-call."

---

## 2:30–3:15 · The database and the analytics

Go to **/dashboard**.

> "Every row here is read from the database. 15 tables under Alembic migrations —
> SQLite for this demo, Postgres with a one-line change and no code edits."

Click a call row. Walk the detail panel:

* **Compliance chips** — "disclosed recording, identified self, no threat, no
  credential request. Scored deterministically, not by asking the model. The
  regulator wants a rule, not an opinion."
* **Agentic actions** — "with the idempotency key visible."
* **Transcript** — "per turn, with the detected language and the latency that
  produced it."

Press **Run post-call analytics**:

> "Batch Saaras, then sarvam-105b for summary, sentiment and disposition, then
> Mayura to translate the summary into English so one reviewer can read all six
> languages. Then a deterministic compliance score.
>
> Human QA samples about 2% of calls. This scores 100% of them, in every language.
> That is not a nice-to-have — it's the difference between an unquantified
> regulatory exposure and a managed one."

Point at the QA coverage figure in the header.

---

## 3:15–3:45 · Why Sarvam

> "Three things a bank actually cares about.
>
> **Accuracy** — Saaras is built for Indian phonetics and code-mixing, not
> retrofitted to it.
>
> **Voice quality** — Bulbul gives me 30-plus Indian voices, and for collections
> the warmth of the voice affects recovery.
>
> **And the one that closes the deal: sovereignty.** When a bank's security team
> says 'we can't send borrower PII to a US-hosted LLM,' that's normally where the
> conversation ends. Sarvam runs in Indian regions and offers on-prem and VPC
> private model deployment — inference without data egress. That clears the biggest
> procurement blocker in Indian BFSI."

Also worth one line:

> "And it plugs into the dialer they already own, over SIP. No rip-and-replace."

---

## 3:45–4:00 · Limits and the plan

Be the engineer who says what doesn't work:

> "Honestly: this demo is over a browser microphone. The Twilio media-stream
> handler is written and I've verified Bulbul produces μ-law at 8 kHz for the RTP
> leg, but it hasn't run against a real carrier — that needs a paid CPaaS account.
> Payment gateway and CRM are mocked behind adapters. Perceived latency is about
> 1.1 seconds against an 800 millisecond target, and I know exactly where those
> 300 milliseconds are.
>
> The rollout is shadow mode first — the bot listens and scores real agent calls,
> so Piramal's recovery numbers are never at risk while we prove the lift. Then a
> pilot on 10–20% of volume with a control cohort. Nothing goes live without a
> parallel run and a rollback."

---

## If asked in the room

**"How does it scale?"**
The scale unit is concurrent calls, not requests per second — each call pins a
media worker plus STT/LLM/TTS sessions. Autoscale on a custom `active_calls`
metric, roughly six calls per pod, stateless workers, graceful drain so live calls
finish before a pod dies. The SBC is the stateful edge: multi-AZ, dual carriers.

**"What happens when the payment gateway is down?"**
The tool retries with backoff, then dead-letters with the intent preserved. The
model is told, so it tells the borrower something honest rather than claiming the
link was sent. `POST /api/tools/replay-dead-letters` reconciles afterwards.

**"How do you stop it saying something that breaches RBI norms?"**
Three layers. The rules are in the system prompt; every drafted line is
independently screened before TTS and replaced with a safe line if it fails; and
the bot has no waiver tool, no settlement tool and no tool that accepts card data
— it can't do what it was never given. Every decision is logged to
`compliance_events` with the regulation cited.

**"Why is the latency above target?"**
250 ms of it is a deliberate coalescing window — Saaras emits several finals per
utterance, and without waiting the bot answers half a sentence. Another ~220 ms is
Saaras finalisation, which I'd attack by acting on stable partials instead of
waiting for the final. The pipeline I control is ~540 ms.

**"Can it do the Lead Qualification bot too?"**
Same architecture: different system prompt, different tool set, different
disposition enum. The campaign table already models it; I built the collections
bot because that's where the ROI argument is strongest.
