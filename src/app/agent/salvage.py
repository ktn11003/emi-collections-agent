"""Recover tool calls that the model emitted as plain text.

sarvam-105b usually returns function calls in the OpenAI ``tool_calls`` field,
but intermittently it writes the call into ``content`` instead — observed live::

    [{"name": "schedule_ptp", "arguments": {"loan_id": "PL0098",
      "promised_date": "2026-08-08", "amount": 4500}}]

Two things then go wrong if this is not handled:

1. the JSON is passed to TTS and **read aloud to the borrower**, and
2. the side effect never happens, so a captured promise-to-pay is silently lost
   and the call is dispositioned INCOMPLETE.

So every drafted sentence is checked before it reaches TTS. If it looks like a
tool call it is withheld from speech, buffered, and parsed at the end of the
turn; anything recovered is dispatched through the normal idempotent executor.

This is defence in depth, not a substitute for the ``tool_calls`` field — when
the model behaves, this module never fires.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.agent.tools import TOOL_NAMES

logger = logging.getLogger("emi.salvage")

# Argument keys used across our tool schemas, for fuzzy matching.
_KNOWN_KEYS = {
    "loan_id", "amount", "channel", "promised_date", "reason", "context",
    "disposition", "notes", "callback_at", "name", "arguments",
}

_JSON_HINT = re.compile(
    r"(\{[^{}]*[\"']\s*(?:" + "|".join(sorted(_KNOWN_KEYS)) + r")\s*[\"']\s*:)"
    r"|(^\s*[\[{])"
    r"|(\b(?:" + "|".join(TOOL_NAMES) + r")\s*\()"
    # XML-style call blocks. sarvam-105b intermittently abandons the OpenAI
    # tool_calls field and emits an Anthropic-style `<function_calls>` wrapper as
    # plain content — observed live, spoken to the borrower as "<functioncalls".
    # The opening tag arrives as its own sentence, ahead of any JSON, so the
    # brace-based checks above see nothing to catch.
    r"|(<\s*/?\s*(?:function|tool|invoke|antml|parameter)\w*)",
    re.IGNORECASE,
)

# Normalise keys the model may mangle (it sometimes drops the underscore).
# Bare enum *values*. The model sometimes emits a tool call one token at a time,
# and a fragment like '"WRONGNUMBER",' carries no brace and no key, so it slipped
# past every structural check and was read to the borrower — observed live on a
# wrong-number call, twice in one conversation. These are never natural speech in
# any language, quoted or not, with or without a trailing comma.
_ENUM_VALUES = {
    # mark_disposition
    "PTP", "PAID", "LINK_SENT", "DISPUTE", "WRONG_NUMBER", "NO_ANSWER",
    "CALLBACK", "REFUSED", "ESCALATED", "INCOMPLETE",
    # escalate_to_human
    "DISTRESS", "REQUESTED_HUMAN", "HARDSHIP", "GRIEVANCE", "COMPLEX_QUERY",
    # send_payment_link
    "WHATSAPP", "SMS",
}
# Matched with separators stripped, so "WRONG_NUMBER", "WRONGNUMBER" and
# "wrong number" all collapse to the same token.
_ENUM_TOKENS = {v.replace("_", "").upper() for v in _ENUM_VALUES} | {
    n.replace("_", "").upper() for n in TOOL_NAMES
}

_KEY_ALIASES = {
    "loanid": "loan_id", "loan": "loan_id", "account": "loan_id",
    "promiseddate": "promised_date", "ptpdate": "promised_date", "date": "promised_date",
    "callbackat": "callback_at", "amountrupees": "amount", "amt": "amount",
    "toolname": "name", "function": "name", "args": "arguments", "params": "arguments",
}


@dataclass(slots=True)
class SalvagedCall:
    id: str
    name: str
    arguments: dict[str, Any]


def looks_like_tool_call(text: str) -> bool:
    """Cheap pre-TTS check: would speaking this read JSON to the borrower?

    Checked on the *raw* sentence, before markdown stripping — stripping removes
    underscores and quotes, which would destroy the evidence.
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    # Bare structural fragments left by splitting a JSON blob, e.g. '"PL0098", {'
    # or '}]'. Checked before any length guard: these are short by nature and are
    # never speech.
    if re.fullmatch(r"[\s\[\]{}(),:\"']+", stripped):
        return True
    # A lone enum value or tool name, however it was punctuated. Deliberately
    # before the length guard and before _JSON_HINT: these fragments are short
    # and carry no JSON syntax at all.
    bare = re.sub(r"[^A-Za-z]", "", stripped).upper()
    if bare and bare in _ENUM_TOKENS and len(re.findall(r"[A-Za-z]+", stripped)) <= 2:
        return True
    if len(stripped) < 4:
        return False
    if _JSON_HINT.search(stripped):
        return True
    # Any XML/HTML-ish tag. sarvam-105b invents new wrappers for text-mode tool
    # calls -- <function_calls>, then <arg_key>loan_id</arg_key> -- and naming
    # them one at a time is a losing game. Nothing Bulbul is ever asked to speak,
    # in any supported language, contains an angle bracket followed by a letter
    # or a slash, so the shape is the reliable signal.
    if re.search(r"<\s*/?\s*[A-Za-z_]", stripped):
        return True

    # A quoted key followed by a colon is never natural speech.
    return bool(re.search(r"[\"']\w+[\"']\s*:", stripped))


def _normalise_keys(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        flat = re.sub(r"[^a-z0-9]", "", str(key).lower())
        out[_KEY_ALIASES.get(flat, key if key in _KNOWN_KEYS else _KEY_ALIASES.get(flat, key))] = value
    return out


def _candidates(blob: str) -> list[dict[str, Any]]:
    """Pull JSON objects out of a possibly-truncated, possibly-fenced blob."""
    text = re.sub(r"```(?:json)?", " ", blob)
    found: list[dict[str, Any]] = []

    # Whole-blob parse first (handles a clean list or object).
    for start, end in ((text.find("["), text.rfind("]")), (text.find("{"), text.rfind("}"))):
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.append(parsed)
            elif isinstance(parsed, list):
                found.extend(p for p in parsed if isinstance(p, dict))
            if found:
                return found

    # Fall back to scanning for balanced objects, so one bad object does not
    # discard the rest.
    depth = 0
    begin = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and begin >= 0:
                try:
                    obj = json.loads(text[begin : i + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(obj, dict):
                        found.append(obj)
                begin = -1
    return found


def salvage(blob: str, *, default_loan_id: str | None = None) -> list[SalvagedCall]:
    """Extract dispatchable tool calls from text the model spoke by mistake."""
    if not blob.strip():
        return []

    calls: list[SalvagedCall] = []
    for raw in _candidates(blob):
        obj = _normalise_keys(raw)

        # Shape A: {"name": "schedule_ptp", "arguments": {...}}
        name = obj.get("name")
        args = obj.get("arguments")
        if isinstance(name, str) and name in TOOL_NAMES:
            arguments = _normalise_keys(args) if isinstance(args, dict) else {
                k: v for k, v in obj.items() if k not in ("name", "arguments")
            }
        # Shape B: bare arguments, tool inferred from the keys present.
        elif (inferred := _infer_tool(obj)) is not None:
            name, arguments = inferred, obj
        else:
            continue

        if default_loan_id and not arguments.get("loan_id"):
            arguments["loan_id"] = default_loan_id
        if not arguments.get("loan_id"):
            continue   # not dispatchable without an account

        calls.append(SalvagedCall(id=f"salvaged_{len(calls)}", name=name, arguments=arguments))

    if calls:
        logger.warning(
            "salvaged %d tool call(s) the model emitted as text: %s",
            len(calls), [c.name for c in calls],
        )
    return calls


def _infer_tool(obj: dict[str, Any]) -> str | None:
    """Guess the tool from the argument keys, when no name was given."""
    keys = set(obj)
    if "promised_date" in keys:
        return "schedule_ptp"
    if "callback_at" in keys:
        return "schedule_callback"
    if "disposition" in keys:
        return "mark_disposition"
    if "reason" in keys and "loan_id" in keys:
        return "escalate_to_human"
    if "amount" in keys and "loan_id" in keys:
        return "send_payment_link"
    return None
