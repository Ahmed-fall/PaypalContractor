"""SQLite (WAL) locally / PostgreSQL on Render: engine, sessions and ID helpers."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any, cast

from sqlalchemy import create_engine, event, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from ulid import ULID

from ..config import get_settings


def utcnow() -> dt.datetime:
    """Timezone-aware UTC timestamp; the single time source for database rows."""
    return dt.datetime.now(dt.UTC)


def new_id(prefix: str) -> str:
    """Prefixed lower-case ULID: ``cnt_01J9Z8A...`` (A.3 primary keys)."""
    return f"{prefix}_{str(ULID()).lower()}"


class Base(DeclarativeBase):
    pass


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def _build_engine() -> Engine:
    settings = get_settings()
    engine: Engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {},
        future=True,
    )
    if settings.database_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables and seed demo data when the database is empty (A.1).

    Alembic is unnecessary in the demo: every environment starts from a fresh
    database (the free Render Postgres is created on purpose close to judging),
    and ``SEED_DEMO_DATA``/``/demo/reset`` rebuild the whole demo in seconds.
    """
    from . import models  # noqa: F401  (imports register every table)
    from .seed import seed_demo

    Base.metadata.create_all(engine)
    seed_demo()


def conflict_insert(
    session: Session, model: Any, values: dict[str, Any], *index_elements: str
) -> bool:
    """``INSERT ... ON CONFLICT DO NOTHING``; returns True when this caller won.

    Works on both the SQLite and PostgreSQL dialects (PIT-03 capture-first lock).
    """
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = cast("Any", pg_insert(model).values(**values))
    else:
        from sqlalchemy.dialects.sqlite import insert as lite_insert

        stmt = cast("Any", lite_insert(model).values(**values))
    result = session.execute(
        stmt.on_conflict_do_nothing(index_elements=list(index_elements))
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def conditional_update(model: Any, values: dict[str, Any], *where_clauses: Any) -> Any:
    """Conditional UPDATE statement used for atomic capture claims (R2)."""
    return update(model).values(**values).where(*where_clauses)


__all__ = [
    "Base",
    "SessionLocal",
    "conditional_update",
    "conflict_insert",
    "engine",
    "get_db",
    "init_db",
    "new_id",
    "utcnow",
]
