"""Call-list ingest: header aliasing, .xlsx reading, and rejection reporting.

These cover the consent/DND scrub path, so they are compliance tests rather than
convenience tests. A regression here means a non-consented borrower could enter the
callable universe, which is the one outcome this system exists to prevent.
"""

from __future__ import annotations

import io

import pytest

from app.ingest.csv_loader import canonical, load_csv
from app.ingest.excel_loader import ExcelIngestError, pick_sheet, xlsx_to_csv_text

openpyxl = pytest.importorskip("openpyxl")


# --- header aliasing ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("days_past_due", "dpd"),
    ("Days Past Due", "dpd"),
    ("DAYS-PAST-DUE", "dpd"),
    ("language", "lang"),
    ("language_code", "lang"),
    ("mobile", "phone"),
    ("account_no", "loan_id"),
    ("customer_name", "name"),
    ("emi", "emi_amount"),
    ("loan_id", "loan_id"),        # already canonical
    ("consent", "consent"),
])
def test_canonical_maps_real_world_headers(raw, expected):
    assert canonical(raw) == expected


def test_canonical_is_stable_for_unknown_headers():
    # An unrecognised column must pass through rather than collapse onto something
    # else, or an unrelated field could silently satisfy a required column.
    assert canonical("branch_code") == "branch_code"
    assert canonical("  Weird Column ") == "weird_column"


# --- the scrub still fails closed -------------------------------------------
HEADER = "loan_id,phone,language,name,emi_amount,due_date,days_past_due,consent,dnd"


def _csv(*rows: str) -> io.StringIO:
    return io.StringIO("\n".join((HEADER, *rows)) + "\n")


def test_aliased_headers_load_and_scrub(tmp_path):
    report = load_csv(_csv(
        "PL1,+919800000001,hi-IN,Consenting Borrower,4500,2026-07-05,25,Y,N",
        "PL2,+919800000002,en-IN,No Consent,4500,2026-07-05,25,N,N",
        "PL3,+919800000003,hi-IN,On DND,4500,2026-07-05,25,Y,Y",
    ), campaign_name="alias test")

    assert report.total_rows == 3
    assert report.loaded == 1
    assert report.skipped_no_consent == 1
    assert report.skipped_dnd == 1


def test_rejections_name_the_borrower_and_the_authority():
    report = load_csv(_csv(
        "PL9,+919800000009,hi-IN,No Consent,4500,2026-07-05,25,N,N",
    ), campaign_name="rejection detail")

    assert len(report.rejected) == 1
    r = report.rejected[0]
    assert r["loan_id"] == "PL9"
    assert r["name"] == "No Consent"
    assert "DPDP" in r["authority"]
    # The phone must be identifiable but not dialable from the report.
    assert r["phone"].endswith("0009")
    assert "9198" not in r["phone"]


def test_missing_required_column_is_rejected_not_guessed():
    # No consent column at all. Defaulting it would be the dangerous behaviour.
    bad = io.StringIO(
        "loan_id,phone,language,name,emi_amount,due_date,days_past_due\n"
        "PL1,+919800000001,hi-IN,Someone,4500,2026-07-05,25\n"
    )
    with pytest.raises(ValueError, match="missing required columns"):
        load_csv(bad, campaign_name="missing column")


# --- xlsx reading ------------------------------------------------------------
def _workbook(rows, sheet_name="Borrowers"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_converts_dates_and_integer_floats():
    import datetime as dt
    data = _workbook([
        ["loan_id", "phone", "language", "name", "emi_amount", "due_date", "days_past_due", "consent", "dnd"],
        ["PL1", "+919800000001", "hi-IN", "A Borrower", 4500.0, dt.datetime(2026, 7, 5), 25.0, True, False],
    ])
    text, sheet = xlsx_to_csv_text(data)
    assert sheet == "Borrowers"
    line = text.splitlines()[1]
    # 4500.0 must not reach the loader as "4500.0", nor 25.0 as "25.0" (int() fails),
    # nor the datetime as "2026-07-05 00:00:00" (date.fromisoformat fails).
    assert "4500," in line
    assert ",25," in line
    assert "2026-07-05," in line
    assert line.endswith("Y,N")          # booleans become the loader's truthy form

    report = load_csv(io.StringIO(text), campaign_name="xlsx roundtrip")
    assert report.loaded == 1


def test_xlsx_skips_blank_trailing_rows():
    data = _workbook([
        ["loan_id", "phone", "language", "name", "emi_amount", "due_date", "days_past_due", "consent", "dnd"],
        ["PL1", "+919800000001", "hi-IN", "A", 4500, "2026-07-05", 25, "Y", "N"],
        [None, None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None, None],
    ])
    text, _ = xlsx_to_csv_text(data)
    assert len(text.strip().splitlines()) == 2
    report = load_csv(io.StringIO(text), campaign_name="blank rows")
    assert report.total_rows == 1
    assert report.skipped_invalid == 0


def test_sheet_selection_prefers_borrowers_then_first():
    wb = openpyxl.Workbook()
    wb.active.title = "Summary"
    wb.create_sheet("Borrowers")
    assert pick_sheet(wb) == "Borrowers"

    wb2 = openpyxl.Workbook()
    wb2.active.title = "Whatever"
    assert pick_sheet(wb2) == "Whatever"

    with pytest.raises(ExcelIngestError, match="not found"):
        pick_sheet(wb2, "Nope")


def test_unreadable_workbook_raises_excel_error():
    with pytest.raises(ExcelIngestError):
        xlsx_to_csv_text(b"this is not a workbook")
