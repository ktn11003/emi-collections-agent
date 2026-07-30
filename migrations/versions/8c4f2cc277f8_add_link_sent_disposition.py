"""add LINK_SENT disposition

A payment link delivered without a committed date is a real collections
outcome — reporting it as INCOMPLETE understates what the bot achieved.

Alembic's autogenerate cannot see enum *value* changes, so this is written by
hand. The two backends differ:

* **SQLite** — ``sa.Enum`` is a plain VARCHAR (SQLAlchemy 2.0 defaults
  ``create_constraint=False``, and this schema has no CHECK), so there is no DDL
  to run.
* **PostgreSQL** — ``sa.Enum`` is a native TYPE, so the value must be added with
  ``ALTER TYPE``. That cannot run inside a transaction block on older servers,
  hence the autocommit block.

Revision ID: 8c4f2cc277f8
Revises: 740872ea0cc8
Create Date: 2026-07-30 20:08:43.673096
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8c4f2cc277f8'
down_revision: Union[str, None] = '740872ea0cc8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_VALUE = "LINK_SENT"
ENUM_NAME = "disposition"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return   # VARCHAR-backed: the new value needs no DDL

    already_present = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :typ AND e.enumlabel = :val"
        ),
        {"typ": ENUM_NAME, "val": NEW_VALUE},
    ).scalar()
    if already_present:
        return

    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value: it would mean recreating the type and
    # every dependent column, and any row already using LINK_SENT would become
    # invalid. Left as an explicit no-op rather than silently corrupting data.
    pass
