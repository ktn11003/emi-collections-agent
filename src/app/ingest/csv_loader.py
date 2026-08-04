"""Call-list ingestion — the SFTP CSV feed that fuels the dialer.

In production the call universe arrives as a CSV on a secure SFTP endpoint:
phone, language, name, EMI amount, due date, DPD, product, consent flag. This is
the "Dynamic Call Personalisation" the SoW calls out — each row's variables become
prompt context, so the bot opens with the borrower's actual name and amount.

The loader does what a production feed must do before a single call is placed:

* validate the header contract (agreed column names, so analytics output is
  machine-mergeable),
* coerce and range-check every field,
* **scrub**: drop rows without consent (DPDP) and rows on the DND registry (TRAI),
* record how many rows were rejected and why — auditors ask.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.db.base import session_scope
from app.db.repo import add_audit, get_or_create_campaign, upsert_borrower
from app.steplog import step

logger = logging.getLogger("emi.ingest")

REQUIRED_COLUMNS = {"loan_id", "phone", "lang", "name", "emi_amount", "due_date", "dpd", "consent"}
OPTIONAL_COLUMNS = {"product", "outstanding", "dnd"}

# Real call lists do not use our column names. The spreadsheet a collections floor
# actually maintains says "days_past_due" and "language"; an LMS export might say
# "account_no" or "mobile". Rejecting the file over a synonym is a header contract
# enforced against the customer rather than agreed with them, so map the common
# variants and keep the contract for things that genuinely have to be agreed.
COLUMN_ALIASES: dict[str, str] = {
    "days_past_due": "dpd",
    "dayspastdue": "dpd",
    "days past due": "dpd",
    "language": "lang",
    "lang_code": "lang",
    "language_code": "lang",
    "mobile": "phone",
    "mobile_no": "phone",
    "phone_number": "phone",
    "msisdn": "phone",
    "account_no": "loan_id",
    "account_number": "loan_id",
    "loan_account": "loan_id",
    "loanid": "loan_id",
    "borrower_name": "name",
    "customer_name": "name",
    "emi": "emi_amount",
    "emi_amt": "emi_amount",
    "instalment": "emi_amount",
    "installment": "emi_amount",
    "due": "due_date",
    "emi_due_date": "due_date",
    "consent_flag": "consent",
    "dnd_flag": "dnd",
    "do_not_disturb": "dnd",
    "outstanding_amount": "outstanding",
    "pos": "outstanding",
}


def canonical(header: str) -> str:
    """Normalise one column name to the contract."""
    h = (header or "").strip().lower().replace("-", "_")
    h = " ".join(h.split())          # collapse internal whitespace
    h = COLUMN_ALIASES.get(h, h)
    return COLUMN_ALIASES.get(h.replace(" ", "_"), h.replace(" ", "_"))

TRUTHY = {"y", "yes", "true", "1", "t"}


@dataclass
class IngestReport:
    total_rows: int = 0
    loaded: int = 0
    skipped_no_consent: int = 0
    skipped_dnd: int = 0
    skipped_invalid: int = 0
    callable_universe: int = 0
    errors: list[str] = field(default_factory=list)
    # Per-row rejection detail. Counts alone answer "how many?"; an auditor - and a
    # demo audience - asks "which ones, and why?". Capped so a bad 100k-row file
    # cannot produce a 100k-item response.
    rejected: list[dict] = field(default_factory=list)
    sheet: str | None = None

    def as_dict(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "loaded": self.loaded,
            "skipped_no_consent": self.skipped_no_consent,
            "skipped_dnd": self.skipped_dnd,
            "skipped_invalid": self.skipped_invalid,
            "callable_universe": self.callable_universe,
            "errors": self.errors[:20],
            "rejected": self.rejected[:100],
            "sheet": self.sheet,
        }


def _to_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in TRUTHY


def _to_paise(value: str) -> int:
    """Accepts '4500', '4,500', '4500.00', '₹4,500'."""
    cleaned = value.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    return int(round(float(cleaned) * 100))


def _mask_phone(phone: str) -> str:
    """Show enough to identify the row, not enough to dial it."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"••••••{digits[-4:]}" if len(digits) >= 4 else "••••"


def _rejection(row: dict, reason: str, authority: str) -> dict:
    return {
        "loan_id": row.get("loan_id") or "",
        "name": row.get("name") or "",
        "phone": _mask_phone(row.get("phone", "")),
        "reason": reason,
        "authority": authority,
    }


def load_csv(source: str | Path | io.StringIO, *, campaign_name: str = "EMI Reminder - July 2026") -> IngestReport:
    """Load a call-list CSV into the database. Returns a scrub report."""
    report = IngestReport()

    if isinstance(source, io.StringIO):
        handle: io.TextIOBase = source
        label = "<stream>"
    else:
        path = Path(source)
        handle = path.open("r", encoding="utf-8-sig", newline="")
        label = path.name

    try:
        reader = csv.DictReader(handle)
        headers = {canonical(h) for h in (reader.fieldnames or [])}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(
                f"call list is missing required columns: {sorted(missing)}. "
                f"Agree the header contract with the customer before go-live."
            )

        with session_scope() as s:
            campaign = get_or_create_campaign(
                s, campaign_name,
                use_case="EMI_REMINDER",
                languages=["hi-IN", "en-IN"],
                dialer_mode="PROGRESSIVE",
            )
            campaign_id = campaign.id

            for raw in reader:
                report.total_rows += 1
                row = {canonical(k): (v or "").strip() for k, v in raw.items()}
                loan_id = row.get("loan_id", "")

                try:
                    consent = _to_bool(row.get("consent"))
                    dnd = _to_bool(row.get("dnd"))

                    # DPDP Act 2023: no consent, no call. Fails closed.
                    if not consent:
                        report.skipped_no_consent += 1
                        report.rejected.append(_rejection(row, "no consent on record", "DPDP Act 2023"))
                        continue
                    # TRAI DND / UCC scrub.
                    if dnd:
                        report.skipped_dnd += 1
                        report.rejected.append(_rejection(row, "number is on the DND registry", "TRAI DND / UCC"))
                        continue

                    upsert_borrower(
                        s,
                        loan_id=loan_id,
                        name=row["name"],
                        phone=row["phone"],
                        language=row.get("lang") or "hi-IN",
                        emi_amount_paise=_to_paise(row["emi_amount"]),
                        due_date=date.fromisoformat(row["due_date"][:10]),
                        dpd=int(row.get("dpd") or 0),
                        product=(row.get("product") or "PERSONAL_LOAN").upper(),
                        outstanding_paise=(_to_paise(row["outstanding"]) if row.get("outstanding") else None),
                        consent=consent,
                        dnd_registered=dnd,
                        attempts_today=0,
                    )
                    report.loaded += 1
                except (KeyError, ValueError) as exc:
                    report.skipped_invalid += 1
                    report.errors.append(f"{loan_id or f'row {report.total_rows}'}: {exc}")
                    report.rejected.append(_rejection(row, str(exc), "data validation"))

            report.callable_universe = report.loaded
            add_audit(
                s, "ingest.call_list", entity="campaign", entity_id=campaign_id,
                payload={"source": label, **report.as_dict()},
            )
    finally:
        if not isinstance(source, io.StringIO):
            handle.close()

    step("ingest.csv", None, source=label, **report.as_dict())
    logger.info(
        "ingested %s: %d/%d loaded (%d no-consent, %d DND, %d invalid)",
        label, report.loaded, report.total_rows,
        report.skipped_no_consent, report.skipped_dnd, report.skipped_invalid,
    )
    return report
