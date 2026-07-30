"""Test fixtures. Every test runs against a throwaway SQLite file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Point at a scratch DB and force offline mode *before* app.config is imported.
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{(ROOT / 'data' / 'test.db').as_posix()}"
os.environ["SARVAM_API_KEY"] = ""
os.environ["ENFORCE_CALLING_WINDOW"] = "true"


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    from app.db.base import engine
    from app.db.models import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    from app.db.base import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def borrower():
    """A seeded borrower, returned detached.

    Deliberately does NOT hold the session open: SQLite allows one writer, so a
    fixture that keeps a write transaction alive makes every later insert fail
    with "database is locked". ``expire_on_commit=False`` keeps the attributes
    readable after the session closes.
    """
    from datetime import date

    from app.db.base import session_scope
    from app.db.repo import upsert_borrower

    with session_scope() as s:
        return upsert_borrower(
            s,
            loan_id="PLTEST1",
            name="Test Borrower",
            phone="+919800000099",
            language="hi-IN",
            emi_amount_paise=450000,
            due_date=date(2026, 7, 5),
            dpd=20,
            product="PERSONAL_LOAN",
            consent=True,
            dnd_registered=False,
            attempts_today=0,
        )
