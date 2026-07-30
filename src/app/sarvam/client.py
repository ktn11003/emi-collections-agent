"""Shared HTTP plumbing for every Sarvam API call.

Verified against the live API on 2026-07-30:
* base URL ``https://api.sarvam.ai``
* auth header ``api-subscription-key`` (auth failures return **403**, not 401)
* errors come back as ``{"error": {"message", "code", "request_id"}}``

``request_id`` is captured on every call and logged — it is what Sarvam support
asks for, and it is what correlates a bad turn to a vendor-side trace.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("emi.sarvam")

# Codes worth retrying: transient capacity and rate limits.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class SarvamError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id

    def __str__(self) -> str:  # pragma: no cover - display only
        bits = [super().__str__()]
        if self.code:
            bits.append(f"code={self.code}")
        if self.status:
            bits.append(f"status={self.status}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " ".join(bits)


def auth_headers() -> dict[str, str]:
    return {"api-subscription-key": settings.sarvam_api_key}


def ws_url(path: str, params: dict[str, Any] | None = None) -> str:
    base = settings.sarvam_base_url.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base.rstrip('/')}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = f"{url}?{qs}"
    return url


def _raise_for_payload(payload: Any, status: int) -> None:
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"] or {}
        if isinstance(err, dict):
            raise SarvamError(
                err.get("message", "Sarvam API error"),
                status=status, code=err.get("code"), request_id=err.get("request_id"),
            )
        raise SarvamError(str(err), status=status)


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    """Process-wide client so connections (and TLS handshakes) are reused."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.sarvam_base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            headers=auth_headers(),
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST JSON with bounded retries on transient failures."""
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = await http().post(path, json=body)
            payload = r.json() if r.content else {}
            if r.status_code in RETRYABLE_STATUS:
                raise SarvamError(f"transient {r.status_code}", status=r.status_code)
            _raise_for_payload(payload, r.status_code)
            r.raise_for_status()
            return payload
        except (SarvamError, httpx.HTTPError) as exc:
            retryable = isinstance(exc, httpx.TransportError) or (
                isinstance(exc, SarvamError) and exc.status in RETRYABLE_STATUS
            )
            last = exc
            if not retryable or attempt == MAX_ATTEMPTS:
                break
            backoff = 0.25 * (2 ** (attempt - 1))
            logger.warning("POST %s attempt %d failed (%s); retrying in %.2fs", path, attempt, exc, backoff)
            await asyncio.sleep(backoff)
    raise SarvamError(f"POST {path} failed: {last}") from last


async def post_multipart(
    path: str, *, files: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    """POST multipart/form-data (used by the speech-to-text endpoint)."""
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = await http().post(path, files=files, data=data)
            payload = r.json() if r.content else {}
            if r.status_code in RETRYABLE_STATUS:
                raise SarvamError(f"transient {r.status_code}", status=r.status_code)
            _raise_for_payload(payload, r.status_code)
            r.raise_for_status()
            return payload
        except (SarvamError, httpx.HTTPError) as exc:
            retryable = isinstance(exc, httpx.TransportError) or (
                isinstance(exc, SarvamError) and exc.status in RETRYABLE_STATUS
            )
            last = exc
            if not retryable or attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
    raise SarvamError(f"POST {path} failed: {last}") from last
