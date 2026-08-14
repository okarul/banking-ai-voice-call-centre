"""Database engine and session management.

The engine is created lazily on first use. Importing this module (or the
FastAPI app) never opens a connection, so the application still starts and
serves /health when PostgreSQL is offline or DATABASE_URL is unset.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when database access is attempted without a DATABASE_URL."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise DatabaseNotConfiguredError(
                "DATABASE_URL is not set. Copy backend/.env.example to "
                "backend/.env and set DATABASE_URL before using the database."
            )
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session context manager for scripts and tests.

    Commits on success, rolls back on error, always closes.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a read-oriented session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
