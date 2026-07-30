"""Call-list ingestion tests: the header contract and the compliance scrub."""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import Borrower
from app.ingest.csv_loader import load_csv

HEADER = "loan_id,phone,lang,name,emi_amount,due_date,dpd,product,outstanding,consent,dnd"


def csv_of(*rows: str) -> io.StringIO:
    return io.StringIO("\n".join([HEADER, *rows]) + "\n")


class TestHeaderContract:
    def test_missing_columns_fail_loudly(self):
        """Agree the header up front; a silently-wrong feed is worse than a crash."""
        bad = io.StringIO("loan_id,phone\nPL1,+919800000001\n")
        with pytest.raises(ValueError, match="missing required columns"):
            load_csv(bad)

    def test_accepts_various_amount_formats(self):
        report = load_csv(csv_of(
            "PLA1,+919800000101,hi-IN,A,4500,2026-07-05,20,PERSONAL_LOAN,54000,Y,N",
            "PLA2,+919800000102,hi-IN,B,\"7,800\",2026-07-04,26,PERSONAL_LOAN,,Y,N",
            "PLA3,+919800000103,hi-IN,C,12500.00,2026-07-10,20,HOME_LOAN,,yes,no",
        ))
        assert report.loaded == 3
        with session_scope() as s:
            amounts = {
                b.loan_id: b.emi_amount_paise
                for b in s.scalars(select(Borrower).where(Borrower.loan_id.in_(["PLA1", "PLA2", "PLA3"]))).all()
            }
        assert amounts == {"PLA1": 450000, "PLA2": 780000, "PLA3": 1250000}


class TestComplianceScrub:
    def test_no_consent_rows_are_dropped(self):
        """DPDP Act 2023 — consent is a precondition for the call, not a field."""
        report = load_csv(csv_of(
            "PLB1,+919800000201,hi-IN,Yes,4500,2026-07-05,20,PERSONAL_LOAN,,Y,N",
            "PLB2,+919800000202,hi-IN,No,4500,2026-07-05,20,PERSONAL_LOAN,,N,N",
        ))
        assert report.loaded == 1
        assert report.skipped_no_consent == 1
        with session_scope() as s:
            assert s.get(Borrower, "PLB2") is None, "a no-consent row must never be loaded"

    def test_dnd_rows_are_dropped(self):
        report = load_csv(csv_of(
            "PLC1,+919800000301,hi-IN,Clean,4500,2026-07-05,20,PERSONAL_LOAN,,Y,N",
            "PLC2,+919800000302,hi-IN,Dnd,4500,2026-07-05,20,PERSONAL_LOAN,,Y,Y",
        ))
        assert report.skipped_dnd == 1
        with session_scope() as s:
            assert s.get(Borrower, "PLC2") is None

    def test_invalid_rows_are_counted_not_fatal(self):
        """One bad row must not abort a 20,000-row feed."""
        report = load_csv(csv_of(
            "PLD1,+919800000401,hi-IN,Good,4500,2026-07-05,20,PERSONAL_LOAN,,Y,N",
            "PLD2,+919800000402,hi-IN,NoAmount,,2026-07-05,20,PERSONAL_LOAN,,Y,N",
            "PLD3,+919800000403,hi-IN,BadDate,4500,not-a-date,20,PERSONAL_LOAN,,Y,N",
            "PLD4,+919800000404,hi-IN,AlsoGood,3300,2026-07-06,10,PERSONAL_LOAN,,Y,N",
        ))
        assert report.loaded == 2
        assert report.skipped_invalid == 2
        assert len(report.errors) == 2


class TestUpsert:
    def test_reingest_updates_rather_than_duplicates(self):
        """The feed arrives daily; DPD changes, the row must not multiply."""
        load_csv(csv_of("PLE1,+919800000501,hi-IN,Ramesh,4500,2026-07-05,20,PERSONAL_LOAN,,Y,N"))
        load_csv(csv_of("PLE1,+919800000501,hi-IN,Ramesh,4500,2026-07-05,27,PERSONAL_LOAN,,Y,N"))
        with session_scope() as s:
            rows = s.scalars(select(Borrower).where(Borrower.loan_id == "PLE1")).all()
        assert len(rows) == 1
        assert rows[0].dpd == 27, "DPD should reflect the latest feed"

    def test_attempt_counter_resets_on_reingest(self):
        load_csv(csv_of("PLF1,+919800000601,hi-IN,Sita,4500,2026-07-05,20,PERSONAL_LOAN,,Y,N"))
        with session_scope() as s:
            s.get(Borrower, "PLF1").attempts_today = 3
        load_csv(csv_of("PLF1,+919800000601,hi-IN,Sita,4500,2026-07-05,21,PERSONAL_LOAN,,Y,N"))
        with session_scope() as s:
            assert s.get(Borrower, "PLF1").attempts_today == 0
