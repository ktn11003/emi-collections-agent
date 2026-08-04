"""FastAPI application entry point.

Run:  uvicorn app.main:app --app-dir src --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import REPO_ROOT, settings
from app.db.base import create_all, db_flavour
from app.routers import api, ws_voice
from app.sarvam.client import close_http
from app.telephony import twilio_media
from app.steplog import configure_logging, step

logger = logging.getLogger("emi.main")
WEB_DIR = REPO_ROOT / "src" / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Idempotent: `alembic upgrade head` is the production path, but creating the
    # schema here means `uvicorn` alone is enough to get a working demo.
    create_all()
    logger.info(
        "EMI collections agent up | db=%s | sarvam=%s | tts=%s | models=%s/%s/%s",
        db_flavour(),
        "live" if not settings.offline_mode else "OFFLINE MOCK (no SARVAM_API_KEY)",
        settings.tts_transport,
        settings.stt_model, settings.llm_model, settings.tts_model,
    )
    if settings.offline_mode:
        logger.warning(
            "SARVAM_API_KEY is not set - serving canned responses. "
            "Copy .env.example to .env and add your key for the real models."
        )
    step("call.started", None, boot=True, db=db_flavour(),
         offline=settings.offline_mode)
    try:
        yield
    finally:
        await close_http()


app = FastAPI(
    title="Generic Collections Agent Bot",
    description=(
        "Multilingual outbound collections voice agent on Sarvam AI "
        "(Saaras v3 STT / sarvam-105b / Bulbul v3 TTS / Mayura translate) "
        "with an agentic orchestrator and a post-call analytics pipeline."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(api.router)
app.include_router(ws_voice.router)

# Real-telephony bridge. Always mounted (harmless without a carrier); a call only
# reaches it when a provider's webhook points at /telephony/twilio/voice.
app.include_router(twilio_media.router)


# The demo front end is edited during a session and a stale app.js silently shows
# the wrong UI, which is far more confusing than an extra request.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "dashboard.html", headers=NO_CACHE)


@app.get("/static/{filename}", include_in_schema=False)
def static_asset(filename: str) -> FileResponse:
    """Serve app.js / styles.css with caching disabled.

    Declared before the StaticFiles mount so it wins, and path-restricted to the
    two known assets so a filename cannot traverse out of the web directory.
    """
    if filename not in {"app.js", "styles.css"}:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(WEB_DIR / filename, headers=NO_CACHE)


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    logger.exception("unhandled error")
    step("error", None, source="http", detail=str(exc))
    return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=500)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
