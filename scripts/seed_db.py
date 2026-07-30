"""Create the schema and load the call list.

    python scripts/seed_db.py            # create + ingest data/call_list.csv
    python scripts/seed_db.py --reset    # drop everything first
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.config import settings                    # noqa: E402
from app.db.base import create_all, db_flavour, engine  # noqa: E402
from app.db.models import Base                     # noqa: E402
from app.ingest.csv_loader import load_csv         # noqa: E402
from app.steplog import configure_logging          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the collections agent database.")
    ap.add_argument("--reset", action="store_true", help="drop all tables first")
    ap.add_argument("--csv", default="data/call_list.csv")
    ap.add_argument("--campaign", default="EMI Reminder - July 2026")
    args = ap.parse_args()

    configure_logging()

    if args.reset:
        print("dropping all tables…")
        Base.metadata.drop_all(engine)

    create_all()
    print(f"schema ready on {db_flavour()} ({settings.database_url})")
    print(f"  {len(Base.metadata.tables)} tables: {', '.join(sorted(Base.metadata.tables))}")

    path = settings.resolve(args.csv)
    if not path.exists():
        print(f"call list not found: {path}", file=sys.stderr)
        return 1

    report = load_csv(path, campaign_name=args.campaign)
    print("\ncall-list ingest (SFTP feed simulation)")
    print(f"  rows read           : {report.total_rows}")
    print(f"  loaded              : {report.loaded}")
    print(f"  skipped, no consent : {report.skipped_no_consent}   (DPDP Act 2023)")
    print(f"  skipped, DND        : {report.skipped_dnd}   (TRAI DND/UCC)")
    print(f"  skipped, invalid    : {report.skipped_invalid}")
    for err in report.errors:
        print(f"      ! {err}")
    print(f"\n  callable universe   : {report.callable_universe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
