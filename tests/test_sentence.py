"""Sentence-aggregation tests — the latency-critical path.

If the aggregator holds a sentence too long, TTS starts late and the caller hears
dead air. If it cuts too early, prosody breaks and amounts get mangled
("Rs." / "4,500").
"""

from __future__ import annotations

import pytest

from app.agent.sentence import (
    MAX_CLAUSE_CHARS,
    SentenceAggregator,
    split_sentences,
    strip_for_speech,
)


def feed(tokens: list[str]) -> list[str]:
    agg = SentenceAggregator()
    out: list[str] = []
    for t in tokens:
        out.extend(agg.push(t))
    tail = agg.flush()
    if tail:
        out.append(tail)
    return out


class TestBoundaries:
    def test_emits_on_full_stop(self):
        out = feed(["Namaste", " Rahul", " ji", ".", " Aapki", " EMI", " pending", " hai", "."])
        assert out == ["Namaste Rahul ji.", "Aapki EMI pending hai."]

    def test_devanagari_danda(self):
        """A Hindi sentence ends with '।', not '.'."""
        assert split_sentences("यह एक वाक्य है। यह दूसरा है।") == [
            "यह एक वाक्य है।", "यह दूसरा है।"
        ]

    def test_question_and_exclamation(self):
        out = split_sentences("Kya aap payment karenge? Bahut accha! Dhanyavaad.")
        assert out == ["Kya aap payment karenge?", "Bahut accha!", "Dhanyavaad."]

    def test_first_sentence_available_before_stream_ends(self):
        """The whole point: sentence 1 must be speakable while 2 is still arriving."""
        agg = SentenceAggregator()
        emitted = agg.push("Aapki EMI 4,500 rupees hai. ")
        assert emitted == ["Aapki EMI 4,500 rupees hai."]
        assert agg.pending.strip() == ""


class TestNoBadSplits:
    def test_does_not_split_indian_grouped_amount(self):
        """'4,500.' must not become 'Rs.' + '4,500.'"""
        out = split_sentences("Aapki EMI Rs. 4,500 hai aur due date 5 July thi.")
        assert len(out) == 1

    def test_does_not_split_on_abbreviation(self):
        out = split_sentences("Mr. Rahul ji se baat karni hai abhi turant.")
        assert len(out) == 1

    def test_does_not_split_decimals(self):
        out = split_sentences("Interest rate 10.5 percent hai is loan par.")
        assert len(out) == 1

    def test_short_fragment_is_buffered_not_emitted(self):
        """One-word fragments make TTS prosody choppy, so they wait."""
        agg = SentenceAggregator()
        assert agg.push("Ok.") == []
        assert "Ok." in agg.pending


class TestLongClause:
    def test_unpunctuated_clause_is_cut_at_a_word_boundary(self):
        """A model that forgets punctuation must not leave the caller waiting."""
        text = "word " * 60
        out = feed([text])
        assert len(out) > 1
        assert all(len(s) <= MAX_CLAUSE_CHARS + 10 for s in out)
        # Cut on whitespace, never mid-word.
        assert not any(s.endswith("wor") for s in out)


class TestStripForSpeech:
    @pytest.mark.parametrize("dirty,clean", [
        ("**Namaste** Rahul", "Namaste Rahul"),
        ("- Aapki EMI pending hai", "Aapki EMI pending hai"),
        ("## Heading text", "Heading text"),
        ("Dhanyavaad 🙏", "Dhanyavaad"),
        ("Line one\n\nLine two", "Line one Line two"),
        ("1. First item", "First item"),
    ])
    def test_removes_unspeakable_markup(self, dirty, clean):
        """Models leak markdown even when told not to; TTS would read it aloud."""
        assert strip_for_speech(dirty) == clean

    def test_preserves_devanagari_and_punctuation(self):
        text = "आपकी EMI ₹4,500 थी। क्या आप आज payment करेंगे?"
        assert strip_for_speech(text) == text
