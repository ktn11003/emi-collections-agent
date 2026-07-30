# Cost model

> **Read this first.** The per-unit rates below are **placeholders**, not quoted
> Sarvam pricing. They are labelled as estimates everywhere they appear, and
> `src/app/agent/session.py::_estimate_cost_paise` is the single place to change
> them. Before this goes in front of a customer, replace them with the real rate
> card from <https://docs.sarvam.ai/api/getting-started/pricing> and your telco
> contract. Everything else in this document is arithmetic.

## Where per-call cost comes from

Four lines, two of which scale with call *duration* and two with *conversation
length*:

| Component | Scales with | Placeholder rate |
|---|---|---|
| Telephony | minutes | ₹0.30 / min |
| Saaras STT (streaming) | minutes | ₹0.25 / min |
| Bulbul TTS | bot utterances | ₹0.15 / utterance |
| sarvam-105b | tokens ≈ utterances | ₹0.10 / utterance |

Implemented as:

```python
minutes   = max(duration_s, 1.0) / 60.0
telephony = 30 * minutes          # paise
stt       = 25 * minutes
tts       = 15 * bot_turns
llm       = 40 * max(bot_turns, 1) / 4
```

Cost is stored per call in `calls.cost_paise` (integer paise, never float) and
surfaced at `/api/stats` and on the dashboard.

## Measured vs production

The PoC's own instrumentation reports **≈₹1.30 for a ~20-second test call** with
~10 bot utterances. That is a real measurement of a short call, not a production
one, and quoting it as "the cost per collections call" would be misleading.

A production EMI reminder runs ~90 seconds:

| | Test call (measured) | 90-second call (extrapolated) |
|---|---|---|
| Duration | ~20 s | 90 s |
| Bot utterances | ~10 | ~12 |
| Telephony | ₹0.10 | ₹0.45 |
| Saaras STT | ₹0.08 | ₹0.38 |
| Bulbul TTS | ₹1.50 | ₹1.80 |
| sarvam-105b | ₹1.00 | ₹1.20 |
| **Total** | **≈₹1.30** | **≈₹3.80** |

So the honest statement is: **≈₹3–6 per automated call at production length, on
placeholder rates**, against **₹40–80** for a human agent touch. The saving is
roughly **₹35–75 per automated call**, and it is dominated by the human-cost
baseline, not by the precision of the AI rates — which is why the case survives
even if these placeholders are off by 2×.

## What actually moves the number

Ordered by leverage:

1. **Shorter turns.** TTS and LLM together are ~75% of the cost and both scale
   with how much the bot says. The system prompt already constrains replies to one
   or two short sentences — that is a cost control as much as a UX one.
2. **Right-size the model per turn.** Routing trivial turns ("are you there?",
   acknowledgements) to a smaller model and reserving sarvam-105b for negotiation
   would cut LLM cost materially. Not implemented.
3. **Prompt-prefix caching.** The grounded system prompt is resent every turn.
   Caching the prefix removes most repeated input tokens.
4. **Codec discipline.** G.711 costs more bandwidth than G.729 but G.729 degrades
   STT accuracy on Indian-language phonemes — a wrong transcript costs a whole
   call, so this is a false economy. Keep G.711.
5. **Batch analytics off-peak.** Post-call scoring has no latency requirement, so
   it should run on whatever the cheapest capacity window is.

## The number to present

Not ₹/minute, and not ₹/call: **₹ per successful contact.**

```
₹ per successful contact = total campaign cost / (calls reaching a real outcome)
```

With a 66% PTP-or-better rate — which is what this PoC's three verified calls
produced, on a sample far too small to extrapolate from — ~₹3.80/call becomes
~₹5.75 per successful contact. Against a human baseline of ₹60/touch at a
comparable success rate, that is the slide a VP of Operations remembers.

Compute it live from the database:

```bash
curl -s localhost:8000/api/stats | python -m json.tool
```

which returns the ROI block alongside disposition mix, PTP rate and containment.

## Costs this model ignores

Stated so the number is not mistaken for a total cost of ownership:

* engineering and integration effort;
* SBC / telephony infrastructure, whether hosted or on-prem;
* the compute cost of a private / in-VPC Sarvam deployment, which is the option a
  BFSI buyer will actually choose and is priced very differently from per-call API
  usage;
* DID/number rental and carrier minimums;
* recording storage and its retention lifecycle;
* human agent capacity still required for escalations — this is a deflection
  model, not a replacement model.
