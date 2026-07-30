"""Typed application settings, loaded from the environment / .env file."""

from __future__ import annotations

from datetime import time as dtime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Sarvam -------------------------------------------------------------
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    stt_model: str = "saaras:v3"
    tts_model: str = "bulbul:v3"
    llm_model: str = "sarvam-105b"
    translate_model: str = "mayura:v1"

    # --- database -----------------------------------------------------------
    database_url: str = "sqlite+pysqlite:///./data/emi_agent.db"

    # --- voice loop ---------------------------------------------------------
    tts_transport: Literal["ws", "rest"] = "ws"
    llm_reasoning_effort: str = "null"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 300
    barge_in_min_speech_ms: int = 280
    stt_min_language_confidence: float = 0.55
    # Saaras emits several finals for one spoken utterance. Wait this long for a
    # follow-on fragment before answering, so the bot replies to the whole
    # sentence. Too high adds latency; too low and it answers half a thought.
    stt_coalesce_ms: int = 400

    # --- compliance ---------------------------------------------------------
    calling_window_start: dtime = dtime(8, 0)
    calling_window_end: dtime = dtime(19, 0)
    calling_window_tz: str = "Asia/Kolkata"
    enforce_calling_window: bool = True
    redact_pii_in_logs: bool = True

    # --- downstream (mocked in the PoC) -------------------------------------
    payment_gateway_base: str = "mock://payments"
    crm_base: str = "mock://lms"
    whatsapp_base: str = "mock://whatsapp"
    recording_sink: str = "file://./data/recordings"

    # --- telephony (optional) ----------------------------------------------
    telephony_provider: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    public_base_url: str = ""

    # --- server -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    step_log_path: Path = Field(default=Path("./logs/steps.jsonl"))

    @field_validator("calling_window_start", "calling_window_end", mode="before")
    @classmethod
    def _parse_hhmm(cls, v: object) -> object:
        if isinstance(v, str) and ":" in v:
            h, m = v.split(":", 1)
            return dtime(int(h), int(m))
        return v

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def _normalise_effort(cls, v: object) -> str:
        s = str(v).strip().lower()
        return "null" if s in ("", "none", "null", "off", "false") else s

    @property
    def reasoning_effort(self) -> str | None:
        """`None` disables sarvam-105b's reasoning pass (needed for voice TTFT)."""
        return None if self.llm_reasoning_effort == "null" else self.llm_reasoning_effort

    @property
    def offline_mode(self) -> bool:
        """No key configured -> serve deterministic canned responses.

        Lets the repo run (and the tests pass) without credentials. Every
        Sarvam call site checks this and falls back to app.sarvam.mock.
        """
        return not self.sarvam_api_key or self.sarvam_api_key.startswith("sk_xxxx")

    def resolve(self, path_like: str | Path) -> Path:
        """Resolve a config-relative path against the repo root."""
        p = Path(path_like)
        return p if p.is_absolute() else (REPO_ROOT / p)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
