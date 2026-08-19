tate.value})
                    if self.state is State.SPEAKING:
                        await self._maybe_barge_in()

                elif ev.kind == "speech_end":
                    self._pending_end_of_speech = time.perf_counter()
                    # If the bot is still talking, this turn is queued behind its
                    # own playback and cannot meet the budget. Record that.
                    self._pending_overlapped = self.state in (State.SPEAKING, State.THINKING)
                    step("stt.speech_end", self.call_id, overlapped=self._pending_overlapped)
                    await self.emit("vad", {"signal": "END_SPEECH", "state": self.state.value})

                elif ev.kind == "transcript":
                    timing = TurnTiming(
                        end_of_speech=self._pending_end_of_speech,
                        stt_final=time.perf_counter(),
                        overlapped=self._pending_overlapped,
                    )
                    self._pending_end_of_speech = None
                    step("stt.transcript", self.call_id, text=ev.text,
                         language=ev.language, confidence=ev.language_confidence)
                    await self.emit("transcript", {
                        "speaker": "BORROWER", "text": ev.text,
                        "language": ev.language, "confidence": ev.language_confidence,
                    })
                    asyncio.create_task(self._on_final_transcript(ev, timing))

                elif ev.kind == "error":
                    step("error", self.call_id, source="stt", detail=ev.detail)
                    await self.emit("error", {"source": "stt", "detail": ev.detail})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("STT consumer died")
            step("error", self.call_id, source="stt_consumer")

    async def _maybe_barge_in(self) -> None:
        """Interrupt the bot — but only for real speech, not backchannel.

        Saaras' own ``min_speech_frames`` already filters very short blips; this
        adds a wall-clock guard so a cough or a "haan" while the bot is
        mid-sentence does not truncate a compliance disclosure.
        """
        await asyncio.sleep(settings.barge_in_min_speech_ms / 1000.0)
        if self.state is not State.SPEAKING:
            return
        self._cancel.set()
        self.state = State.INTERRUPTED
        step("agent.barge_in", self.call_id, after_ms=settings.barge_in_min_speech_ms)
        # The client must drop already-buffered audio, or the bot keeps talking
        # from the browser's buffer after we have stopped sending.
        await self.emit("barge_in", {"flush_audio": True})
        self.state = State.LISTENING

    async def _on_final_transcript(self, ev: Any, timing: TurnTiming) -> None:
        """One caller utterance -> one bot turn."""
        text = (ev.text or "").strip()
        if not text or self.state is State.ENDED:
            return

        stt_ms = timing.as_dict().get("stt_finalisation_ms")
        with session_scope() as s:
            call = get_call(s, self.call_id)
            if call is not None:
                add_turn(
                    s, call, speaker=Speaker.BORROWER, text=text,
                    language=ev.language, language_confidence=ev.language_confidence,
                    latency_ms={"stt_finalisation_ms": stt_ms} if stt_ms else {},
                )

        # Confidence gating: never act on a hypothesis we do not trust.
        if ev.language_confidence is not None and ev.language_confidence < settings.stt_min_language_confidence:
            step("guardrail.check", self.call_id, kind="low_confidence",
                 confidence=ev.language_confidence)
            await self._say(reprompt_line(self.language or "hi-IN"), record=True)
            return

        # Mid-call language switch: the list said Hindi, they answered in Tamil.
        if ev.language:
            await self._consider_language_switch(ev.language, ev.language_confidence)

        # Buffer this fragment and restart the quiet timer. The turn only runs
        # once the caller has actually stopped adding to the utterance.
        self._utterance_parts.append(text)
        if self._utterance_timing is None:
            self._utterance_timing = timing
        else:
            # The caller stopped speaking at the *last* fragment, so that is the
            # instant the response clock should run from.
            self._utterance_timing.end_of_speech = timing.end_of_speech
            self._utterance_timing.stt_final = timing.stt_final
            self._utterance_timing.overlapped |= timing.overlapped

        if self._utterance_task is not None and not self._utterance_task.done():
            self._utterance_task.cancel()
        self._utterance_task = asyncio.create_task(self._flush_utterance())

    async def _flush_utterance(self) -> None:
        """Run one turn for the whole coalesced utterance."""
        try:
            await asyncio.sleep(settings.stt_coalesce_ms / 1000.0)
        except asyncio.CancelledError:
            return   # another fragment arrived; that task will flush instead

        parts, self._utterance_parts = self._utterance_parts, []
        timing, self._utterance_timing = self._utterance_timing, None
        if not parts or timing is None or self.state is State.ENDED:
            return

        text = " ".join(parts).strip()
        step("stt.transcript", self.call_id, coalesced=len(parts), text=text)

        # A turn that has to queue behind an in-flight turn cannot meet the
        # response budget; record that rather than blaming the pipeline.
        contended = self._turn_lock.locked()
        async with self._turn_lock:
            # The controllable clock starts here; everything before was wait.
            timing.turn_admitted = time.perf_counter()
            timing.overlapped = timing.overlapped or contended
            await self._run_llm_turn(text, timing)

    async def _consider_language_switch(self, detected: str, confidence: float | None) -> None:
        """Switch language only on real evidence, not on code-mixing.

        Indian borrowers code-mix constantly — "WhatsApp-la anuppunga" is Tamil
        with an English noun, and Saaras will sometimes label that fragment
        ``en-IN``. Switching on it makes the bot answer a Tamil speaker in
        English, which is worse than not switching at all.

        So: English detected during a call already running in an Indic language
        is treated as code-mixing and ignored, and any other switch needs two
        consecutive consistent detections plus reasonable confidence.
        """
        target = tts_language_for(detected)
        current = self.language or DEFAULT_LANGUAGE
        if target == current:
            self._language_votes.clear()
            return

        # English inside an Indic conversation is USUALLY code-mixing - a Hinglish
        # speaker says "payment" and "due date" without changing language - so it
        # needs more evidence than any other switch. It used to be ignored outright,
        # which meant a borrower who genuinely switched to English was answered in
        # Hindi forever, however many times they tried. Observed on a live call:
        # five consecutive English turns, five Hindi replies.
        english_switch = target == "en-IN" and not current.startswith("en")
        votes_needed = ENGLISH_SWITCH_VOTES if english_switch else LANGUAGE_SWITCH_VOTES

        if confidence is not None and confidence < LANGUAGE_SWITCH_MIN_CONFIDENCE:
            step("guardrail.check", self.call_id, kind="language_switch_low_confidence",
                 detected=detected, confidence=confidence)
            return

        self._language_votes.append(target)
        if len(self._language_votes) < votes_needed:
            step("guardrail.check", self.call_id, kind="language_switch_pending",
                 detected=target, votes=len(self._language_votes), needed=votes_needed)
            return
        if len(set(self._language_votes[-votes_needed:])) > 1:
            return   # detections disagree; keep listening

        self._language_votes.clear()
        await self._switch_language(target)

    async def _switch_language(self, detected: str) -> None:
        target = tts_language_for(detected)
        if target == self.language:
            return
        previous, self.language = self.language, target
        step("llm.request", self.call_id, language_switch=f"{previous}->{target}")
        if self._tts is not None:
            await self._tts.switch_language(target)
        with session_scope() as s:
            add_event(s, self.call_id, "language.switched", {"from": previous, "to": target})
        await self.emit("language_switched", {"from": previous, "to": target,
                                              "voice": voice_for(target)})

    # --- the LLM -> TTS turn -------------------------------------------------
    async def _run_llm_turn(self, user_text: str, timing: TurnTiming) -> None:
        """Stream a reply, speaking each sentence as it completes."""
        self._cancel = asyncio.Event()   # fresh cancellation scope per turn
        self.state = State.THINKING
        self.messages.append({"role": "user", "content": user_text})

        timing.llm_request = time.perf_counter()
        step("llm.request", self.call_id, turns=len(self.messages))
        await self.emit("state", {"state": self.state.value})

        aggregator = SentenceAggregator()
        spoken: list[str] = []
        tool_calls: list[Any] = []
        # Sentences that were JSON rather than speech; parsed after the stream.
        leaked: list[str] = []

        async def handle(sentence: str) -> None:
            """Speak a sentence, unless it is actually a leaked tool call."""
            if looks_like_tool_call(sentence):
                leaked.append(sentence)
                step("guardrail.check", self.call_id, kind="tool_call_leaked_to_speech",
                     suppressed=sentence[:80])
                return
            spoken.append(sentence)
            await self._speak_sentence(sentence, timing)

        try:
            async for delta in stream_chat(self.messages, tools=ALL_TOOLS):
                if self._cancel.is_set():
                    break

                if delta.content:
                    if timing.llm_first_token is None:
                        timing.llm_first_token = time.perf_counter()
                        step("llm.first_token", self.call_id,
                             ms=timing.as_dict().get("llm_ttft_ms"))
                    for sentence in aggregator.push(delta.content):
                        await handle(sentence)
                        if self._cancel.is_set():
                            break

                if delta.tool_calls:
                    tool_calls.extend(delta.tool_calls)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM turn failed")
            step("error", self.call_id, source="llm", detail=str(exc))
            # Degrade to a safe scripted line rather than dead air.
            await self._speak_sentence(reprompt_line(self.language or "hi-IN"), timing)

        if not self._cancel.is_set():
            tail = aggregator.flush()
            if tail:
                await handle(tail)

        # Recover any tool call the model wrote into content instead of the
        # tool_calls field, so a captured PTP is not silently lost.
        if leaked and not tool_calls:
            recovered = salvage(" ".join(leaked), default_loan_id=self.loan_id)
            if recovered:
                step("llm.tool_call", self.call_id, salvaged=True,
                     tools=[c.name for c in recovered])
                await self.emit("tool_salvaged", {"tools": [c.name for c in recovered]})
                tool_calls.extend(recovered)
        elif leaked:
            step("guardrail.check", self.call_id, kind="tool_call_text_discarded",
                 chars=sum(len(s) for s in leaked))

        reply = " ".join(spoken).strip()
        timings = timing.as_dict()
        if reply:
            self.messages.append({"role": "assistant", "content": reply})
            self.bot_lines.append(reply)
            with session_scope() as s:
                call = get_call(s, self.call_id)
                if call is not None:
                    add_turn(s, call, speaker=Speaker.BOT, text=reply,
                             language=self.language, latency_ms=timings,
                             barged_in=self._cancel.is_set())
            if timings:
                self.turn_latencies.append(timings)
                await self.emit("latency", timings)

        if tool_calls:
            await self._dispatch_tools(tool_calls, timing)

        if reply:
            await self._honour_link_promise(reply)

        # Hard ceiling: a conversation this long is not going to converge, and
        # letting it run is exactly the runaway loop borrowers complain about.
        if not self._hangup_requested and len(self.bot_lines) >= MAX_BOT_TURNS:
            step("guardrail.check", self.call_id, kind="max_turns_reached",
                 turns=len(self.bot_lines))
            self._hangup_requested = True
            self._forced_close = True

        if self._hangup_requested and self.state is not State.ENDED:
            await self._hangup()
            return

        if self.state is not State.ENDED:
            self.state = State.LISTENING
            await self.emit("state", {"state": self.state.value})

    async def _honour_link_promise(self, reply: str) -> None:
        """Make good on a spoken claim that the payment link was sent.

        Idempotency in the executor means a correctly-behaving model that already
        called the tool is not double-charged or double-messaged; this only fires
        when the claim was made and nothing was actually sent.
        """
        if self._link_sent or self.state is State.ENDED:
            return
        # Per sentence, and questions do not count: an offer awaiting an
        # answer is not a statement that anything was sent.
        if not any(
            _LINK_NOUN.search(part) and _SEND_CLAIM.search(part)
            for part in _SENTENCE_SPLIT.split(reply)
            if not part.strip().endswith(QUESTION_MARKS)
        ):
            return

        with session_scope() as s:
            borrower = get_borrower(s, self.loan_id)
            if borrower is None:
                raise CallBlocked([(f"unknown loan_id {self.loan_id}", "data")])

            gate = precall_check(borrower)
            self._in_window = gate.checks.get("in_calling_window", True)
            step("dialer.precheck", None, loan_id=self.loan_id, allowed=gate.allowed,
                 **{k: v for k, v in gate.checks.items()})

            if not gate.allowed:
                for reason, regulation in gate.reasons:
                    add_compliance_event(s, None, "precall_block", passed=False,
                                         detail=reason, regulation=regulation)
                raise CallBlocked(gate.reasons)

            self.language = self.language or borrower.language
            self._borrower_name = borrower.name
            self._borrower_phone = borrower.phone
            self._emi_paise = borrower.emi_amount_paise

            call = create_call(
                s,
                correlation_id=self.correlation_id,
                loan_id=self.loan_id,
                campaign_id=self.campaign_id,
                channel=self.channel,
                script_language=self.language,
                compliance={"in_window": self._in_window, "consent_on_file": borrower.consent},
            )
            self.call_id = call.id
            borrower.attempts_today += 1
            add_event(s, self.call_id, "call.started",
                      {"loan_id": self.loan_id, "language": self.language, "channel": self.channel.value})
            add_audit(s, "call.started", entity="call", entity_id=self.call_id,
                      payload={"loan_id": self.loan_id})

            # Localise the mandatory disclosure via Translate (cached) if we do
            # not have a pre-authored native version.
            disclosure = DISCLOSURE_NATIVE.get(self.language)
            system_prompt = build_system_prompt(borrower, language=self.language, disclosure=disclosure)
            greeting = opening_line(borrower, language=self.language, disclosure=disclosure)

        if disclosure is None:
            disclosure = await translate_cached(
                DISCLOSURE_EN, source_language="en-IN", target_language=self.language
            )
            greeting = await translate_cached(
                greeting, source_language="en-IN", target_language=self.language
            )

        self.messages = [{"role": "system", "content": system_prompt}]
        step("call.started", self.call_id, loan_id=self.loan_id, language=self.language)
        await self.emit("call_started", {
            "call_id": self.call_id,
            "correlation_id": self.correlation_id,
            "borrower": {"name": self._borrower_name, "loan_id": self.loan_id,
                         "emi_rupees": self._emi_paise / 100.0},
            "language": self.language,
            "in_window": self._in_window,
        })

        # Open both sockets before the greeting so the first sentence pays no
        # handshake cost.
        self._stt = await StreamingSTT(language="unknown", sample_rate=16000).__aenter__()
        if settings.tts_transport == "ws" and not settings.offline_mode:
            self._tts = await tts_api.StreamingTTS(language=self.language).__aenter__()

        asyncio.create_task(self._consume_stt())
        self.state = State.LISTENING
        await self._say(greeting, record=True)

    async def stop(self, *, disposition: Disposition | None = None) -> dict:
        """Hang up: write the CDR, transcript, compliance blob and latency stats."""
        if self.state is State.ENDED:
            return {}
        self.state = State.ENDED
        self._cancel.set()

        if self._stt is not None:
            await self._stt.close()
            self._stt = None
        if self._tts is not None:
            await self._tts.close()
            self._tts = None

        summary = compliance_summary(
            self.bot_lines,
            borrower=_ShimBorrower(consent=True),
            in_window=self._in_window,
            violation_tags=self.violation_tags,
        )
        score = compliance_score(summary)
        latency = _aggregate_latency(self.turn_latencies)
        final_disposition = disposition or self.disposition
        if final_disposition is Disposition.INCOMPLETE:
            # The model does not always remember to call mark_disposition. Derive
            # the outcome from side effects that actually landed, so the business
            # metric never under-reports a real result.
            final_disposition = self._derive_disposition()

        transcript_uri = None
        with session_scope() as s:
            call = get_call(s, self.call_id)
            if call is None:
                return {}
            call.compliance = summary
            transcript = call_transcript(s, self.call_id)
            transcript_uri = _write_transcript(self.call_id, self.correlation_id, transcript)
            finalise_call(
                s, call,
                disposition=final_disposition,
                latency_stats=latency,
                transcript_uri=transcript_uri,
                cost_paise=_estimate_cost_paise(call.duration_s or 0, len(self.bot_lines)),
            )
            add_compliance_event(
                s, self.call_id, "call_compliance_summary",
                passed=score >= 100.0, detail=json.dumps(summary),
                regulation="RBI recovery-agent norms / DPDP Act 2023",
            )
            add_event(s, self.call_id, "call.ended",
                      {"disposition": final_disposition.value, "compliance_score": score})
            add_audit(s, "call.ended", entity="call", entity_id=self.call_id,
                      payload={"disposition": final_disposition.value})

        step("call.ended", self.call_id, disposition=final_disposition.value,
             compliance_score=score, **latency)
        step("cdr.written", self.call_id, transcript_uri=transcript_uri)
        payload = {
            "call_id": self.call_id,
            "disposition": final_disposition.value,
            "compliance": summary,
            "compliance_score": score,
            "latency": latency,
            "transcript_uri": transcript_uri,
        }
        await self.emit("call_ended", payload)
        return payload

    def _derive_disposition(self) -> Disposition:
        """Infer the outcome from side effects that actually committed.

        Ordered by commercial strength: a dated promise beats a link, a link
        beats nothing. Reads the database rather than trusting in-memory state,
        so a tool that succeeded but whose narration failed still counts.
        """
        from sqlalchemy import select

        from app.db.models import Escalation, PaymentLink, PromiseToPay

        with session_scope() as s:
            if s.scalar(select(PromiseToPay).where(PromiseToPay.call_id == self.call_id)):
                return Disposition.PTP
            if s.scalar(select(Escalation).where(Escalation.call_id == self.call_id)):
                return Disposition.ESCALATED
            link = s.scalar(
                select(PaymentLink).where(
                    PaymentLink.call_id == self.call_id, PaymentLink.status == "SENT"
                )
            )
            if link:
                return Disposition.LINK_SENT
        # Nothing landed. If the caller never said anything, the line never
        # really connected; otherwise the conversation just did not conclude.
        spoke = any(t for t in self.turn_latencies)
        return Disposition.INCOMPLETE if spoke else Disposition.NO_ANSWER

    # --- audio in ------------------------------------------------------------
    async def push_audio(self, pcm16k: bytes) -> None:
        """Feed one window of mono 16 kHz PCM from the caller."""
        if self._stt is None or self.state is State.ENDED:
            return
        await self._stt.send_pcm(pcm16k)

    # --- STT event loop ------------------------------------------------------
    async def _consume_stt(self) -> None:
        """Translate Saaras events into state transitions."""
        assert self._stt is not None
        try:
            async for ev in self._stt.events():
                if self.state is State.ENDED:
                    return

                if ev.kind == "speech_start":
                    self._speech_started_at = time.perf_counter()
                    step("stt.speech_start", self.call_id, state=self.state.value)
                    await self.emit("vad", {"signal": "START_SPEECH", "state": self.state.value})
                    if self.state is State.SPEAKING:
                        await self._maybe_barge_in()

                elif ev.kind == "speech_end":
                    self._pending_end_of_speech = time.perf_counter()
                    # If the bot is still talking, this turn is queued behind its
                    # own playback and cannot meet the budget. Record that.
                    self._pending_overlapped = self.state in (State.SPEAKING, State.THINKING)
                    step("stt.speech_end", self.call_id, overlapped=self._pending_overlapped)
                    await self.emit("vad", {"signal": "END_SPEECH", "state": self.state.value})

                elif ev.kind == "transcript":
                    timing = TurnTiming(
                        end_of_speech=self._pending_end_of_speech,
                        stt_final=time.perf_counter(),
                        overlapped=self._pending_overlapped,
                    )
                    self._pending_end_of_speech = None
                    step("stt.transcript", self.call_id, text=ev.text,
                         language=ev.language, confidence=ev.language_confidence)
                    await self.emit("transcript", {
                        "speaker": "BORROWER", "text": ev.text,
                        "language": ev.language, "confidence": ev.language_confidence,
                    })
                    asyncio.create_task(self._on_final_transcript(ev, timing))

                elif ev.kind == "error":
                    step("error", self.call_id, source="stt", detail=ev.detail)
                    await self.emit("error", {"source": "stt", "detail": ev.detail})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("STT consumer died")
            step("error", self.call_id, source="stt_consumer")

    async def _maybe_barge_in(self) -> None:
        """Interrupt the bot — but only for real speech, not backchannel.

        Saaras' own ``min_speech_frames`` already filters very short blips; this
        adds a wall-clock guard so a cough or a "haan" while the bot is
        mid-sentence does not truncate a compliance disclosure.
        """
        await asyncio.sleep(settings.barge_in_min_speech_ms / 1000.0)
        if self.state is not State.SPEAKING:
            return
        self._cancel.set()
        self.state = State.INTERRUPTED
        step("agent.barge_in", self.call_id, after_ms=settings.barge_in_min_speech_ms)
        # The client must drop already-buffered audio, or the bot keeps talking
        # from the browser's buffer after we have stopped sending.
        await self.emit("barge_in", {"flush_audio": True})
        self.state = State.LISTENING

    async def _on_final_transcript(self, ev: Any, timing: TurnTiming) -> None:
        """One caller utterance -> one bot turn."""
        text = (ev.text or "").strip()
        if not text or self.state is State.ENDED:
            return

        stt_ms = timing.as_dict().get("stt_finalisation_ms")
        with session_scope() as s:
            call = get_call(s, self.call_id)
            if call is not None:
                add_turn(
                    s, call, speaker=Speaker.BORROWER, text=text,
                    language=ev.language, language_confidence=ev.language_confidence,
                    latency_ms={"stt_finalisation_ms": stt_ms} if stt_ms else {},
                )

        # Confidence gating: never act on a hypothesis we do not trust.
        if ev.language_confidence is not None and ev.language_confidence < settings.stt_min_language_confidence:
            step("guardrail.check", self.call_id, kind="low_confidence",
                 confidence=ev.language_confidence)
            await self._say(reprompt_line(self.language or "hi-IN"), record=True)
            return

        # Mid-call language switch: the list said Hindi, they answered in Tamil.
        if ev.language:
            await self._consider_language_switch(ev.language, ev.language_confidence)

        # Buffer this fragment and restart the quiet timer. The turn only runs
        # once the caller has actually stopped adding to the utterance.
        self._utterance_parts.append(text)
        if self._utterance_timing is None:
            self._utterance_timing = timing
        else:
            # The caller stopped speaking at the *last* fragment, so that is the
            # instant the response clock should run from.
            self._utterance_timing.end_of_speech = timing.end_of_speech
            self._utterance_timing.stt_final = timing.stt_final
            self._utterance_timing.overlapped |= timing.overlapped

        if self._utterance_task is not None and not self._utterance_task.done():
            self._utterance_task.cancel()
        self._utterance_task = asyncio.create_task(self._flush_utterance())

    async def _flush_utterance(self) -> None:
        """Run one turn for the whole coalesced utterance."""
        try:
            await asyncio.sleep(settings.stt_coalesce_ms / 1000.0)
        except asyncio.CancelledError:
            return   # another fragment arrived; that task will flush instead

        parts, self._utterance_parts = self._utterance_parts, []
        timing, self._utterance_timing = self._utterance_timing, None
        if not parts or timing is None or self.state is State.ENDED:
            return

        text = " ".join(parts).strip()
        step("stt.transcript", self.call_id, coalesced=len(parts), text=text)

        # A turn that has to queue behind an in-flight turn cannot meet the
        # response budget; record that rather than blaming the pipeline.
        contended = self._turn_lock.locked()
        async with self._turn_lock:
            # The controllable clock starts here; everything before was wait.
            timing.turn_admitted = time.perf_counter()
            timing.overlapped = timing.overlapped or contended
            await self._run_llm_turn(text, timing)

    async def _consider_language_switch(self, detected: str, confidence: float | None) -> None:
        """Switch language only on real evidence, not on code-mixing.

        Indian borrowers code-mix constantly — "WhatsApp-la anuppunga" is Tamil
        with an English noun, and Saaras will sometimes label that fragment
        ``en-IN``. Switching on it makes the bot answer a Tamil speaker in
        English, which is worse than not switching at all.

        So: English detected during a call already running in an Indic language
        is treated as code-mixing and ignored, and any other switch needs two
        consecutive consistent detections plus reasonable confidence.
        """
        target = tts_language_for(detected)
        current = self.language or DEFAULT_LANGUAGE
        if target == current:
            self._language_votes.clear()
            return

        # English inside an Indic conversation is USUALLY code-mixing - a Hinglish
        # speaker says "payment" and "due date" without changing language - so it
        # needs more evidence than any other switch. It used to be ignored outright,
        # which meant a borrower who genuinely switched to English was answered in
        # Hindi forever, however many times they tried. Observed on a live call:
        # five consecutive English turns, five Hindi replies.
        english_switch = target == "en-IN" and not current.startswith("en")
        votes_needed = ENGLISH_SWITCH_VOTES if english_switch else LANGUAGE_SWITCH_VOTES

        if confidence is not None and confidence < LANGUAGE_SWITCH_MIN_CONFIDENCE:
            step("guardrail.check", self.call_id, kind="language_switch_low_confidence",
                 detected=detected, confidence=confidence)
            return

        self._language_votes.append(target)
        if len(self._language_votes) < votes_needed:
            step("guardrail.check", self.call_id, kind="language_switch_pending",
                 detected=target, votes=len(self._language_votes), needed=votes_needed)
            return
        if len(set(self._language_votes[-votes_needed:])) > 1:
            return   # detections disagree; keep listening

        self._language_votes.clear()
        await self._switch_language(target)

    async def _switch_language(self, detected: str) -> None:
        target = tts_language_for(detected)
        if target == self.language:
            return
        previous, self.language = self.language, target
        step("llm.request", self.call_id, language_switch=f"{previous}->{target}")
        if self._tts is not None:
            await self._tts.switch_language(target)
        with session_scope() as s:
            add_event(s, self.call_id, "language.switched", {"from": previous, "to": target})
        await self.emit("language_switched", {"from": previous, "to": target,
                                              "voice": voice_for(target)})

    # --- the LLM -> TTS turn -------------------------------------------------
    async def _run_llm_turn(self, user_text: str, timing: TurnTiming) -> None:
        """Stream a reply, speaking each sentence as it completes."""
        self._cancel = asyncio.Event()   # fresh cancellation scope per turn
        self.state = State.THINKING
        self.messages.append({"role": "user", "content": user_text})

        timing.llm_request = time.perf_counter()
        step("llm.request", self.call_id, turns=len(self.messages))
        await self.emit("state", {"state": self.state.value})

        aggregator = SentenceAggregator()
        spoken: list[str] = []
        tool_calls: list[Any] = []
        # Sentences that were JSON rather than speech; parsed after the stream.
        leaked: list[str] = []

        async def handle(sentence: str) -> None:
            """Speak a sentence, unless it is actually a leaked tool call."""
            if looks_like_tool_call(sentence):
                leaked.append(sentence)
                step("guardrail.check", self.call_id, kind="tool_call_leaked_to_speech",
                     suppressed=sentence[:80])
                return
            spoken.append(sentence)
            await self._speak_sentence(sentence, timing)

        try:
            async for delta in stream_chat(self.messages, tools=ALL_TOOLS):
                if self._cancel.is_set():
                    break

                if delta.content:
                    if timing.llm_first_token is None:
                        timing.llm_first_token = time.perf_counter()
                        step("llm.first_token", self.call_id,
                             ms=timing.as_dict().get("llm_ttft_ms"))
                    for sentence in aggregator.push(delta.content):
                        await handle(sentence)
                        if self._cancel.is_set():
                            break

                if delta.tool_calls:
                    tool_calls.extend(delta.tool_calls)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM turn failed")
            step("error", self.call_id, source="llm", detail=str(exc))
            # Degrade to a safe scripted line rather than dead air.
            await self._speak_sentence(reprompt_line(self.language or "hi-IN"), timing)

        if not self._cancel.is_set():
            tail = aggregator.flush()
            if tail:
                await handle(tail)

        # Recover any tool call the model wrote into content instead of the
        # tool_calls field, so a captured PTP is not silently lost.
        if leaked and not tool_calls:
            recovered = salvage(" ".join(leaked), default_loan_id=self.loan_id)
            if recovered:
                step("llm.tool_call", self.call_id, salvaged=True,
                     tools=[c.name for c in recovered])
                await self.emit("tool_salvaged", {"tools": [c.name for c in recovered]})
                tool_calls.extend(recovered)
        elif leaked:
            step("guardrail.check", self.call_id, kind="tool_call_text_discarded",
                 chars=sum(len(s) for s in leaked))

        reply = " ".join(spoken).strip()
        timings = timing.as_dict()
        if reply:
            self.messages.append({"role": "assistant", "content": reply})
            self.bot_lines.append(reply)
            with session_scope() as s:
                call = get_call(s, self.call_id)
                if call is not None:
                    add_turn(s, call, speaker=Speaker.BOT, text=reply,
                             language=self.language, latency_ms=timings,
                             barged_in=self._cancel.is_set())
            if timings:
                self.turn_latencies.append(timings)
                await self.emit("latency", timings)

        if tool_calls:
            await self._dispatch_tools(tool_calls, timing)

        if reply:
            await self._honour_link_promise(reply)

        # Hard ceiling: a conversation this long is not going to converge, and
        # letting it run is exactly the runaway loop borrowers complain about.
        if not self._hangup_requested and len(self.bot_lines) >= MAX_BOT_TURNS:
            step("guardrail.check", self.call_id, kind="max_turns_reached",
                 turns=len(self.bot_lines))
            self._hangup_requested = True
            self._forced_close = True

        if self._hangup_requested and self.state is not State.ENDED:
            await self._hangup()
            return

        if self.state is not State.ENDED:
            self.state = State.LISTENING
            await self.emit("state", {"state": self.state.value})

    async def _honour_link_promise(self, reply: str) -> None:
        """Make good on a spoken claim that the payment link was sent.

        Idempotency in the executor means a correctly-behaving model that already
        called the tool is not double-charged or double-messaged; this only fires
        when the claim was made and nothing was actually sent.
        """
        if self._link_sent or self.state is State.ENDED:
            return
        # Per sentence, and questions do not count: an offer awaiting an
        # answer is not a statement that anything was sent.
        if not any(
            _LINK_NOUN.search(part) and _SEND_CLAIM.search(part)
            for part in _SENTENCE_SPLIT.split(reply)
            if not part.strip().endswith(QUESTION_MARKS)
        ):
            return

        with session_scope() as s:
            borrower = get_borrower(s, self.loan_id)
            amount = (borrower.emi_amount_paise / 100.0) if borrower else 0.0
        if not amount:
            return

        step("guardrail.check", self.call_id, kind="link_promised_but_not_sent",
             said=reply[:100])
        result = await execute_tool(
            "send_payment_link",
            {"loan_id": self.loan_id, "amount": amount, "channel": "WHATSAPP"},
            call_id=self.call_id,
        )
        self._link_sent = bool(result.ok)
        step("llm.tool_call", self.call_id, tool="send_payment_link",
             auto_dispatched=True, ok=result.ok)
        await self.emit("tool_call", {"name": "send_payment_link", "auto": True,
                                      "arguments": {"loan_id": self.loan_id, "amount": amount}})
        await self.emit("tool_result", {"name": "send_payment_link", "ok": result.ok,
                                        "auto": True, "data": result.data,
                                        "error": result.error})
        # The model must know the link exists, or it will offer it again.
        self.messages.append({
            "role": "system",
            "content": ("The payment link has been sent on WhatsApp. Do not offer "
                        "or send it again."),
        })

    async def _hangup(self) -> None:
        """End the call from the agent's side.

        ``stop()`` is idempotent, so the router's own stop() in its finally block
        becomes a no-op.

        If a backstop fired rather than the agent choosing to end -- a detected
        loop, the turn ceiling -- nothing has been recorded and the call would
        land as INCOMPLETE, which tells the collections floor nothing. A bot that
        got stuck on a live borrower is a warm transfer, so hand it to a human
        and disposition it properly on the way out.
        """
        if self._forced_close and self.disposition is Disposition.INCOMPLETE:
            step("agent.hangup", self.call_id, forced=True, reason="loop_or_turn_cap")
            try:
                await execute_tool(
                    "escalate_to_human",
                    {"loan_id": self.loan_id, "reason": "COMPLEX_QUERY",
                     "context": "Agent could not progress the call; handing to a human."},
                    call_id=self.call_id,
                )
                await execute_tool(
                    "mark_disposition",
                    {"loan_id": self.loan_id, "disposition": "ESCALATED",
                     "notes": "Auto-escalated: conversation stopped progressing."},
                    call_id=self.call_id,
                )
                self.disposition = Disposition.ESCALATED
            except Exception:  # noqa: BLE001
                logger.exception("forced-close escalation failed for %s", self.call_id)

        await self.emit("agent_hangup", {"reason": "agent ended the call"})
        try:
            await self.say_closing()
        finally:
            await self.stop()

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())

    def _is_repeat(self, text: str) -> bool:
        """Has this sentence, or the substance of it, already been spoken?

        The prompt forbids repetition at length; the model does it anyway, and a
        borrower hearing the same thing twice is the single loudest failure this
        agent has. So it is also checked here, where it can be stopped.

        Three ways a repeat shows up, all of them seen on real calls:

        * verbatim, or differing only in case and punctuation;
        * reworded a little ("kab pay kar sakte hain" / "kab payment kar sakte
          hain") -- caught by similarity;
        * the same question wearing a new prefix ("haan, maine aapko call kiya
          hai, kya aap PL0102 loan account ki baat kar rahi hain?") -- whole-string
          similarity misses these entirely, because the padding dilutes it, so
          containment is checked separately.
        """
        norm = self._normalise(text)
        if len(norm) < REPEAT_MIN_CHARS:
            return False
        for prior in self._spoken_norm:
            if prior == norm:
                return True
            if SequenceMatcher(None, prior, norm).ratio() >= REPEAT_SIMILARITY:
                return True
            # Containment either way: a padded restatement of an earlier line, or
            # an earlier line trimmed down and sent again.
            if len(prior) >= REPEAT_SUBSTRING_MIN_CHARS and prior in norm:
                return True
            if len(norm) >= REPEAT_SUBSTRING_MIN_CHARS and norm in prior:
                return True
        return False

    async def _speak_sentence(
        self, sentence: str, timing: TurnTiming, *, guard_repeats: bool = True
    ) -> None:
        """Screen, then synthesise and stream one sentence.

        ``guard_repeats=False`` for deterministic lines -- the opening, the
        closing, the reprompt. Those are ours, not the model's, and re-stating
        who you are because the borrower asked is required behaviour, not a loop.
        """
        # Last line of defence: no code path may read JSON to a borrower, even
        # if a caller of this method forgot to check (see agent/salvage.py).
        if looks_like_tool_call(sentence):
            step("guardrail.check", self.call_id, kind="tool_call_blocked_at_tts",
                 suppressed=sentence[:80])
            return

        text = strip_for_speech(sentence)
        if not text:
            return

        screen = screen_utterance(text)
        if not screen.allowed:
            self.violation_tags.extend(screen.tags)
            step("compliance.flag", self.call_id, tags=screen.tags, blocked=True)
            with session_scope() as s:
                for tag, regulation in screen.violations:
                    add_compliance_event(s, self.call_id, tag, passed=False,
                                         detail=text[:300], regulation=regulation)
            await self.emit("compliance_block", {"tags": screen.tags, "replacement": screen.safe_text})
            text = screen.safe_text or ""
            if not text:
                return
        else:
            step("guardrail.check", self.call_id, passed=True, chars=len(text))

        if guard_repeats and self._is_repeat(text):
            self._repeats_suppressed += 1
            step("guardrail.check", self.call_id, kind="repeat_suppressed",
                 count=self._repeats_suppressed, suppressed=text[:80])
            await self.emit("repeat_suppressed", {"text": text,
                                                  "count": self._repeats_suppressed})
            if self._repeats_suppressed >= MAX_REPEATS_BEFORE_CLOSE:
                step("guardrail.check", self.call_id, kind="loop_detected_closing")
                self._hangup_requested = True
                self._forced_close = True
            return

        if guard_repeats:
            self._spoken_norm.append(self._normalise(text))
        await self.emit("transcript", {"speaker": "BOT", "text": text, "language": self.language})
        await self._stream_tts(text, timing)

    async def _stream_tts(self, text: str, timing: TurnTiming) -> None:
        """Synthesise and push audio to the caller, cancellable at any chunk.

        Serialised: the Bulbul socket is not re-entrant, and the hangup/closing
        path can otherwise overlap an in-flight turn.
        """
        async with self._tts_lock:
            await self._stream_tts_locked(text, timing)

    async def _stream_tts_locked(self, text: str, timing: TurnTiming) -> None:
        self.state = State.SPEAKING
        # Only the *first* sentence of a turn defines that turn's TTFA; later
        # sentences overlap with playback and would understate the metric.
        measuring = timing.tts_first_audio is None
        if measuring:
            timing.tts_request = time.perf_counter()
        step("tts.request", self.call_id, chars=len(text), transport=settings.tts_transport)

        first = True
        chunks = 0
        try:
            if self._tts is not None:
                stream = self._tts.speak(text, self._cancel)
            else:
                stream = tts_api.stream_sentence(
                    text, language=self.language or "hi-IN", cancel=self._cancel
                )
            async for chunk in stream:
                if self._cancel.is_set():
                    # Abandoning the generator here means speak() never runs its
                    # own cancel branch, so flag the socket explicitly.
                    if self._tts is not None:
                        self._tts.mark_interrupted()
                    step("tts.complete", self.call_id, cancelled=True, chunks=chunks)
                    return
                if first:
                    first = False
                    if measuring:
                        timing.tts_first_audio = time.perf_counter()
                        step("tts.first_audio", self.call_id,
                             ms=timing.as_dict().get("tts_ttfa_ms"),
                             content_type=chunk.content_type)
                self._audio_seq += 1
                chunks += 1
                await self.emit("audio", {
                    "seq": self._audio_seq,
                    "content_type": chunk.content_type,
                    "b64": _b64(chunk.data),
                })
        except Exception as exc:  # noqa: BLE001
            logger.exception("TTS failed")
            step("error", self.call_id, source="tts", detail=str(exc))
            return

        step("tts.complete", self.call_id, chunks=chunks)
        step("media.audio_out", self.call_id, chunks=chunks)
        if self.state is State.SPEAKING:
            self.state = State.LISTENING

    # --- tools ---------------------------------------------------------------
    async def _dispatch_tools(self, tool_calls: list[Any], timing: TurnTiming, *, round_: int = 1) -> None:
        """Execute the model's tool calls, then let it narrate the result.

        The narration pass may itself want a tool -- ``mark_disposition`` almost
        always arrives there, because the model records the outcome only once it
        has seen the first tool succeed. So follow-on calls are dispatched rather
        than dropped, bounded by MAX_TOOL_ROUNDS so a model that keeps calling
        tools cannot hold the call open indefinitely.
        """
        self.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in tool_calls
            ],
        })

        hints: list[str] = []
        for tc in tool_calls:
            step("llm.tool_call", self.call_id, tool=tc.name, args=tc.arguments)
            await self.emit("tool_call", {"name": tc.name, "arguments": tc.arguments})

            if tc.name == "end_call":
                # Honoured after this batch finishes, so mark_disposition in the
                # same batch still commits before the line drops.
                self._hangup_requested = True

            result = await execute_tool(tc.name, tc.arguments, call_id=self.call_id)
            self.messages.append(result.as_tool_message(tc.id))
            await self.emit("tool_result", {
                "name": tc.name, "ok": result.ok, "replayed": result.replayed,
                "data": result.data, "error": result.error,
            })
            if result.speech_hint:
                hints.append(result.speech_hint)

            # Tool outcomes drive the CDR disposition.
            if result.ok:
                if tc.name == "send_payment_link":
                    self._link_sent = True
                if tc.name == "schedule_ptp":
                    self.disposition = Disposition.PTP
                elif tc.name == "escalate_to_human":
                    self.disposition = Disposition.ESCALATED
                elif tc.name == "schedule_callback":
                    self.disposition = Disposition.CALLBACK
                elif tc.name == "mark_disposition":
                    with contextlib.suppress(ValueError):
                        self.disposition = Disposition(result.data.get("disposition", ""))

        # Let the model tell the borrower what just happened, in their language.
        # Called from inside _run_llm_turn, which already holds _turn_lock.
        # A narration pass after a hangup is the loop this tool exists to stop.
        if self._hangup_requested:
            step("agent.hangup", self.call_id, requested_by="end_call")
            return

        if hints and self.state is not State.ENDED:
            follow_on = await self._narrate_tool_outcome()
            if follow_on:
                if round_ < MAX_TOOL_ROUNDS:
                    await self._dispatch_tools(follow_on, timing, round_=round_ + 1)
                else:
                    step("guardrail.check", self.call_id, kind="tool_round_cap",
                         dropped=[c.name for c in follow_on])

    async def _narrate_tool_outcome(self) -> list[Any]:
        """Second LLM pass so the bot confirms the action it just took.

        Returns any tool calls the model made during that pass, for the caller to
        dispatch. Dropping them here used to make ``mark_disposition`` structurally
        unreachable after any other tool: the model reliably asks for it in this
        pass, and every one of those requests was discarded.

        Gets its own timing that is *not* folded into the turn's budget: the
        caller already heard audio for this turn, so this is follow-on speech,
        not response latency.
        """
        timing = TurnTiming(turn_admitted=time.perf_counter())
        timing.llm_request = timing.turn_admitted
        aggregator = SentenceAggregator()
        spoken: list[str] = []
        tool_calls: list[Any] = []
        leaked: list[str] = []

        def handle(sentence: str) -> str | None:
            """Withhold a leaked tool call from speech, same as the main turn."""
            if looks_like_tool_call(sentence):
                leaked.append(sentence)
                step("guardrail.check", self.call_id, kind="tool_call_leaked_to_speech",
                     phase="narrate", suppressed=sentence[:80])
                return None
            return sentence

        try:
            async for delta in stream_chat(self.messages, tools=ALL_TOOLS):
                if self._cancel.is_set():
                    break
                if delta.content:
                    if timing.llm_first_token is None:
                        timing.llm_first_token = time.perf_counter()
                    for sentence in aggregator.push(delta.content):
                        if handle(sentence) is None:
                            continue
                        spoken.append(sentence)
                        await self._speak_sentence(sentence, timing)
                if delta.tool_calls:
                    tool_calls.extend(delta.tool_calls)
        except Exception as exc:  # noqa: BLE001
            step("error", self.call_id, source="llm_narrate", detail=str(exc))
            return []

        tail = aggregator.flush()
        if tail and handle(tail) is not None:
            spoken.append(tail)
            await self._speak_sentence(tail, timing)

        # Same recovery as the main turn: a tool written into content instead of
        # the tool_calls field is still a real outcome and must not be lost.
        if leaked and not tool_calls:
            recovered = salvage(" ".join(leaked), default_loan_id=self.loan_id)
            if recovered:
                step("llm.tool_call", self.call_id, salvaged=True, phase="narrate",
                     tools=[c.name for c in recovered])
                await self.emit("tool_salvaged", {"tools": [c.name for c in recovered]})
                tool_calls.extend(recovered)

        reply = " ".join(spoken).strip()
        if reply:
            self.messages.append({"role": "assistant", "content": reply})
            self.bot_lines.append(reply)
            with session_scope() as s:
                call = get_call(s, self.call_id)
                if call is not None:
                    add_turn(s, call, speaker=Speaker.BOT, text=reply, language=self.language,
                             latency_ms=timing.as_dict())
        return tool_calls

    # --- helpers -------------------------------------------------------------
    async def _say(self, text: str, *, record: bool = False) -> None:
        """Speak a deterministic line (greeting, reprompt, closing).

        Bot-initiated, so there is no caller utterance to measure against; only
        TTS TTFA is meaningful and it stays out of the turn budget.
        """
        self._cancel = asyncio.Event()
        timing = TurnTiming(turn_admitted=time.perf_counter())
        # Long deterministic lines still stream sentence by sentence.
        sentences = split_sentences(text) or [text]
        for sentence in sentences:
            if self._cancel.is_set():
                break
            await self._speak_sentence(sentence, timing, guard_repeats=False)
        if record:
            self.bot_lines.append(text)
            self.messages.append({"role": "assistant", "content": text})
            with session_scope() as s:
                call = get_call(s, self.call_id)
                if call is not None:
                    add_turn(s, call, speaker=Speaker.BOT, text=text, language=self.language,
                             latency_ms={k: v for k, v in timing.as_dict().items()
                                         if k in ("tts_ttfa_ms",)})

    async def say_closing(self) -> None:
        if self._closing:
            return
        self._closing = True
        language = self.language or DEFAULT_LANGUAGE
        line = CLOSING_NATIVE.get(language)
        if line is None:
            # Not pre-authored: translate once and cache, exactly as the opening
            # disclosure does. Falls back to English rather than to Hindi, which
            # is at least neutral if the translation hop fails.
            try:
                line = await translate_cached(
                    CLOSING_EN, source_language="en-IN", target_language=language
                )
            except Exception:  # noqa: BLE001
                logger.warning("closing-line translation failed for %s", language)
                line = CLOSING_EN
        await self._say(line, record=True)


@dataclass(slots=True)
class _ShimBorrower:
    """compliance_summary only needs ``consent``; avoids holding a live ORM row."""

    consent: bool


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile on a sorted list."""
    if not values:
        return 0.0
    idx = min(len(values) - 1, max(0, int(round(pct * (len(values) - 1)))))
    return values[idx]


def _aggregate_latency(per_turn: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-turn timings into the call's latency stats.

    The headline budget (end-of-speech -> first audio) is computed over
    *non-overlapped* turns only. A turn where the caller stopped talking while
    the bot was still speaking is queued behind that playback, so including it
    would measure conversational overlap rather than pipeline latency. Both
    counts are reported so the exclusion is visible, not hidden.
    """
    key = "end_of_speech_to_first_audio_ms"
    clean = sorted(t[key] for t in per_turn if key in t and not t.get("overlapped"))
    overlapped = [t[key] for t in per_turn if key in t and t.get("overlapped")]

    out: dict[str, Any] = {
        "turns_measured": len(clean),
        "turns_overlapped_excluded": len(overlapped),
    }
    if clean:
        out["p50_end_to_first_audio_ms"] = _percentile(clean, 0.50)
        out["p95_end_to_first_audio_ms"] = _percentile(clean, 0.95)
        out["max_end_to_first_audio_ms"] = clean[-1]
        out["within_budget_pct"] = round(
            100.0 * sum(1 for v in clean if v <= 800) / len(clean), 1
        )
    if overlapped:
        out["p50_overlapped_ms"] = _percentile(sorted(overlapped), 0.50)

    # Per-stage medians, over every turn: these are stage costs, not budgets.
    for metric in ("stt_finalisation_ms", "llm_ttft_ms", "tts_ttfa_ms", "pipeline_ms", "queue_wait_ms"):
        vals = sorted(t[metric] for t in per_turn if metric in t)
        if vals:
            out[f"p50_{metric}"] = _percentile(vals, 0.50)
    return out


def _write_transcript(call_id: str, correlation_id: str, turns: list[dict]) -> str:
    """Persist the per-turn transcript JSON.

    ``file://`` in the PoC; in production this is the customer's S3 prefix and
    the platform's own copy is purged after upload (see docs/security.md).
    """
    from pathlib import Path

    root = settings.resolve("data/transcripts")
    root.mkdir(parents=True, exist_ok=True)
    path: Path = root / f"{call_id}.json"
    path.write_text(
        json.dumps({"call_id": call_id, "correlation_id": correlation_id, "turns": turns},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return f"file://{path.as_posix()}"


def _estimate_cost_paise(duration_s: float, bot_turns: int) -> int:
    """Per-call cost in paise, for the ROI figures on the dashboard.

    **The rates below are placeholders, not quoted Sarvam pricing.** This function
    is deliberately the single place they live — replace them with the real rate
    card before showing the numbers to a customer. See docs/cost.md for the
    derivation, what actually moves the number, and what it excludes.
    """
    minutes = max(duration_s, 1.0) / 60.0
    telephony = 30 * minutes            # ₹0.30/min  — telco contract
    stt = 25 * minutes                  # ₹0.25/min  — Saaras streaming
    tts = 15 * bot_turns                # ₹0.15/utterance — Bulbul
    llm = 40 * max(bot_turns, 1) / 4    # ₹0.10/utterance — sarvam-105b tokens
    return int(round(telephony + stt + tts + llm))
