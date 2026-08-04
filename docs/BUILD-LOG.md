# Build log — decisions, evidence, and the bugs found on the way

A chronological record of *why* this system looks the way it does. Written to be
presented: each entry is a decision, the evidence behind it, and what it cost.
Every measurement here came from a live run against `api.sarvam.ai` on
**30 July 2026**, not from documentation.

---

## 1. Read the API before writing the client

**Decision:** verify every Sarvam contract against the live API first, rather
than trusting the assignment brief or the docs prose.

**Why:** the brief's code sketches contained two things that would not have
worked, and one API shape that has since changed.

**What the probes found:**

| Probe | Result |
|---|---|
| `POST /speech-to-text` (Saaras v3) | `200` in **0.80 s** for 8 s of Hindi; perfect transcript; auto-detected `hi-IN` at **0.992** confidence |
| `wss://…/speech-to-text/ws` | Emits `START_SPEECH` / `END_SPEECH` VAD signals **and** the final transcript |
| `wss://…/text-to-speech/ws` | First audio chunk in **234 ms**, `content_type: audio/mpeg`, 15 chunks |
| `POST /v1/chat/completions` (sarvam-105b) | Streams; OpenAI-compatible; tool calling works |
| `POST /translate` (Mayura) | `en-IN → hi-IN` correct, keeps `Rs. 4,500` intact |
| `POST /text-to-speech`, `output_audio_codec: mulaw`, 8 kHz | Works — this is the telephony path |

**Consequences for the design:**

1. **The STT WebSocket does server-side VAD.** With `vad_signals=true` Saaras
   tells us when the caller starts and stops speaking. That is endpointing *and*
   the barge-in trigger, for free — so there is no local VAD (no Silero, no
   webrtcvad) anywhere in this repo. One less model to host and tune.
2. **`send_completion_event=true` is mandatory on the TTS socket.** See §5.
3. **Bulbul rejects `bulbul:v2` voices.** `speaker: "anushka"` — the voice used
   in the brief — returns HTTP 400 against `bulbul:v3`. The valid v3 list is
   captured in `src/app/sarvam/voices.py` from the API's own error message.

---

## 2. sarvam-105b is a reasoning model — and that nearly broke the voice loop

**Found by accident.** The very first chat call returned:

```json
{"choices":[{"finish_reason":"length","message":{"content":null,
  "reasoning_content":"1.  **Analyze the User's"}}]}
```

`content` was **null**. A 10-token budget had been consumed entirely by
`reasoning_content` before a single user-visible token appeared.

**Why it matters:** `reasoning_effort` defaults to `medium`. In a voice turn,
reasoning tokens are pure dead air — the caller hears silence while the model
thinks to itself.

**Decision:** the live loop sets `reasoning_effort=null`, which disables the
reasoning pass. Offline work (post-call analytics, QA scoring) keeps reasoning
**on**, because there accuracy matters and latency does not.

**Measured effect:** time-to-first-token dropped to **~200–310 ms**.

This is the single most important Sarvam-specific finding in the build. It is not
in the assignment brief.

---

## 3. Python 3.13 removed `audioop`

The brief's audio snippet uses `audioop.ulaw2lin` / `audioop.ratecv`. That module
was **deleted from the standard library in Python 3.13**; this machine runs 3.14,
so the snippet cannot run at all.

**Decision:** implement G.711 μ-law and the resampler on numpy in
`src/app/audio.py`, and test them properly (`tests/test_audio.py` asserts
round-trip correlation > 0.999 and that a 440 Hz tone is still 440 Hz after an
8 k → 16 k resample).

Also blocked: `pip install pydantic==2.11.7` tried to compile `pydantic-core`
from Rust source and was refused by this machine's Application Control policy.
Pins were moved to versions with cp314 wheels.

---

## 4. `tzdata` — a compliance control that would have crashed on Windows

The RBI calling-window check does `ZoneInfo("Asia/Kolkata")`. Windows ships no
system tz database, so this raised `ZoneInfoNotFoundError`. Caught by
`tests/test_guardrails.py`, not by a code review.

Had it shipped, the *compliance gate itself* would have thrown on every call on a
Windows host. `tzdata` is now a hard requirement.

---

## 5. Bug: the TTS socket appeared to hang for 20 s per sentence

**Symptom** in the first smoke run: `TTS socket idle for 20s, giving up on this
sentence` — after the audio had already arrived.

**Cause:** I was looking for a completion message of type `"events"`. The actual
message is:

```json
{"type": "event", "data": {"event_type": "final"}}
```

`event`, singular — and it is only sent when `send_completion_event=true` is
passed **as a connection query parameter**, which I had not done.

**Fix:** pass the flag, match on `type == "event"` / `event_type == "final"`, plus
a tight inter-chunk timeout as a fallback.

**Bonus finding:** keeping one socket open for the whole call, instead of opening
one per sentence, cut TTFA from **382–392 ms to 215–287 ms**. Handshake cost was
about 40% of first-audio latency. The session now holds one Bulbul socket per
call.

---

## 6. Bug: the latency metric was measuring the wrong thing

**Symptom:** first end-to-end call reported `END→AUDIO = 3637 ms` while its own
components summed to ~1280 ms.

**Cause:** `TurnTiming` was shared session state. Turns serialise on a lock, so
turn N+1's clock started while turn N was still speaking — the metric was
measuring *the bot finishing its previous sentence*.

**Fix:** one `TurnTiming` per turn, created when the utterance is finalised and
threaded explicitly through the turn. Also split the metric honestly:

| Metric | Meaning |
|---|---|
| `stt_finalisation_ms` | end-of-speech → final transcript |
| `queue_wait_ms` | transcript → turn admitted (coalescing + lock) |
| `pipeline_ms` | turn admitted → first audio — **what we can engineer** |
| `end_of_speech_to_first_audio_ms` | what the caller actually perceives |
| `overlapped` | caller stopped while the bot was still talking |

Turns flagged `overlapped` are **excluded from the p50/p95** and counted
separately (`turns_overlapped_excluded`), because a turn queued behind the bot's
own playback cannot meet a response budget by definition. The exclusion is
reported, never hidden.

Result: `3637 ms → 1142 ms`, and the number now means something.

---

## 7. Bug: barge-in left stale audio in the shared TTS socket

**Symptom:** `tts_ttfa = 1.7 ms`. Physically impossible over a network.

**Cause:** on barge-in we stop reading mid-utterance, but the socket still holds
that utterance's remaining chunks plus its `final` event. The next sentence read
them and reported them as its own first audio — meaning **the caller would hear a
fragment of the sentence they had just interrupted.**

**Fix:** mark the socket dirty on interruption and drain it before the next
sentence (`StreamingTTS._drain`).

**Second hole in the same fix:** when `session.py` breaks out of `async for chunk
in stream`, the generator is abandoned and `speak()`'s own cancel branch never
runs, so the dirty flag was never set. Added an explicit
`StreamingTTS.mark_interrupted()` for consumer-side abandonment.

---

## 8. Bug: two coroutines sharing one TTS socket

**Symptom:** `tts_ttfa = 1.8 ms` again — but on a call with **zero barge-ins**, so
§7 was not the cause.

**Cause:** the closing line is spoken from the hangup path while the last turn's
TTS may still be streaming. The Bulbul socket is a single request/response
channel; two concurrent `speak()` calls interleave their `recv()` loops and steal
each other's chunks.

**Fix:** a session-level `_tts_lock` around `_stream_tts`. Deliberately *not* a
lock inside `speak()` — holding a lock across an async generator deadlocks if the
consumer abandons iteration before the generator is finalised.

---

## 9. Bug: the model read JSON aloud to the borrower

**Symptom** in a live call:

```
BOT  [ { "loanid":
BOT  "PL0098", "promiseddate":
```

...and `tool calls: []`, `disposition=INCOMPLETE`.

**Cause:** sarvam-105b intermittently writes a function call into `content`
instead of the `tool_calls` field. Two failures at once: the bot **spoke JSON to a
customer**, and the captured promise-to-pay was **silently lost**.

**Fix** (`src/app/agent/salvage.py`), defence in depth:

1. Every drafted sentence is checked *before* markdown stripping (stripping
   removes the underscores and quotes that are the evidence).
2. Tool-call-shaped text is withheld from TTS and buffered.
3. At end of turn the buffer is parsed — tolerating truncation, code fences,
   bare argument objects and mangled keys (`loanid` → `loan_id`) — and anything
   recovered is dispatched through the normal idempotent executor.
4. A final guard sits inside `_speak_sentence`, so **no** code path can speak
   JSON even if a future caller forgets step 1.

`tests/test_salvage.py` asserts both directions: every leak shape is caught, and
no line of real Hinglish/Devanagari speech is falsely suppressed (a false
positive here silences the bot, which is worse).

---

## 10. Quality: one utterance was becoming several turns

**Symptom:** a bare `"हाँ।"` triggered its own LLM turn and a stray
`mark_disposition`; turns queued 4 s deep behind each other.

**Cause:** Saaras emits **several finals for one spoken utterance**
("Theek hai," / "main aath tareekh ko kar dunga"). Each was becoming its own
turn, so the bot answered half a sentence and then talked over itself.

**Fix:** coalesce fragments — buffer them and only run a turn after
`STT_COALESCE_MS` (250 ms) of quiet, concatenating the parts. Directly costs
250 ms of perceived latency and is the main tunable in the budget.

---

## 11. Quality: a false language switch

**Symptom:** on a Tamil call, `LANGUAGE ta-IN → en-IN`, after which the bot said
"Thank you for your time" to a Tamil speaker.

**Cause:** the borrower said "WhatsApp-la anuppunga" — Tamil with an English
noun. Saaras labelled that fragment `en-IN`, and the naive rule
"detected ≠ current → switch" fired.

**Fix:** code-mixing is the normal case, not a language change. So:

* English detected inside an Indic conversation is **ignored** as code-mixing;
* any other switch needs **two consecutive consistent detections** and
  confidence ≥ 0.75.

This is the India-first detail a generic stack gets wrong.

---

## 12. Correctness: the outcome must not under-report

A call that delivered a payment link was being written to the CDR as
`INCOMPLETE`, because the model had not called `mark_disposition`.

**Fix:** at hangup, if the disposition is still `INCOMPLETE`, derive it from side
effects that actually committed — reading the **database**, not in-memory state,
so a tool that succeeded but whose narration failed still counts. Ordered by
commercial strength: dated promise → escalation → link sent. Added a `LINK_SENT`
disposition, with a hand-written migration (Alembic cannot autogenerate enum
*value* changes, and Postgres needs `ALTER TYPE` in an autocommit block).

---

## 13. Compliance controls firing for real

Two moments during testing where the system refused to do what I wanted — both
correct:

* **`daily attempt cap reached [RBI recovery-agent norms]`** — after three test
  calls to `PL0098`, the pre-call gate refused the fourth. The counter is genuine
  daily state, so `POST /api/borrowers/reset-attempts` exists as the day-boundary
  reset a dialer would perform.
* **`in_window: FAIL`** — testing ran past 19:00 IST. The call proceeded only
  because `ENFORCE_CALLING_WINDOW=false` is set for demos, and the CDR
  **truthfully records that it was out of window** (compliance score 87.5, not
  100). The system does not flatter itself.

---

## 14. Bug: the entire analytics pipeline was silently failing

**Symptom:** `POST /api/analytics/run/pending` returned `{"scored": 0}` with three
completed calls in the database.

**Cause 1 — a signature collision.** `step()` takes `stage` as its first
positional parameter, and the pipeline was *also* passing `stage="start"` as a
keyword:

```
TypeError: step() got multiple values for argument 'stage'
```

Every call failed, and the per-call `except Exception` logged it and moved on — so
the endpoint returned `200 OK` with zero scored. Nothing caught it because
`analytics/` had **no tests**. Renamed the keyword to `phase=` and added
`tests/test_analytics.py` (8 tests), which now covers exactly this path.

**Cause 2 — reasoning tokens ate the budget.** With the crash fixed, summaries came
back **empty**. Direct probe, `max_tokens=600`:

| `reasoning_effort` | `finish_reason` | `content` | reasoning chars |
|---|---|---|---|
| `low` | `length` | `""` | 2196 |
| `medium` | `length` | `""` | 2182 |
| `null` | `stop` | valid JSON | 0 |

Reasoning tokens are billed against `max_tokens` **and emitted before any
content**. Even `low` effort spent ~900–1000 completion tokens thinking about a
trivial extraction. At `max_tokens=800` the response was truncated mid-reasoning:
`finish_reason: "length"`, `content: ""`.

At `max_tokens=4000` it completes properly (~900–1000 completion tokens, ~7.8 s)
and produces better output than reasoning-off.

**Fix:** a `REASONING_TOKEN_FLOOR = 3000` in `sarvam/chat.py` — any call that
leaves reasoning enabled gets its budget raised automatically — plus a one-shot
retry with reasoning disabled if a response still comes back truncated and empty.
It is now impossible to configure this wrong by accident.

**Generalised rule:** on sarvam-105b, `max_tokens` is a *reasoning + output*
budget, not an output budget. Either disable reasoning (live path) or budget
generously for it (offline path). There is no useful middle setting.

---

## 15. The translator renamed the client

**Symptom:** the English summary of a Hindi call read *"agent Priya from
**PrimeLife Finance**"*. The Hindi original said Generic Finance.

Mayura paraphrases proper nouns. In a document a compliance reviewer reads, or one
that gets attached to a dispute file, silently renaming the lender is not a
cosmetic defect.

**Attempt 1 — placeholder protection.** Swap each entity for an opaque token before
translating, restore after. Partially worked (2 of 3 entities survived) but the
model sometimes **drops the placeholder entirely**, which produced a summary with
the brand name simply missing. Worse than the original problem, so it was removed
rather than shipped.

**Fix:** don't translate free-form summaries at all. sarvam-105b already writes
"Generic Finance" correctly because it has the conversation in context, so it now
returns `summary` and `summary_english` in the **same JSON call** — better entity
fidelity, and one fewer API round trip.

Mayura is still used where it is genuinely the right tool: the **fixed,
legal-reviewed compliance disclosure**, translated once per language and cached in
`translation_cache`. That text is reviewed before it ever ships, so paraphrasing
risk is controlled at authoring time rather than at runtime. It also remains the
fallback if the model omits the English field.

**Verified after the fix:** `brand preserved: True` on both calls, native summary
correctly code-mixed, dispositions matching the CDR (`PTP`, `ESCALATED`).

---

## 16. Bug: PII redaction destroyed the step log

The worst kind of bug — a feature that looked like it worked.

**Symptom:** `logs/steps.jsonl` was 26 KB and full of plausible-looking records.
Parsing it gave **0 usable records out of 166**.

**Cause:** redaction was applied to the *serialised JSON line* rather than to the
values inside it. The phone-number pattern `(\+?\d[\d\-\s]{7,}\d)` happily matched:

* epoch timestamps — `"ts": 1785360762.0594416` became `178******60.0594416`,
  which is not a valid JSON number;
* UUID call ids — `e711a86b-4571-b02d-...` became `e711******86bf-4571-...`.

Every line was corrupt. The structured trace — the thing you actually walk through
when presenting a call — was silently worthless, and nothing noticed because the
file existed and looked full.

**Fix:**

1. Redact **values, recursively**, then serialise. Structure and types survive.
2. A `_NEVER_REDACT` set for structural keys (`ts`, `stage`, `call_id`,
   `correlation_id`, `ms`, `seq`).
3. A phone pattern that actually describes an Indian mobile — `[6-9]` followed by
   9 digits, with optional `+91`/`0`, and negative lookarounds for `.`/`-`/digits
   so it cannot swallow a float or a UUID fragment.
4. `tests/test_steplog.py` (13 tests) asserting **both** directions: every emitted
   line must parse as JSON, *and* phone numbers and loan ids must still be masked.

**Verified after the fix** — one call produces:

```
85 records across 18 stages (0 unparseable)
PII check -> raw phone present: False | PL0098 present: False
ts is float: True | call_id intact: True
```

and a single turn reads cleanly hop by hop:

```
stt.speech_end    {"overlapped": false}
stt.transcript    {"text": "देखिए, अभी मेरे पास पैसे नहीं हैं।", "language": "hi-IN", "confidence": 0.996}
llm.request       {"turns": 5}
llm.first_token   {"ms": 181.3}
tts.first_audio   {"ms": 226.6, "content_type": "audio/mpeg"}
```

**Lesson worth stating out loud:** never transform a serialised document to
sanitise it. Sanitise the data, then serialise. Redaction that corrupts its own
output is worse than no redaction, because it destroys the audit trail while
appearing to protect it.

---

## Where the latency actually goes

Measured p50 across live calls, from `calls.latency_stats`:

```
stt_finalisation      221 ms   Saaras endpoint → final transcript
queue_wait            290 ms   coalescing window (deliberate, tunable)
llm_ttft              231 ms   sarvam-105b, reasoning disabled
tts_ttfa              236 ms   Bulbul, socket already open
                     -------
pipeline_ms           537 ms   admitted → first audio  (engineerable)
end→first audio      1142 ms   what the caller perceives
```

**Honest position:** the pipeline is ~540 ms, comfortably inside budget. The
caller-perceived figure is ~1.1 s, above the 800 ms target, and the two
recoverable costs are the 250 ms coalescing window and Saaras's ~220 ms
finalisation. Both are named in `docs/architecture.md` with what I would do next
(act on stable partials rather than waiting for the final; drop coalescing to
~150 ms once turn quality is measured over more calls).

The dashboard reports `within_budget_pct` per call rather than a single flattering
average.

---

## Verification performed

| Check | Command | Result |
|---|---|---|
| All four Sarvam APIs + both sockets + telephony codec | `python scripts/smoke_test_sarvam.py` | **8/8 pass** |
| Unit + integration tests | `pytest` | **127 pass** |
| Migrations from empty DB | `alembic upgrade head` | 2 revisions, 15 tables, 24 indexes |
| Full Hindi call, headless | `python scripts/simulate_call.py --script hindi` | PTP captured, link sent, compliance 100/100 |
| Tamil call | `--script tamil --loan-id PL0102` | tool call fired, Tamil throughout |
| Consent / DND / malformed-row scrub | `python scripts/seed_db.py` | 10 of 13 loaded, 3 rejected with reasons |

**Not verified:** the real-telephony path
(`src/app/telephony/twilio_media.py`) is written against the documented Twilio
Media Streams protocol and the confirmed Sarvam μ-law output, but has **never run
against a carrier** — that needs a paid CPaaS account and a public HTTPS URL.
Said plainly here and in the README rather than implied to work.
