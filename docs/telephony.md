# Making a real phone call

The browser demo and a real PSTN call differ in **exactly one layer**: audio
transport. `CallSession` is transport-agnostic — it consumes 16 kHz mono PCM and
emits audio events, and does not know or care whether the other end is a
microphone or a carrier.

> **Status: written, not run.** `src/app/telephony/twilio_media.py` targets the
> documented Twilio Media Streams protocol, and the Sarvam side of it (μ-law at
> 8 kHz out of Bulbul's REST endpoint) is **verified** by
> `scripts/smoke_test_sarvam.py`. But it has never handled a call from a real
> carrier, because that needs a paid CPaaS account and a public HTTPS URL. Treat
> the checklist below as untested until you run it.

---

## The two conversions

| Direction | Carrier format | What we do |
|---|---|---|
| Inbound | base64 G.711 μ-law, 8 kHz, 20 ms frames (160 B) | μ-law decode → high-pass + AGC → resample to 16 kHz → Saaras |
| Outbound | same | ask Bulbul for `output_audio_codec: "mulaw"` at 8 kHz — **native, no transcode** |

That second row is the useful detail: Bulbul's REST endpoint emits telephony μ-law
directly, so the RTP path has no resampling or codec work on the hot path. The
WebSocket is mp3-only, which is why the browser uses the socket and the carrier leg
uses REST.

Both conversions live in `src/app/audio.py` (`telephony_to_stt`,
`tts_to_telephony`) and are unit-tested — including that a 440 Hz tone survives an
8 k → 16 k resample, and that a 20 ms μ-law frame becomes exactly 640 bytes of
16 kHz PCM.

---

## Activation checklist (~15 minutes)

**1. Expose the server publicly.** Twilio needs to reach your webhook and open a
WebSocket to you.

```bash
ngrok http 8000
```

**2. Configure `.env`:**

```dotenv
TELEPHONY_PROVIDER=twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx
PUBLIC_BASE_URL=https://your-subdomain.ngrok.app

# The carrier leg wants mu-law, which only the REST endpoint produces.
TTS_TRANSPORT=rest
```

**3. Point the number's voice webhook at:**

```
https://your-subdomain.ngrok.app/telephony/twilio/voice?loan_id=PL0098
```

The handler returns TwiML that opens a bidirectional `<Stream>` back to
`/telephony/twilio/stream`, passing `loan_id` and the carrier's `CallSid`.

**4. Place an outbound call:**

```bash
curl -X POST "https://api.twilio.com/2010-04-01/Accounts/$SID/Calls.json" \
  -u "$SID:$TOKEN" \
  --data-urlencode "To=+9198XXXXXXXX" \
  --data-urlencode "From=$YOUR_TWILIO_NUMBER" \
  --data-urlencode "Url=$PUBLIC_BASE_URL/telephony/twilio/voice?loan_id=PL0098"
```

The pre-call compliance gate still applies. If consent is missing, the number is on
DND, it is outside 08:00–19:00 IST, or the attempt cap is hit, the socket closes
with `1008` and no audio is exchanged.

---

## What the correlation key buys you

`calls.correlation_id` is set to the carrier's `CallSid` (the SIP `Call-ID` in a
self-hosted setup). That is deliberately the same field the browser demo fills with
a synthetic id, so **one trace spans SIP → media → STT → LLM → TTS → tools →
analytics** regardless of channel:

```bash
sqlite3 data/emi_agent.db \
  "SELECT correlation_id, disposition, duration_s FROM calls ORDER BY started_at DESC LIMIT 5;"

curl 'http://127.0.0.1:8000/api/steps?call_id=<uuid>'
```

---

## Barge-in on a carrier leg

The browser flushes its own audio buffer on interruption. A carrier buffers audio
too, so the same problem exists and needs the provider's equivalent:

```json
{"event": "clear", "streamSid": "MZ..."}
```

`twilio_media.py` sends this on the `barge_in` event. Without it, the caller keeps
hearing the bot for as long as the carrier's buffer lasts, even though the server
stopped sending — which is the single most common reason barge-in "doesn't work" in
production.

---

## Other providers

**Plivo / Exotel** use the same shape — an XML answer document plus a
bidirectional audio WebSocket carrying base64 μ-law. Differences are the envelope
field names and the "clear the buffer" message. `twilio_media.py` is ~150 lines;
a sibling module per provider is the intended pattern, not a plugin abstraction.

**Self-hosted (the SoW's model)** — SIP trunk → Kamailio/Asterisk → your media
plane:

* Asterisk **ARI** or FreeSWITCH **ESL** to answer and fork the audio;
* insist on **G.711** on the trunk, not G.729 — G.729's compression discards
  spectral detail that Indian-language phonemes depend on, and word-error-rate
  climbs measurably;
* SIP-TLS on 5061 and SRTP for the media, with the carrier whitelisting your SBC's
  public IP on 5060/5061;
* RTP over a UDP range (e.g. 10000–20000), symmetric, with the SBC handling NAT.

This is the right production topology for BFSI: data stays in the customer's VPC or
DC, which is the whole point of the sovereignty argument. CPaaS is the right choice
for the PoC because it is live in hours.

**Recommend exactly that split in the room** — PoC on CPaaS for speed, production
self-hosted or hybrid for control and residency. It shows commercial judgement
rather than product loyalty.

---

## Known gaps in this path

* **Never run against a carrier.** See the banner above.
* **No recording capture.** The hook belongs at the SBC (fork a media copy) or in
  the media handler. Once a recording URI exists, the analytics pipeline's
  batch-STT branch activates automatically — it is already implemented.
* **No DTMF-driven flows.** Digits arrive (`event: "dtmf"`) and are logged, but
  nothing branches on them. "Press 1 to pay" would ride RFC 2833
  `telephone-event` RTP packets, not audio.
* **No early media.** A `183 Session Progress` ringback path is not handled; the
  agent starts on answer.
* **Single leg.** Warm transfer to a human currently enqueues the escalation and
  ends the bot's turn; it does not bridge the caller to an agent.
