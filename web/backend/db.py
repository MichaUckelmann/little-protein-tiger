"""
SQLModel engine and session dependency.

Database lives at web/web.db (SQLite, file-based, no separate process needed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlmodel import Session, SQLModel, create_engine

_ROOT = Path(__file__).resolve().parent.parent.parent
_DB_PATH = _ROOT / "web" / "web.db"

engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create all tables on first startup.

    Import all models here so SQLModel.metadata is populated before create_all.
    Adding a new model? Register it in this import.
    """
    from web.backend import models_db  # noqa: F401 — registers all table metadata
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a SQLModel session per request."""
    with Session(engine) as session:
        yield session
