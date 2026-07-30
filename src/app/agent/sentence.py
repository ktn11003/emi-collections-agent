"""Sentence aggregation — the single biggest latency win in the pipeline.

The LLM streams tokens; TTS wants sentences. If you wait for the whole reply
before synthesising, the caller hears a full sentence of dead air. Instead,
accumulate tokens until a sentence boundary, hand *that* to TTS, and let the LLM
keep writing the next one. A ~2.5 s "sum of stages" becomes a ~0.8 s felt
latency.

Boundaries include the Devanagari danda (``।``) and the Urdu full stop (``۔``),
not just ASCII punctuation.
"""

from __future__ import annotations

import re

TERMINATORS = ".?!।۔;:\n"

# Don't split inside an abbreviation, a decimal, or an Indian-grouped amount
# ("Rs. 4,500." must not become "Rs." + "4,500.").
_ABBREV = re.compile(r"(?:^|\s)(?:Rs|Mr|Mrs|Ms|Dr|Sr|Jr|St|No|vs|etc|i\.e|e\.g)\.$", re.IGNORECASE)
_DECIMAL_TAIL = re.compile(r"\d[.,]$")

# A clause this long is emitted even without punctuation, so the caller is never
# left waiting on a model that forgets to punctuate.
MAX_CLAUSE_CHARS = 160
# Below this, keep buffering: one-word fragments make TTS prosody choppy.
MIN_SENTENCE_CHARS = 12


class SentenceAggregator:
    """Feed it token deltas; it yields speakable sentences."""

    def __init__(self, *, min_chars: int = MIN_SENTENCE_CHARS, max_chars: int = MAX_CLAUSE_CHARS) -> None:
        self._buf = ""
        self.min_chars = min_chars
        self.max_chars = max_chars

    def push(self, token: str) -> list[str]:
        """Add a token, return any sentences that just became complete."""
        if not token:
            return []
        self._buf += token
        out: list[str] = []

        while True:
            cut = self._find_cut()
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def _find_cut(self) -> int | None:
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch not in TERMINATORS:
                continue
            head = buf[: i + 1]
            if len(head.strip()) < self.min_chars:
                continue
            if _ABBREV.search(head) or _DECIMAL_TAIL.search(head):
                continue
            return i + 1

        # No punctuation but the clause is long: cut at the last word boundary.
        if len(buf) >= self.max_chars:
            space = buf.rfind(" ", 0, self.max_chars)
            return space + 1 if space > self.min_chars else self.max_chars
        return None

    def flush(self) -> str | None:
        """Emit whatever is left (call this when the LLM stream ends)."""
        rest, self._buf = self._buf.strip(), ""
        return rest or None

    @property
    def pending(self) -> str:
        return self._buf


def split_sentences(text: str) -> list[str]:
    """Split a complete string the same way the streaming aggregator would."""
    agg = SentenceAggregator()
    out = agg.push(text)
    tail = agg.flush()
    if tail:
        out.append(tail)
    return out


def strip_for_speech(text: str) -> str:
    """Remove anything that should never reach TTS.

    Models leak markdown, list markers and emoji even when told not to; a voice
    channel has no way to render them, so they get read out loud as noise.
    """
    text = re.sub(r"[*_`#>]+", "", text)
    # Numbered ("1.", "2)") and bulleted ("-", "•", "*") list markers alike.
    text = re.sub(r"^[ \t]*(?:\d+[.)]|[-–—•])[ \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()
