# Build this from scratch, manually

A complete, ordered walkthrough of how this system was built — every command,
every decision, and what to expect at each step. Follow it top to bottom on an
empty folder and you will end up with the same working system.

Written so you can **do it yourself and understand why each piece is there**, not
just copy files. Where a step exists because of something I discovered the hard
way, the discovery is called out inline so you can explain it.

**Total time:** ~4–6 hours if you type it, ~30 minutes if you only run the
verification steps against the existing repo.

---

## Contents

- [Stage 0 · Prerequisites](#stage-0--prerequisites)
- [Stage 1 · Get a Sarvam API key](#stage-1--get-a-sarvam-api-key)
- [Stage 2 · Verify the APIs before writing any code](#stage-2--verify-the-apis-before-writing-any-code)
- [Stage 3 · Project skeleton and dependencies](#stage-3--project-skeleton-and-dependencies)
- [Stage 4 · Config and step logging](#stage-4--config-and-step-logging)
- [Stage 5 · Audio codecs](#stage-5--audio-codecs)
- [Stage 6 · The database](#stage-6--the-database)
- [Stage 7 · Call-list ingestion with the compliance scrub](#stage-7--call-list-ingestion-with-the-compliance-scrub)
- [Stage 8 · Sarvam API clients](#stage-8--sarvam-api-clients)
- [Stage 9 · The agent](#stage-9--the-agent)
- [Stage 10 · The agentic orchestrator](#stage-10--the-agentic-orchestrator)
- [Stage 11 · The live call session](#stage-11--the-live-call-session)
- [Stage 12 · API and WebSocket routes](#stage-12--api-and-websocket-routes)
- [Stage 13 · The browser front end](#stage-13--the-browser-front-end)
- [Stage 14 · Post-call analytics](#stage-14--post-call-analytics)
- [Stage 15 · Telephony bridge](#stage-15--telephony-bridge)
- [Stage 16 · Tests](#stage-16--tests)
- [Stage 17 · Run and verify end to end](#stage-17--run-and-verify-end-to-end)
- [Stage 18 · Git and submission](#stage-18--git-and-submission)
- [Appendix A · Every command in order](#appendix-a--every-command-in-order)
- [Appendix B · Troubleshooting](#appendix-b--troubleshooting)

---

## Stage 0 · Prerequisites

| Need | Version used | Notes |
|---|---|---|
| Python | 3.14.3 | 3.10+ works. See the `audioop` note in Stage 5 |
| Git | any | for the submission repo |
| A browser | Chrome/Edge | needs `AudioWorklet` + `getUserMedia` |
| Sarvam account | free tier | 100 free credits, no card |

```bash
python --version          # 3.10 or newer
git --version
```

You do **not** need Docker, Postgres, Redis, Kafka, or a telephony account.

---

## Stage 1 · Get a Sarvam API key

1. Go to **<https://dashboard.sarvam.ai>**.
2. Sign in (Google, or email).
3. Left sidebar → **API Keys** (the page is `/key-management`).
4. **Create API Key** → name it `emi-collections-agent` → **Create key**.
5. **Copy it immediately** — it is shown once. Format: `sk_xxxxxxxx_...`.

Sanity-check it before building anything on top of it:

```bash
curl -s -X POST https://api.sarvam.ai/v1/chat/completions \
  -H "api-subscription-key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sarvam-105b","messages":[{"role":"user","content":"Say hi"}],"reasoning_effort":null,"max_tokens":20}'
```

You should get JSON with `choices[0].message.content`. A **403** means a bad key
(Sarvam returns 403, not 401, for auth failures).

---

## Stage 2 · Verify the APIs before writing any code

**This is the most important stage, and the one most people skip.** Every hour
spent here saved several later.

Get the real contracts, not blog posts:

```bash
# The whole doc index, LLM-readable:
curl -s https://docs.sarvam.ai/llms.txt

# Any docs page as clean markdown — just append .md:
curl -s https://docs.sarvam.ai/api-reference/chat/chat-completions.md
curl -s https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/ws.md
curl -s https://docs.sarvam.ai/api-reference/text-to-speech/stream.md
```

### What you need to establish

| Fact | Value |
|---|---|
| Base URL | `https://api.sarvam.ai` |
| Auth header | `api-subscription-key` (**not** `Authorization: Bearer`) |
| Auth failure code | **403**, not 401 |
| STT | `POST /speech-to-text`, multipart, model `saaras:v3` |
| STT streaming | `wss://api.sarvam.ai/speech-to-text/ws` |
| TTS | `POST /text-to-speech`, JSON, model `bulbul:v3` |
| TTS streaming | `wss://api.sarvam.ai/text-to-speech/ws` (**mp3 only**) |
| Chat | `POST /v1/chat/completions`, OpenAI-compatible, `sarvam-105b` |
| Translate | `POST /translate`, `mayura:v1` / `sarvam-translate:v1` |
| Language ID | `POST /text-lid` |

### Four findings that shaped the whole design

**① `sarvam-105b` is a reasoning model.** Run this:

```bash
curl -s -X POST https://api.sarvam.ai/v1/chat/completions \
  -H "api-subscription-key: $KEY" -H "Content-Type: application/json" \
  -d '{"model":"sarvam-105b","messages":[{"role":"user","content":"Say OK"}],"max_tokens":10}'
```

You get `"content": null` and `"reasoning_content": "1. **Analyze the User's"`.
The entire budget went on reasoning. `reasoning_effort` defaults to `medium`.

**In a voice call that is dead air.** So: `reasoning_effort: null` on the live
path. And for offline work where you *want* reasoning, `max_tokens` must be
≥ ~3000, because reasoning tokens are billed against it and emitted *first* —
at 800 you get an empty string and `finish_reason: "length"`.

**② The STT WebSocket does VAD for you.** Connect with `vad_signals=true` and
Saaras sends `START_SPEECH` / `END_SPEECH` events alongside transcripts. That is
your endpointing *and* your barge-in trigger — **no local VAD model needed**.

**③ The TTS WebSocket needs `send_completion_event=true`** as a *connection query
parameter*, and the terminating message is `{"type":"event","data":{"event_type":"final"}}`
— note `event` **singular**. Miss this and every sentence appears to hang.

**④ Bulbul v3 rejects v2 voices.** `speaker: "anushka"` returns 400. Ask the API
for the valid list — the error message enumerates them:

```bash
curl -s -X POST https://api.sarvam.ai/text-to-speech \
  -H "api-subscription-key: $KEY" -H "Content-Type: application/json" \
  -d '{"text":"test","target_language_code":"hi-IN","speaker":"anushka","model":"bulbul:v3"}'
```

Also confirm the telephony codec works — this is what makes a real phone call
cheap:

```bash
curl -s -X POST https://api.sarvam.ai/text-to-speech \
  -H "api-subscription-key: $KEY" -H "Content-Type: application/json" \
  -d '{"text":"Test","target_language_code":"hi-IN","speaker":"priya","model":"bulbul:v3",
       "speech_sample_rate":8000,"output_audio_codec":"mulaw"}'
```

μ-law at 8 kHz natively means **no transcoding** on the RTP hot path.

---

## Stage 3 · Project skeleton and dependencies

```bash
mkdir emi-collections-agent && cd emi-collections-agent

mkdir -p src/app/{db,sarvam,agent,orchestrator,analytics,ingest,telephony,routers} \
         src/web scripts tests docs data migrations/versions

# Python packages need __init__.py
for d in src/app src/app/db src/app/sarvam src/app/agent src/app/orchestrator \
         src/app/analytics src/app/ingest src/app/telephony src/app/routers tests; do
  touch "$d/__init__.py"
done

python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\activate           # Windows CMD/PowerShell
# source .venv/bin/activate        # macOS / Linux
```

Install. **Use `--only-binary :all:`** so pip never tries to compile from source:

```bash
pip install --only-binary :all: \
  fastapi "uvicorn[standard]" python-multipart websockets httpx \
  pydantic pydantic-settings python-dotenv \
  SQLAlchemy alembic numpy tzdata pytest pytest-asyncio

pip freeze > requirements.txt
```

**Why `--only-binary`:** on Python 3.14, pinning `pydantic==2.11.7` makes pip
build `pydantic-core` from Rust source, which failed on my machine with
`An Application Control policy has blocked this file`. Binary-only wheels avoid
the whole class of problem.

**Why `tzdata`:** Windows ships no system timezone database, so
`ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError`. The RBI calling-window
check needs it — without `tzdata` **the compliance gate crashes on every call**.
This is a genuine bug I hit; it is not optional on Windows.

Then create `.gitignore` (must include `.env`, `data/*.db*`, `logs/`) and
`.env.example`, and copy it:

```bash
cp .env.example .env
# edit .env, paste your SARVAM_API_KEY
```

---

## Stage 4 · Config and step logging

**`src/app/config.py`** — one typed `Settings` class via `pydantic-settings`. Two
properties earn their keep:

* `reasoning_effort` → returns `None` when the env var is `"null"`, so Stage 2's
  finding ① is expressible in config.
* `offline_mode` → `True` when no key is set, so the whole repo runs without
  credentials.

**`src/app/steplog.py`** — this is what makes the system explainable. Every hop
emits one record to three sinks: console, `logs/steps.jsonl`, and the `events`
table. Stage names match `docs/architecture.md` so you can walk a call hop by hop.

> **Trap I fell into — do not repeat it.** My first version redacted PII by
> running a regex over the *serialised JSON line*. The phone pattern
> `(\+?\d[\d\-\s]{7,}\d)` matched epoch timestamps and UUIDs, turning
> `"ts": 1785360762.05` into `178******60.05`. **All 166 log records became
> unparseable** while the file looked perfectly full.
>
> **Redact values recursively, then serialise.** Keep a never-redact set for
> `ts`, `stage`, `call_id`. Make the phone pattern actually describe a phone
> number (`[6-9]\d{9}` with lookarounds), not "some digits".

Verify:

```bash
python -c "
import sys; sys.path.insert(0,'src')
from app.steplog import step
r = step('call.started','test-id',loan_id='PL0098',phone='+919800000001')
print(r)
"
cat logs/steps.jsonl | python -c "import json,sys; [json.loads(l) for l in sys.stdin]; print('all lines valid JSON')"
```

---

## Stage 5 · Audio codecs

**`src/app/audio.py`** — G.711 μ-law encode/decode, resampling, mono conversion,
a high-pass filter, cheap AGC, and WAV framing.

> **`audioop` was removed from the standard library in Python 3.13.** Every
> tutorial uses `audioop.ulaw2lin` / `audioop.ratecv`; on 3.13+ that is an
> `ImportError`. Implement G.711 on numpy instead. It is ~30 lines each way.

The two conversions that matter:

```
inbound   μ-law 8 kHz → PCM → high-pass + AGC → resample 16 kHz → Saaras
outbound  Bulbul μ-law 8 kHz → straight into RTP (no transcode)
```

**Why this matters commercially:** G.729 compression discards spectral detail
that Indian-language phonemes depend on, so word-error-rate climbs. Insist on
**G.711** on the trunk. That is a pre-sales talking point grounded in the code.

Verify with a real signal, not an assertion of faith:

```bash
python -c "
import sys, math, struct; sys.path.insert(0,'src')
import numpy as np
from app.audio import pcm16_to_mulaw, mulaw_to_pcm16, resample_pcm16
pcm = b''.join(struct.pack('<h', int(12000*math.sin(2*math.pi*440*i/8000))) for i in range(8000))
back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))
a = np.frombuffer(pcm,dtype='<i2').astype(float); b = np.frombuffer(back,dtype='<i2').astype(float)
print('mu-law round-trip correlation:', round(float(np.corrcoef(a,b)[0,1]), 5))
up = resample_pcm16(pcm, 8000, 16000)
x = np.frombuffer(up,dtype='<i2').astype(float)
peak = np.fft.rfftfreq(len(x),1/16000)[np.abs(np.fft.rfft(x*np.hanning(len(x)))).argmax()]
print('440 Hz tone after 8k->16k resample:', round(float(peak),1), 'Hz')
"
```

Expect correlation > 0.999 and ~440 Hz.

---

## Stage 6 · The database

**`src/app/db/models.py`** — 15 tables. The ones that carry the argument:

| Table | Why it exists |
|---|---|
| `borrowers` | the call universe, with `consent` and `dnd_registered` |
| `calls` | **the CDR**; `correlation_id` = the SIP `Call-ID` |
| `turns` | one row per utterance **with the latency that produced it** |
| `tool_invocations` | every side effect, **UNIQUE `idempotency_key`** |
| `promises_to_pay`, `payment_links`, `escalations` | the commercial outputs |
| `crm_writebacks` | outbox — queue, retry, dead-letter, reconcile |
| `analytics_reports` | post-call scoring |
| `compliance_events` | every guardrail decision, with the regulation cited |
| `audit_log` | immutable trail, incl. the vendor-copy purge proof |
| `translation_cache` | one Mayura call per approved string per language |

Four decisions to be able to defend:

1. **Money is integer paise.** Never float near a rupee value.
2. **`tool_invocations.idempotency_key` is UNIQUE.** *That constraint*, not
   application logic, is what makes "exactly one payment link" true.
3. **`JSON().with_variant(JSONB, "postgresql")`** — SQLite locally, Postgres in
   production, one env var, zero code change.
4. **`correlation_id` is UNIQUE** so one trace spans SIP → media → STT → LLM →
   TTS → tools → analytics.

**`src/app/db/base.py`** — engine + `session_scope()`. On SQLite, enable
`PRAGMA foreign_keys=ON` (off by default!) and `journal_mode=WAL`.

**`src/app/db/repo.py`** — every write goes through here.

### Wire up Alembic

```bash
# alembic.ini with script_location = migrations, prepend_sys_path = src,
# and sqlalchemy.url left EMPTY (migrations/env.py reads it from settings)
python -m alembic revision --autogenerate -m "initial collections schema"
python -m alembic upgrade head
```

> **Two traps.**
>
> 1. The autogenerated file renders `postgresql.JSONB(astext_type=Text())` but
>    **does not import `Text`** → `NameError`. Add `from sqlalchemy import Text`.
> 2. Alembic **cannot detect enum *value* changes.** When I later added a
>    `LINK_SENT` disposition it generated an empty migration. On SQLite that is
>    harmless (it is a VARCHAR); on **Postgres it needs `ALTER TYPE ... ADD VALUE`
>    inside an autocommit block**. Hand-write it — see
>    `migrations/versions/8c4f2cc277f8_*.py`.

Verify:

```bash
python -c "
import sqlite3; c=sqlite3.connect('data/emi_agent.db')
t=[r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")]
print(len(t),'tables:',', '.join(t))
"
```

---

## Stage 7 · Call-list ingestion with the compliance scrub

**`src/app/ingest/csv_loader.py`** — the SFTP feed. Do four things before a single
call is placed:

1. **Validate the header contract.** Missing columns raise. Agree column names
   with the customer up front or analytics output will not merge.
2. Coerce and range-check every field.
3. **Scrub:** drop rows without consent (DPDP Act 2023) and rows on the DND
   registry (TRAI). They are **never loaded into the database at all** — the
   strongest posture is not to store them.
4. **Count and report rejections with reasons.** Auditors ask; and one bad row
   must never abort a 20,000-row feed.

Put deliberately-bad rows in `data/call_list.csv` so the scrub is *demonstrable*:
one with `consent=N`, one with `dnd=Y`, one with an empty `emi_amount`.

```bash
python scripts/seed_db.py
```

Expected — and this is a good demo slide:

```
rows read           : 13
loaded              : 10
skipped, no consent : 1   (DPDP Act 2023)
skipped, DND        : 1   (TRAI DND/UCC)
skipped, invalid    : 1
```

---

## Stage 8 · Sarvam API clients

One module per API, all under `src/app/sarvam/`.

**`client.py`** — shared HTTP: `api-subscription-key` header, one pooled
`httpx.AsyncClient`, retries with backoff on 429/5xx, and error mapping that
captures `request_id` (that is what Sarvam support asks for).

**`stt.py`** — two very different jobs:
* `transcribe()` — REST, for analytics. Accuracy over latency.
* `StreamingSTT` — the WebSocket. Connect with:
  ```
  ?model=saaras:v3&mode=transcribe&language-code=unknown&sample_rate=16000
  &input_audio_codec=pcm_s16le&vad_signals=true&flush_signal=true
  ```
  Send `{"audio":{"data":"<b64>","sample_rate":"16000","encoding":"audio/wav"}}`,
  flush with `{"type":"flush"}`. Normalise the three inbound message types
  (`events` / `data` / `error`) into one event stream.

  Use `language-code=unknown` **always** — the call list is often wrong about
  what language the borrower will actually answer in.

**`tts.py`** — `StreamingTTS` (WebSocket, mp3) plus `synthesize()` (REST, any
codec incl. μ-law). Send `config`, then `text`, then `flush`.

> **Keep one socket open for the whole call.** Per-sentence sockets cost
> **382–392 ms** to first audio; a warm socket costs **215–287 ms**. The handshake
> was ~40% of TTFA.
>
> **Two concurrency bugs to avoid.** (a) On barge-in you stop reading, but the
> socket still holds that utterance's remaining chunks — the *next* sentence will
> read them, so the caller hears the sentence they just interrupted. Mark the
> socket dirty and **drain** it. (b) The socket is **not re-entrant**: two
> concurrent `speak()` calls steal each other's chunks. Serialise at the caller
> with a lock — do *not* hold a lock across an async generator, because it
> deadlocks if the consumer abandons iteration.

**`chat.py`** — SSE streaming, accumulating OpenAI-style indexed tool-call
argument fragments. `reasoning_effort=None` for voice; a `REASONING_TOKEN_FLOOR`
of 3000 for anything that leaves reasoning on.

**`translate.py`** — Mayura, with a DB cache.

> **Use Translate for fixed, reviewed strings only.** It paraphrases proper
> nouns: "Generic Finance" came back as **"PrimeLife Finance"**. Placeholder
> protection did not reliably survive either — the model sometimes drops the
> placeholder. For free-form summaries, ask sarvam-105b for the English version
> in the same JSON call; it keeps entity names because it has the context.

**`voices.py`** — language → Bulbul v3 speaker, and `tts_language_for()` so a
borrower answering in a language Bulbul cannot speak still gets a reply (in
Hindi) rather than a failed turn. Saaras understands 23 languages; Bulbul speaks 11.

**`mock.py`** — canned responses for `offline_mode`, so the repo runs with no key.

### Verify all of it at once

Write `scripts/smoke_test_sarvam.py` covering all four APIs, both WebSockets, and
the μ-law path, then:

```bash
python scripts/smoke_test_sarvam.py
```

Expect `8/8 checks passed`. **Do this before writing the agent** — it isolates
"the API changed" from "my code is wrong" for the rest of the build.

---

## Stage 9 · The agent

**`agent/prompt.py`** — the grounded system prompt. Three things make it
production-grade:

1. **Grounding** — the borrower's real numbers injected, with Indian digit
   grouping (`12,50,000` not `1,250,000`) because Bulbul reads comma-grouped
   numbers correctly.
2. **RBI rules as hard constraints** — no threats, no third-party disclosure, no
   waivers, no credential requests, escalate on distress or dispute.
3. **Brevity** — one or two short sentences. This is speech; long turns destroy
   the latency budget *and* the cost model.

**The opening line is deterministic, not model-generated.** The recording
disclosure must be word-for-word identical every time to be defensible in an
audit — and a fixed greeting removes one LLM round trip from the start of the call.

**`agent/sentence.py`** — the single biggest latency win. Aggregate tokens into
sentences so sentence 1 goes to TTS while the model writes sentence 2. Split on
`.?!` **and the Devanagari danda `।`**. Do *not* split inside `Rs. 4,500` or an
abbreviation. Emit a long clause at ~160 chars even without punctuation, so a
model that forgets to punctuate cannot leave the caller waiting.

**`agent/guardrails.py`** — compliance in code, not prose:
* `precall_check()` — consent, DND, calling window, attempt cap. **Fails closed.**
* `screen_utterance()` — a deterministic regex pass over every drafted line
  (threats, unauthorised offers, credential requests). Costs the latency budget
  nothing. If it fails, substitute a **safe line** — never silence, which sounds
  like a dropped call.
* `compliance_summary()` / `compliance_score()` — what the auditor gets shown.

**`agent/tools.py`** — five tools, deliberately small. Note what is **absent**:
no waiver tool, no settlement tool, nothing that accepts a card number. *The bot
cannot do what it was never given.* That is a stronger control than any prompt.

**`agent/salvage.py`** — because the model sometimes writes a tool call into
`content`:

```
BOT  [ { "loanid":
BOT  "PL0098", "promiseddate":
```

Two failures at once: the bot **read JSON to a customer**, and the promise-to-pay
was **lost**. So: check each sentence *before* markdown-stripping (stripping
removes the underscores and quotes that are your evidence), withhold it from TTS,
parse it at end of turn, and dispatch through the normal executor. Put a final
guard inside the speak path so no future code path can regress this.

---

## Stage 10 · The agentic orchestrator

**`orchestrator/adapters.py`** — payment gateway, LMS/CRM, WhatsApp, human queue.
Each mocked behind the interface the real thing exposes; `mock://` vs `https://`
in `.env` selects which. The mocks still write to the database, so the demo shows
a genuine chain.

**`orchestrator/executor.py`** — the boundary between a model's *intention* and a
side effect the business is accountable for:

1. **Idempotency.** Derive a key from `(call, tool, business fields)` — not the
   whole argument blob, so `channel` differing does not create a second link.
   `INSERT` claims it; a duplicate loses on the UNIQUE index and **replays the
   cached result**.
2. **Retries** with backoff on transient failure.
3. **Dead-letter** on permanent failure, with the intent preserved and the model
   told, so it says something honest to the borrower instead of claiming success.
4. **Every effect is a row** before it is reported back.

Verify the guarantee against the *database*, not a mock:

```bash
python -c "
import asyncio, sys; sys.path.insert(0,'src')
from app.orchestrator.executor import execute
from app.db.base import session_scope
from app.db.repo import create_call
from sqlalchemy import select, func
from app.db.models import PaymentLink
import uuid
async def main():
    with session_scope() as s:
        cid = create_call(s, correlation_id='manual-'+str(uuid.uuid4()), loan_id='PL0098').id
    args = {'loan_id':'PL0098','amount':4500}
    a = await execute('send_payment_link', args, call_id=cid)
    b = await execute('send_payment_link', args, call_id=cid)
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(PaymentLink).where(PaymentLink.call_id==cid))
    print('first replayed:', a.replayed, '| second replayed:', b.replayed, '| link rows:', n)
    assert n == 1, 'idempotency broken'
asyncio.run(main())
"
```

Expect `first replayed: False | second replayed: True | link rows: 1`.

---

## Stage 11 · The live call session

**`agent/session.py`** — the hard-real-time loop. This is the hardest file; build
it in this order.

**The state machine:**

```
Listening --(END_SPEECH from Saaras)--> Thinking
Thinking  --(first TTS audio)--------->  Speaking
Speaking  --(START_SPEECH while bot talks)--> Interrupted --> Listening
Speaking  --(utterance complete)------> Listening
```

**Build order:**

1. `start()` — pre-call gate → create CDR → open **both** sockets before the
   greeting (so sentence 1 pays no handshake) → speak the deterministic greeting.
2. `_consume_stt()` — translate Saaras events into transitions.
3. **Utterance coalescing.** Saaras emits **several finals per spoken utterance**
   ("Theek hai," / "main aath tareekh ko kar dunga"). Without coalescing each
   fragment becomes its own LLM turn, the bot answers half a sentence and then
   talks over itself, and turns queue seconds deep. Buffer fragments and only run
   a turn after `STT_COALESCE_MS` (250 ms) of quiet.
4. `_run_llm_turn()` — stream tokens → aggregate sentences → screen → TTS, with a
   fresh `asyncio.Event` per turn as the cancel scope.
5. `_maybe_barge_in()` — on `START_SPEECH` while `Speaking`, wait
   `BARGE_IN_MIN_SPEECH_MS` (280 ms) to filter backchannel ("haan", a cough), then
   set cancel, tell the client to **flush its buffered audio**, hand the floor back.
6. `_dispatch_tools()` → executor → feed results back → let the model narrate.
7. `stop()` — write CDR, transcript, compliance blob, latency stats.

### Getting the latency metric honest

My first version reported **3637 ms** when its own components summed to ~1280 ms.
`TurnTiming` was shared session state, so turn N+1's clock started while turn N
was still speaking — it was measuring *the previous sentence finishing*.

**Fix:** one `TurnTiming` per turn, threaded explicitly. And split it:

| Metric | Meaning |
|---|---|
| `stt_finalisation_ms` | end-of-speech → final transcript |
| `queue_wait_ms` | coalescing + lock wait |
| `pipeline_ms` | admitted → first audio — **what you can engineer** |
| `end_of_speech_to_first_audio_ms` | what the caller perceives |
| `overlapped` | caller stopped while the bot was still talking |

**Exclude `overlapped` turns from the p50/p95** — they are queued behind the bot's
own playback and cannot meet a response budget — but **report the exclusion
count**. Only the *first* sentence of a turn defines that turn's TTFA.

### Language switching without breaking on code-mixing

Naive "detected ≠ current → switch" is wrong. A Tamil speaker saying
"WhatsApp-la anuppunga" gets that fragment labelled `en-IN`, and the bot switches
to English mid-call. So:

* **English inside an Indic conversation is ignored** — that is code-mixing, the
  normal case;
* any other switch needs **two consecutive consistent detections** and confidence
  ≥ 0.75.

### Never under-report the outcome

The model does not always call `mark_disposition`. At hangup, if the disposition
is still `INCOMPLETE`, **derive it from side effects that actually committed** —
reading the database, so a tool that succeeded but whose narration failed still
counts. Order by commercial strength: dated promise → escalation → link sent.

---

## Stage 12 · API and WebSocket routes

**`routers/ws_voice.py`** — the voice channel:
* client → server: **binary** mono PCM16 @ 16 kHz, plus JSON control
  (`hangup`, `dtmf`, `ping`);
* server → client: JSON events (`transcript`, `audio`, `vad`, `barge_in`,
  `state`, `latency`, `tool_call`, `tool_result`, `call_ended`, …).

Accumulate inbound audio into ~100 ms windows before forwarding (one JSON
envelope per 20 ms frame is pure overhead), and **serialise all sends with a
lock** — audio chunks and events share one socket.

**`routers/api.py`** — borrowers, precheck, ingest, calls, call detail, tools as
webhooks (honouring an `Idempotency-Key` header, so an n8n retry is safe),
analytics, stats, and `/api/steps` to tail the trace.

**`main.py`** — app, lifespan (configure logging, `create_all()` so `uvicorn`
alone works), routers, static files with **caching disabled** (a stale `app.js`
silently shows the wrong UI, which is worse than an extra request).

---

## Stage 13 · The browser front end

No build step, no CDN. `src/web/{index.html,dashboard.html,app.js,styles.css}`.

**Mic capture:** `getUserMedia` with `echoCancellation: true` — without it the
bot's own voice re-triggers the VAD and it interrupts itself. An `AudioWorklet`
(defined inline as a Blob, so no extra file) downsamples 48 kHz → 16 kHz and posts
PCM16 off the main thread.

**Playback with barge-in** is the fiddly part. Two paths by content type:
* `audio/mpeg` → **MediaSource** for progressive playback (lowest latency);
* `audio/wav` → `decodeAudioData` + scheduled `AudioContext` sources.

On `barge_in`, **flush**: bump a generation counter (so an in-flight decode is
discarded), clear the pending queue, `abort()`/`remove()` the SourceBuffer, and
`stop()` live nodes. **Without this the caller keeps hearing the bot from the
browser's buffer after the server has stopped sending** — the single most common
reason barge-in "doesn't work".

Then the latency HUD, compliance chips, tool ledger and step trace — all driven
by the same events that go to `logs/steps.jsonl`.

---

## Stage 14 · Post-call analytics

**`analytics/pipeline.py`** — where the 100%-QA claim becomes concrete.

1. **Source the text** — batch Saaras over the recording if one exists, else the
   per-turn transcript (already diarized by construction: we generated one side).
2. **Reason over it** with sarvam-105b, reasoning **ON** (the opposite trade-off
   from the live loop) constrained to JSON, `max_tokens=4000`.
3. **English summary in the same call** (see the Mayura caveat in Stage 8).
4. **Score compliance deterministically** — re-screen every agent line
   independently. The regulator wants a rule, not the model's opinion.

> **Two bugs here cost me an hour.** (a) `step()` takes `stage` as its first
> positional parameter and I also passed `stage=` as a keyword →
> `TypeError: step() got multiple values for argument 'stage'`, which made
> analytics fail for **every** call while the endpoint still returned `200 OK`
> with `{"scored": 0}`. Nothing caught it because `analytics/` had **no tests**.
> (b) Then summaries came back **empty** — `max_tokens=800` was consumed by
> reasoning. Both are why `tests/test_analytics.py` now exists.

```bash
curl -X POST 'http://127.0.0.1:8000/api/analytics/run/pending?limit=10'
curl -s http://127.0.0.1:8000/api/analytics/portfolio | python -m json.tool
```

---

## Stage 15 · Telephony bridge

**`telephony/twilio_media.py`** — the production path the browser demo stands in
for. `CallSession` is transport-agnostic, so **only the audio plumbing differs**:

* in: base64 μ-law 8 kHz → `telephony_to_stt()` → Saaras;
* out: Bulbul μ-law 8 kHz → carrier frames (no transcode);
* the carrier's `CallSid` becomes `calls.correlation_id`;
* on barge-in, send the carrier's buffer-clear message
  (`{"event":"clear","streamSid":…}`) as well as stopping.

**This is written but never run against a carrier** — it needs a paid CPaaS
account and a public HTTPS URL. Say that plainly rather than implying it works.
Activation checklist is in `docs/telephony.md`.

---

## Stage 16 · Tests

`tests/` — 127 tests. What is worth testing here, and why:

| File | Guards |
|---|---|
| `test_audio.py` | hand-written G.711 (no stdlib to lean on): round-trip correlation, 440 Hz survives resampling, 20 ms frame → 640 bytes |
| `test_guardrails.py` | **the regulations**. Every harassment/credential/waiver phrasing blocked; and — just as important — real Hinglish script **not** blocked |
| `test_sentence.py` | Devanagari danda, no split inside `Rs. 4,500`, sentence 1 available before the stream ends |
| `test_orchestrator.py` | idempotency asserted **on the database** — two calls, one link row |
| `test_ingest.py` | header contract, consent/DND scrub, one bad row does not abort the batch |
| `test_salvage.py` | both directions: every leak shape caught, no false positive on real speech |
| `test_analytics.py` | the pipeline runs at all (added after it silently failed) |
| `test_steplog.py` | every line valid JSON **and** PII masked |

Two fixture traps on SQLite:

* A fixture that yields an **open session** holds a write transaction → later
  inserts fail with `database is locked`. Commit and return detached objects
  (`expire_on_commit=False`).
* Do not use `id(obj)` for uniqueness — **CPython recycles addresses**, so two
  tests collided on the UNIQUE `correlation_id`. Use `uuid4()`.

```bash
pytest -q          # expect: 127 passed
```

---

## Stage 17 · Run and verify end to end

```bash
python -m alembic upgrade head
python scripts/seed_db.py
uvicorn app.main:app --app-dir src --reload
```

Open <http://127.0.0.1:8000>, pick a borrower, **Start call**, speak.

Then verify headlessly — this is the check worth keeping in CI:

```bash
python scripts/simulate_call.py --loan-id PL0098 --script hindi
python scripts/simulate_call.py --loan-id PL0102 --script tamil
python scripts/simulate_call.py --loan-id PL0163 --script dispute      # escalation
python scripts/simulate_call.py --loan-id PL0098 --barge-in
```

The simulator synthesises the *borrower's* replies with Bulbul (a different
voice) and streams them into the real WebSocket, so the whole loop is testable
without a human. Expect `PASS`, a disposition, at least one tool call, and
`errors: 0`.

Then confirm the trace and the data:

```bash
# the hop-by-hop trace
python -c "
import json, collections, io
c = collections.Counter()
for l in io.open('logs/steps.jsonl', encoding='utf-8'):
    if l.strip(): c[json.loads(l)['stage']] += 1
print(sum(c.values()), 'records across', len(c), 'stages')
"

# the database
curl -s http://127.0.0.1:8000/api/stats | python -m json.tool
```

To query the database directly: the `sqlite3` CLI is **not installed by default on
Windows**, so use Python, which is always available here:

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/emi_agent.db')
for q in [
    'SELECT disposition, COUNT(*) FROM calls GROUP BY 1',
    'SELECT name, status, substr(idempotency_key,1,12) FROM tool_invocations',
    'SELECT loan_id, promised_date, amount_paise/100.0 FROM promises_to_pay',
]:
    print('--', q)
    for row in c.execute(q): print('  ', row)
"
```

(If you do want the CLI: `winget install SQLite.SQLite`, or use DB Browser for
SQLite for a GUI.)

### If you are outside 08:00–19:00 IST

The RBI gate will (correctly) refuse every call. For a demo only, set
`ENFORCE_CALLING_WINDOW=false`. Every other check still fails closed, and the CDR
still records `in_window: false` and scores 87.5 instead of 100 — the system does
not pretend. **Record your demo video inside the window** for clean 100/100.

---

## Stage 18 · Git and submission

```bash
git init && git branch -M main
git config user.name  "Your Name"
git config user.email "you@example.com"

# Confirm nothing secret is staged BEFORE committing:
git add -A
git diff --cached --name-only | grep -E '^\.env$|\.db(-wal|-shm)?$|steps\.jsonl' \
  && echo "STOP - secret staged" || echo "clean"

git commit -m "Multilingual EMI collections voice agent on Sarvam AI"
```

Two gitignore entries people miss: **SQLite WAL sidecars** (`data/*.db-wal`,
`data/*.db-shm`) — I initially staged them — and `logs/`, which contains
transcripts.

Add a `.gitattributes` with `* text=auto eol=lf` so the repo is clean on any OS.

Then:

```bash
gh repo create emi-collections-agent --private --source=. --push
```

Still to produce by hand: the **3–5 minute demo video**
(`docs/demo-script.md` is a timed shot list) and the email —
subject `[Pre-Sales Assignment] Your Name — BFSI Collections Voice Bot`.

---

## Appendix A · Every command in order

```bash
# --- setup -----------------------------------------------------------------
mkdir emi-collections-agent && cd emi-collections-agent
mkdir -p src/app/{db,sarvam,agent,orchestrator,analytics,ingest,telephony,routers} \
         src/web scripts tests docs data migrations/versions
for d in src/app src/app/db src/app/sarvam src/app/agent src/app/orchestrator \
         src/app/analytics src/app/ingest src/app/telephony src/app/routers tests; do
  touch "$d/__init__.py"; done
python -m venv .venv && source .venv/Scripts/activate
pip install --only-binary :all: fastapi "uvicorn[standard]" python-multipart \
  websockets httpx pydantic pydantic-settings python-dotenv SQLAlchemy alembic \
  numpy tzdata pytest pytest-asyncio
pip freeze > requirements.txt
cp .env.example .env          # then paste SARVAM_API_KEY

# --- write the code (Stages 4-16) ------------------------------------------

# --- database --------------------------------------------------------------
python -m alembic revision --autogenerate -m "initial collections schema"
# add `from sqlalchemy import Text` to the generated file
python -m alembic upgrade head
python scripts/seed_db.py

# --- verify ----------------------------------------------------------------
python scripts/smoke_test_sarvam.py        # 8/8
pytest -q                                  # 127 passed

# --- run -------------------------------------------------------------------
uvicorn app.main:app --app-dir src --reload
# new terminal:
python scripts/simulate_call.py --loan-id PL0098 --script hindi
curl -X POST 'http://127.0.0.1:8000/api/analytics/run/pending?limit=10'

# --- ship ------------------------------------------------------------------
git init && git branch -M main && git add -A
git commit -m "Multilingual EMI collections voice agent on Sarvam AI"
```

---

## Appendix B · Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403` from any Sarvam endpoint | bad/expired key | Sarvam uses 403 not 401; regenerate at dashboard.sarvam.ai |
| `ZoneInfoNotFoundError: Asia/Kolkata` | Windows has no tz database | `pip install tzdata` |
| `ModuleNotFoundError: audioop` | removed in Python 3.13 | use `app/audio.py`; do not use `audioop` |
| `pydantic-core` build fails / blocked | no cp314 wheel for that pin | `pip install --only-binary :all:` and unpin |
| `content: null`, `finish_reason: length` | reasoning ate the budget | `reasoning_effort=null`, or `max_tokens` ≥ 3000 |
| TTS "hangs" ~20 s per sentence | no completion event | `send_completion_event=true` **as a query param**; match `type=="event"` and `event_type=="final"` |
| `speaker 'anushka' is not compatible` | v2 voice on v3 model | use a `bulbul:v3` speaker (`priya`, `aditya`, …) |
| `tts_ttfa` ~2 ms | stale chunks in the shared socket | drain after barge-in; serialise `speak()` |
| Bot reads JSON aloud | tool call emitted as `content` | `agent/salvage.py` |
| Bot answers half a sentence | one utterance, several STT finals | raise `STT_COALESCE_MS` |
| Bot replies in the wrong language | code-mixed fragment labelled `en-IN` | ignore English inside Indic; require 2 votes |
| Bot interrupts itself | mic picking up the speaker | `echoCancellation: true`; raise `BARGE_IN_MIN_SPEECH_MS` |
| Caller still hears bot after interrupting | client/carrier buffer not flushed | flush on `barge_in`; send the carrier's `clear` |
| `database is locked` | fixture holding a write txn | commit and detach; `expire_on_commit=False` |
| `NameError: Text` in a migration | autogenerate omits the import | add `from sqlalchemy import Text` |
| Empty migration after an enum change | Alembic cannot see enum values | hand-write `ALTER TYPE` for Postgres |
| Every call blocked at the gate | outside 08:00–19:00 IST, or attempt cap | `ENFORCE_CALLING_WINDOW=false`; `POST /api/borrowers/reset-attempts` |
| `{"scored": 0}` from analytics | per-call exception swallowed | check `logs/server.log` for the traceback |
| Step log unparseable | redaction applied to serialised JSON | redact values, then serialise |
| UI shows stale behaviour | cached `app.js` | no-cache headers (already set); hard-reload |
