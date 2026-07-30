"""sarvam-105b chat completions — streaming, with function calling.

The endpoint is OpenAI-compatible (``POST /v1/chat/completions``), with one
Sarvam-specific wrinkle that matters enormously for voice:

**sarvam-105b is a reasoning model.** By default (``reasoning_effort="medium"``)
it emits ``reasoning_content`` before any user-visible ``content`` — verified on
the live API, where a 10-token budget was consumed entirely by reasoning and
returned ``content: null``. In a voice turn that is dead air. So the agent sets
``reasoning_effort=None``, which streams answer tokens immediately.

Keep it in reasoning mode only for offline work (analytics, QA scoring) where a
few extra seconds buy better judgement.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from app.config import settings
from app.sarvam.client import SarvamError, http, post_json

logger = logging.getLogger("emi.llm")

# Reasoning tokens are billed against `max_tokens`, and the model emits them
# BEFORE any user-visible content. Measured on the live API: a trivial
# JSON-extraction prompt spends ~900-1,000 completion tokens reasoning even at
# `reasoning_effort="low"`. With max_tokens=800 the response comes back
# `finish_reason: "length"` and `content: ""` -- reasoning consumed the whole
# budget. So any call that leaves reasoning enabled gets a floor.
REASONING_TOKEN_FLOOR = 3000


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Delta:
    """One streamed increment: text, a completed tool call, or the end."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


def _accumulate_tool_calls(buf: dict[int, dict], deltas: list[dict]) -> None:
    """OpenAI-style streamed tool calls arrive as indexed argument fragments."""
    for tc in deltas or []:
        idx = tc.get("index", 0)
        slot = buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


def _finalise_tool_calls(buf: dict[int, dict]) -> list[ToolCall]:
    out: list[ToolCall] = []
    for idx in sorted(buf):
        slot = buf[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
        except json.JSONDecodeError:
            logger.warning("tool call %s had unparseable arguments: %r", slot["name"], slot["arguments"])
            args = {}
        out.append(ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=args))
    return out


async def stream_chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None | object = "__default__",
) -> AsyncIterator[Delta]:
    """Stream a completion. Yields text deltas, then any tool calls, then finish."""
    if settings.offline_mode:
        from app.sarvam import mock
        async for d in mock.stream_chat(messages, tools=tools):
            yield d
        return

    body: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
    }
    # `None` is meaningful (disables reasoning), so a sentinel marks "unset".
    body["reasoning_effort"] = (
        settings.reasoning_effort if reasoning_effort == "__default__" else reasoning_effort
    )
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    tool_buf: dict[int, dict] = {}
    finish: str | None = None

    try:
        async with http().stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise SarvamError(f"chat stream failed: {detail}", status=resp.status_code)

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    if delta.get("tool_calls"):
                        _accumulate_tool_calls(tool_buf, delta["tool_calls"])
                    reasoning = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""
                    if content or reasoning:
                        yield Delta(content=content, reasoning=reasoning)
    except httpx.HTTPError as exc:
        raise SarvamError(f"chat stream transport error: {exc}") from exc

    calls = _finalise_tool_calls(tool_buf)
    if calls or finish:
        yield Delta(tool_calls=calls, finish_reason=finish)


async def complete(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    max_tokens: int = REASONING_TOKEN_FLOOR,
    reasoning_effort: str | None = "low",
    response_format: dict | None = None,
) -> str:
    """Non-streaming completion, for offline work (analytics, QA, guardrails).

    Reasoning is left ON here by default — accuracy matters more than latency,
    the opposite trade-off from the live voice loop. See REASONING_TOKEN_FLOOR
    for why the token budget must be generous when it is enabled.
    """
    if settings.offline_mode:
        from app.sarvam import mock
        return mock.complete(messages)

    if reasoning_effort is not None and max_tokens < REASONING_TOKEN_FLOOR:
        logger.debug(
            "raising max_tokens %d -> %d: reasoning is enabled and would consume "
            "the whole budget before emitting content",
            max_tokens, REASONING_TOKEN_FLOOR,
        )
        max_tokens = REASONING_TOKEN_FLOOR

    body: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
    }
    if response_format:
        body["response_format"] = response_format

    payload = await post_json("/v1/chat/completions", body)
    choices = payload.get("choices") or []
    if not choices:
        return ""

    choice = choices[0]
    content = (choice.get("message", {}).get("content") or "").strip()
    if not content and choice.get("finish_reason") == "length":
        # Truncated mid-reasoning. Retry once without reasoning rather than
        # returning an empty analysis and silently losing the call's report.
        logger.warning(
            "completion truncated during reasoning (%d tokens); retrying with "
            "reasoning disabled", max_tokens,
        )
        body["reasoning_effort"] = None
        payload = await post_json("/v1/chat/completions", body)
        choices = payload.get("choices") or []
        if choices:
            content = (choices[0].get("message", {}).get("content") or "").strip()
    return content


async def complete_json(
    messages: list[dict[str, Any]], *, max_tokens: int = REASONING_TOKEN_FLOOR
) -> dict:
    """Completion constrained to a JSON object (Sarvam supports json_object mode)."""
    raw = await complete(
        messages,
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Models occasionally fence the object; recover the outermost braces.
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("could not parse JSON response: %r", raw[:300])
        return {}
