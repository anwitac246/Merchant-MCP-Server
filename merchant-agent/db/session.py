from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from db.models import Base

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "merchant.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=_connect_args)
_SessionFactory = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
_ScopedSession = scoped_session(_SessionFactory)


def init_db(drop_first: bool = False) -> None:
    """Create all tables. If drop_first, wipe the schema first (used by
    normalize.py to give a clean, reproducible dataset on every run)."""
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if drop_first:
        Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)


def get_session() -> Session:
    return _ScopedSession()


def get_engine():
    return _engine