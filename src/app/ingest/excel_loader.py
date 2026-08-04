"""Read an uploaded .xlsx call list.

The production feed is a CSV on SFTP, and :mod:`app.ingest.csv_loader` is the real
ingest path. This module exists because the *demo* starts from a spreadsheet on
someone's desktop, and asking a collections manager to export to CSV first is a
worse demo than accepting the file they already have.

It converts a worksheet to CSV text and hands it to the existing loader, so there
is exactly one implementation of validation, coercion and the consent/DND scrub.
Nothing about the compliance path is duplicated here.
"""

from __future__ import annotations

import csv
import io
import logging

logger = logging.getLogger("emi.ingest.excel")

# Worksheet names we will look for, in order, when the caller does not name one.
# A workbook exported from the earlier n8n build has four tabs and only the first
# is a call list.
PREFERRED_SHEETS = ("Borrowers", "Call List", "CallList", "Sheet1")


class ExcelIngestError(ValueError):
    """The workbook could not be read as a call list."""


def _cell_to_text(value: object) -> str:
    """Render a cell the way the CSV loader expects to receive it.

    Two cases matter. Excel stores dates as datetimes, and ``str()`` on those
    yields ``2026-07-05 00:00:00`` which ``date.fromisoformat`` rejects; the loader
    already slices to 10 characters, but only if the separator is a ``-``. And
    Excel stores whole numbers as floats, so an EMI of 8450 arrives as ``8450.0``
    and a DPD of 27 as ``27.0`` — the latter fails ``int()``.
    """
    if value is None:
        return ""
    if hasattr(value, "isoformat"):          # date / datetime
        return value.isoformat()[:10]
    if isinstance(value, bool):              # before the int check: bool is an int
        return "Y" if value else "N"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def pick_sheet(workbook, sheet: str | None = None) -> str:
    names = list(workbook.sheetnames)
    if sheet:
        if sheet not in names:
            raise ExcelIngestError(
                f"worksheet {sheet!r} not found. Available: {names}"
            )
        return sheet
    for candidate in PREFERRED_SHEETS:
        if candidate in names:
            return candidate
    if not names:
        raise ExcelIngestError("workbook has no worksheets")
    return names[0]


def xlsx_to_csv_text(data: bytes, *, sheet: str | None = None) -> tuple[str, str]:
    """Convert one worksheet to CSV text.

    Returns ``(csv_text, sheet_name)``. Blank trailing rows are dropped — a
    spreadsheet that has been edited by hand routinely carries a few, and each one
    would otherwise be counted and reported as an invalid row.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ExcelIngestError(
            "openpyxl is required to read .xlsx files: pip install openpyxl"
        ) from exc

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise ExcelIngestError(f"could not open workbook: {exc}") from exc

    try:
        name = pick_sheet(wb, sheet)
        ws = wb[name]

        rows: list[list[str]] = []
        for raw in ws.iter_rows(values_only=True):
            cells = [_cell_to_text(c) for c in raw]
            if any(c for c in cells):
                rows.append(cells)
    finally:
        wb.close()

    if not rows:
        raise ExcelIngestError(f"worksheet {name!r} is empty")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    logger.info("converted worksheet %r: %d rows including header", name, len(rows))
    return buf.getvalue(), name
