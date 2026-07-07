"""SQLAlchemy engine + session machinery.

Phase 2 of the cloud SaaS path. Same store APIs (``PilotProfileStore.load``,
``DayOverrideStore.save_one``, etc.) — only the backend changes from JSON
files to a SQL database.

DB URL resolution (each ``get_engine()`` call so tests can re-resolve):

1. ``NAC_PAY_DATABASE_URL`` env var if set (e.g. ``postgresql+psycopg2://...``)
2. ``sqlite:///{NAC_PAY_DATA_DIR}/nac_pay.db`` if ``NAC_PAY_DATA_DIR`` is set
3. ``sqlite:///{HOME}/.nac-pay/data/nac_pay.db`` as the default

The engine is cached at module level; ``dispose_engine()`` resets it (for
tests that need a fresh database between cases).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import get_data_dir


class Base(DeclarativeBase):
    """Single declarative base shared by every ORM model."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_url: str | None = None


def database_url() -> str:
    """Resolve the database URL. Resolved per call so tests + env changes
    take effect without an import-time freeze."""
    env = os.environ.get("NAC_PAY_DATABASE_URL")
    if env:
        return env
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'nac_pay.db'}"


def get_engine() -> Engine:
    """Lazy global engine. Created on first use, recreated when the URL
    changes (lets tests swap NAC_PAY_DATA_DIR mid-suite)."""
    global _engine, _session_factory, _engine_url
    url = database_url()
    if _engine is None or url != _engine_url:
        if _engine is not None:
            _engine.dispose()
        connect_args: dict = {}
        if url.startswith("sqlite"):
            # Allow same-process multi-thread usage (FastAPI TestClient).
            connect_args["check_same_thread"] = False
        _engine = create_engine(url, connect_args=connect_args, future=True)
        _session_factory = sessionmaker(
            bind=_engine, expire_on_commit=False, future=True,
        )
        _engine_url = url
        # Import models so their tables are registered, then create.
        from . import db_models  # noqa: F401  side-effect import
        Base.metadata.create_all(_engine)
        _ensure_added_columns(_engine)
    return _engine


# Columns introduced after their table first shipped. ``create_all`` only
# creates missing tables — it never ALTERs an existing one — so a new nullable
# column on an already-created table must be back-filled here. Keep entries
# until every live database is known to have them. Portable ADD COLUMN (SQLite
# + Postgres); nullable so no default/backfill of rows is needed.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "feed_reassignment_decisions": [("pch_value", "VARCHAR(16)")],
}


def _ensure_added_columns(engine: Engine) -> None:
    insp = sa_inspect(engine)
    for table, cols in _ADDED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    )


def session_factory() -> sessionmaker[Session]:
    get_engine()                      # ensures _session_factory is set
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager: commit on success, rollback on exception."""
    factory = session_factory()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def dispose_engine() -> None:
    """Drop the engine + session factory. Next ``get_engine()`` recreates
    them. Used by tests to start each case with a fresh database."""
    global _engine, _session_factory, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _engine_url = None


def reset_tables() -> None:
    """Drop and recreate every mapped table on the current engine. Used
    by tests for hermetic per-case state."""
    engine = get_engine()
    from . import db_models  # noqa: F401
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
