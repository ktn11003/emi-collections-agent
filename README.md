# Generic Collections Agent Bot — on Sarvam AI

A multilingual outbound **collections voice agent** with an agentic backend, a
relational datastore, and a post-call analytics pipeline that scores **100% of
calls** for RBI compliance.

Built on **Saaras v3** (STT) · **sarvam-105b** (LLM + tools) · **Bulbul v3** (TTS)
· **Mayura** (translate).

> **Use case:** early-delinquency EMI reminders for Generic Finance across six
> Indian languages — the money bot in collections, where the ROI is undeniable.
> Business case: [`docs/business-writeup.md`](docs/business-writeup.md).

---

## What it does

Talk to it in your microphone as the borrower. The agent:

1. **Refuses the call if it should not be made** — consent (DPDP), DND (TRAI),
   RBI calling window 08:00–19:00 IST, daily attempt cap. Fails closed, and tells
   you which regulation blocked it.
2. **Opens with a deterministic, legal-approved disclosure**, grounded in the
   borrower's real EMI amount, due date and days-past-due — never an invented
   number.
3. **Listens with Saaras's server-side VAD**, auto-detecting the language. Answer
   in Tamil on a Hindi-scripted call and the voice switches.
4. **Streams the reply sentence-by-sentence** — sentence 1 is spoken while
   sarvam-105b is still writing sentence 2.
5. **Stops instantly if you interrupt it** (barge-in): the LLM stream and the TTS
   socket are cancelled, the socket drained, the client's audio buffer flushed.
6. **Screens every line before speaking it.** Threats, settlement offers and
   credential requests are blocked at the last moment and replaced with a safe
   line — not silence.
7. **Acts, exactly once.** `schedule_ptp`, `send_payment_link`,
   `escalate_to_human`, `mark_disposition`, `schedule_callback` — each with an
   idempotency key enforced by a UNIQUE index, retries, and a dead-letter path.
8. **Writes a CDR**, per-turn transcript with per-stage latency, compliance record
   and audit trail.
9. **Scores itself afterwards** — batch summary, sentiment, disposition, and a
   deterministic compliance score.

---

## Quickstart

```bash
git clone <this repo> && cd emi-collections-agent

python -m venv .venv
.venv/Scripts/activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

cp .env.example .env              # add SARVAM_API_KEY (see below)

alembic upgrade head              # create the schema
python scripts/seed_db.py         # load + scrub the call list

uvicorn app.main:app --app-dir src --reload
```

Open **<http://127.0.0.1:8000>**, pick a borrower, press **Start call**, and speak.
The dashboard is at **/dashboard**, the API docs at **/docs**.

> Want to build it yourself rather than run it? **[`docs/BUILD-FROM-SCRATCH.md`](docs/BUILD-FROM-SCRATCH.md)**
> is the full manual walkthrough — every command in order, the four Sarvam API
> findings that shaped the design, and a troubleshooting table of every failure I
> hit.

### Getting a key

Sign in at **<https://dashboard.sarvam.ai>** → **API Keys** → **Create API Key**,
then put it in `.env`:

```dotenv
SARVAM_API_KEY=sk_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**No key?** It still runs. With `SARVAM_API_KEY` unset the app serves
deterministic canned responses (`src/app/sarvam/mock.py`) so the whole pipeline —
database writes, tool execution, analytics, dashboard — is inspectable before you
have credentials. The banner in the UI says so. Nothing else changes when you add
the key.

### Demoing outside 08:00–19:00 IST

The RBI calling-window gate will (correctly) refuse every call. For a demo only:

```dotenv
ENFORCE_CALLING_WINDOW=false
```

Every other check still fails closed, and the CDR still records
`in_window: false` — the system does not pretend it was compliant.

---

## Verify it yourself

```bash
# 1. Every Sarvam API + both WebSockets + the telephony codec path
python scripts/smoke_test_sarvam.py

# 2. Unit + integration tests (no key needed)
pytest -q

# 3. A full call, headless, with a synthetic borrower (needs the server running)
python scripts/simulate_call.py --loan-id PL0098 --script hindi
python scripts/simulate_call.py --loan-id PL0102 --script tamil
python scripts/simulate_call.py --loan-id PL0098 --script dispute      # escalation
python scripts/simulate_call.py --loan-id PL0098 --barge-in            # interruption
```

Results on this machine, against the live API (30 July 2026):

```
scripts/smoke_test_sarvam.py         8/8 checks passed
  Bulbul v3 TTS (REST)               780,070 bytes, 22050 Hz, mono
  Saaras v3 STT (REST)               694 ms · lang=hi-IN p=1.0
  Saaras v3 STT (WebSocket)          vad=['speech_start','speech_end'] + transcript
  Bulbul v3 TTS (WebSocket)          TTFA 409 ms · 19 chunks
  sarvam-105b chat (streaming)       TTFT 310 ms
  sarvam-105b tool calling           schedule_ptp({...}) emitted correctly
  Mayura translate en->hi            correct, amount preserved
  Bulbul mu-law 8 kHz (telephony)    16,996 bytes for 2.12 s

pytest                               127 passed
```

---

## Which Sarvam APIs are used, and why

| API | Model | Where | Why this one |
|---|---|---|---|
| Speech-to-Text (streaming WS) | `saaras:v3` | live call | 23 languages, code-mix aware, and **`vad_signals=true` gives server-side endpointing** — so there is no local VAD model to host or tune |
| Speech-to-Text (REST) | `saaras:v3` | analytics | higher accuracy than the streaming pass for archive scoring |
| Speech-to-Text (`mode="translate"`) | `saaras:v3` | analytics | Indic speech → English text in one call |
| Chat Completions (streaming + tools) | `sarvam-105b` | dialogue policy | function calling drives the real side effects; **`reasoning_effort=null`** for voice latency |
| Chat Completions (JSON mode) | `sarvam-105b` | analytics | reasoning left **on** — accuracy over latency |
| Text-to-Speech (streaming WS) | `bulbul:v3` | live call | ~230 ms to first audio on a warm socket |
| Text-to-Speech (REST, `mulaw` @ 8 kHz) | `bulbul:v3` | telephony | μ-law natively → **no transcode** on the RTP hot path |
| Translate | `mayura:v1` / `sarvam-translate:v1` | compliance + reporting | localise the approved disclosure once per language (cached); translate summaries to English for HQ |

All four are load-bearing. Remove any one and the system stops doing something a
buyer asked for.

---

## Repo layout

```
├─ README.md
├─ requirements.txt            pinned for Python 3.14 (3.10+ works)
├─ alembic.ini · migrations/   schema as migrations, SQLite + Postgres
├─ data/call_list.csv          SFTP-style feed (with rows that must be rejected)
├─ docs/
│  ├─ BUILD-FROM-SCRATCH.md    ← rebuild it yourself, step by step
│  ├─ business-writeup.md      ← problem · why AI · why Sarvam · ROI · limits
│  ├─ architecture.md          ← diagrams, latency budget, data model, topology
│  ├─ architecture.excalidraw  ← the same diagram, editable for slides
│  ├─ BUILD-LOG.md             ← every decision + the bugs found, with evidence
│  ├─ telephony.md             ← activating a real phone call
│  ├─ cost.md                  ← per-call cost model (rates are placeholders)
│  ├─ security.md              ← implemented vs production-required controls
│  ├─ demo-script.md           ← what to show, in what order
│  └─ screenshots/             ← live call + dashboard
├─ scripts/
│  ├─ seed_db.py               create schema + ingest/scrub the call list
│  ├─ smoke_test_sarvam.py     verify all four APIs end to end
│  └─ simulate_call.py         drive a whole call headlessly
├─ src/app/
│  ├─ config.py steplog.py audio.py
│  ├─ db/          models (15 tables) · session · repository
│  ├─ sarvam/      stt · tts · chat · translate · voices · mock
│  ├─ agent/       prompt · session (state machine) · sentence · guardrails
│  │               tools · salvage
│  ├─ orchestrator/ executor (idempotency) · adapters (payment/CRM/WhatsApp)
│  ├─ analytics/   post-call pipeline
│  ├─ ingest/      CSV loader with consent/DND scrub
│  ├─ telephony/   Twilio Media Streams bridge (written, unrun)
│  └─ routers/     REST API · voice WebSocket
├─ src/web/        demo UI + dashboard (no build step, no CDN)
└─ tests/          127 tests
```

---

## How the latency budget actually performs

Measured p50 across live calls, from `calls.latency_stats`:

| Stage | p50 |
|---|---|
| Saaras finalisation | 221 ms |
| Coalescing / queue wait | 290 ms |
| sarvam-105b first token | 231 ms |
| Bulbul first audio | 236 ms |
| **Pipeline** (admitted → audio) | **537 ms** |
| **End-of-speech → first audio** | **1142 ms** |

**Target is ≤800 ms and the perceived figure misses it.** The pipeline is fine at
~540 ms; the gap is a deliberate 250 ms utterance-coalescing window plus Saaras
finalisation. Both fixes are identified in
[`docs/architecture.md`](docs/architecture.md#why-the-perceived-figure-exceeds-target-and-what-i-would-do-next).

Turns where the caller stopped speaking *while the bot was still talking* are
flagged `overlapped`, excluded from the percentiles, and counted separately —
they are queued behind playback and cannot meet a response budget. The dashboard
shows `within_budget_pct` per call rather than a flattering average.

---

## Three engineering details worth reading the code for

**1. `reasoning_effort=null` — `src/app/sarvam/chat.py`**
sarvam-105b is a reasoning model. At its default `medium`, a live probe returned
`content: null` with the entire token budget spent on `reasoning_content`. In a
voice turn that is dead air. Reasoning is disabled on the live path and left on
for offline analytics.

**2. Idempotency is a database constraint — `src/app/orchestrator/executor.py`**
`tool_invocations.idempotency_key` is UNIQUE over (call, tool, business fields).
Call `send_payment_link` twice and the second call **replays the cached result**;
the borrower never gets two links. Asserted against the database, not a mock, in
`tests/test_orchestrator.py`.

**3. The model sometimes speaks JSON — `src/app/agent/salvage.py`**
Observed live: sarvam-105b wrote a tool call into `content`, so the bot began
reading `[{"loan_id": …` aloud *and* the promise-to-pay was lost. Every sentence
is now checked before TTS; tool-call-shaped text is suppressed, parsed, and
dispatched through the normal executor. A final guard inside `_speak_sentence`
means no code path can ever speak JSON to a customer.

The full list of bugs found and fixed — with the evidence for each — is in
[`docs/BUILD-LOG.md`](docs/BUILD-LOG.md).

---

## Database

15 tables under Alembic. SQLite by default; **Postgres with no code change**:

```dotenv
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/emi_agent
```

`JSON` columns become `JSONB` automatically on Postgres. Money is integer paise,
never float. `calls.correlation_id` is the SIP `Call-ID`, so one trace spans
SIP → media → STT → LLM → TTS → tools → analytics.

Inspect it directly. (The `sqlite3` CLI is not installed by default on Windows, so
this uses Python, which the venv always has):

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/emi_agent.db')
for q in [
    'SELECT disposition, COUNT(*) FROM calls GROUP BY 1',
    'SELECT name, status, substr(idempotency_key,1,12) FROM tool_invocations',
]:
    print('--', q)
    for row in c.execute(q): print('  ', row)
"
```

Or over HTTP: `/api/calls`, `/api/calls/{id}`, `/api/stats`,
`/api/analytics/portfolio`, `/api/steps`.

---

## Step-by-step trace

Every hop emits a step record to three places: the console, `logs/steps.jsonl`,
and the `events` table. The stage names match `docs/architecture.md`, so a
recorded call can be walked hop by hop:

```bash
tail -f logs/steps.jsonl
curl 'http://127.0.0.1:8000/api/steps?limit=50'
```

PII is redacted by default (`REDACT_PII_IN_LOGS=true`): phone numbers and loan IDs
are masked before anything is written.

---

## Known limitations

* **Real telephony is written but unrun.** `src/app/telephony/twilio_media.py`
  targets the documented Twilio Media Streams protocol and the verified Sarvam
  μ-law output, but has never touched a carrier — that needs a paid CPaaS account
  and a public HTTPS URL. See [`docs/telephony.md`](docs/telephony.md).
* **Downstream systems are mocked** (payment gateway, LMS/CRM, WhatsApp) — one
  adapter class each, selected by `mock://` vs `https://` in `.env`.
* **No recording capture**, so analytics scores the live transcript. The batch-STT
  branch is implemented and activates as soon as a recording URI exists.
* **Perceived latency ~1.1 s** vs the 800 ms target — see above.
* **EMI Reminder only.** Lead Qualification is scoped, not built.
* **Not load tested.** The concurrency model is designed and documented, not
  proven at 20,000 calls.

---

## Licence / notes

Built as a technical assignment. Mock borrower data only — no real PII. Sarvam API
usage is billed to whichever key is configured in `.env`.
