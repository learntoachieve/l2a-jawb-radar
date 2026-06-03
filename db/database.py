"""
Database session factory.
Reads JAWB_DB_BACKEND from env (sqlite | postgres).
Defaults to SQLite at data/jawb.db for local MVP.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from db.models import Base


def _get_database_url() -> str:
    backend = os.getenv("JAWB_DB_BACKEND", "sqlite").lower()

    if backend == "postgres":
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "JAWB_DB_BACKEND=postgres but DATABASE_URL is not set. "
                "Check your .env file."
            )
        return url

    # SQLite default
    sqlite_path = os.getenv("JAWB_SQLITE_PATH", "data/jawb.db")
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{sqlite_path}"


def _make_engine():
    url = _get_database_url()
    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
    return create_engine(url, echo=False)


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def init_db() -> None:
    """Create all tables. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    Base.metadata.create_all(get_engine())


@contextmanager
def get_session() -> Session:
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
