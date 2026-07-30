"""Engine / session wiring.

The only place the database URL is read, so switching SQLite -> Postgres is a
one-line .env change with no code edits.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base


def _make_engine() -> Engine:
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}

    if url.startswith("sqlite"):
        # The file lives under data/; make sure the directory exists first.
        if ":///" in url and ":memory:" not in url:
            db_path = settings.resolve(url.split(":///", 1)[1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+pysqlite:///{db_path}"
        # FastAPI touches the session from several tasks on one connection.
        kwargs["connect_args"] = {"check_same_thread": False}

    return create_engine(url, **kwargs)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
    """SQLite needs FK enforcement turned on explicitly; WAL helps concurrency."""
    if engine.dialect.name != "sqlite":
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def create_all() -> None:
    """Create the schema directly (used by tests and `scripts/seed_db.py`).

    Production path is `alembic upgrade head`; both produce the same schema.
    """
    Base.metadata.create_all(engine)


def db_flavour() -> str:
    return engine.dialect.name


def sqlite_file() -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    p = engine.url.database
    return Path(p) if p and p != ":memory:" else None
