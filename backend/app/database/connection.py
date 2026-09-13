"""Database engine and session management.

The engine is created lazily on first use. Importing this module (or the
FastAPI app) never opens a connection, so the application still starts and
serves /health when PostgreSQL is offline or DATABASE_URL is unset.

**Every wait on this database is bounded.** That is the load-bearing property
of this module and it is not the default. `create_engine` with nothing said
about timeouts gives a pool that waits thirty seconds for a connection, a
libpq connect with no deadline at all, and statements that run until the
server decides otherwise. Each of those is fine while PostgreSQL is healthy
and each of them is a hang while it is not — and "not" includes the most
ordinary failure there is, a database whose host still accepts TCP but no
longer answers: a stopped container behind a live port-forward, a dropped
packet filter, a failed-over host.

That distinction matters more here than in an ordinary web service. A refusal
this application already survives by design — readiness goes red, `_safe`
swallows the write, the call carries on. A *hang* it does not: the realtime
event pump awaits its turn-decision write inline so the ruling is in force
before the next tool runs, and `PhoneCallBridge.close` awaits the trace writes
it must not abandon. A database that never answers therefore stops the audio
of a live call and stalls its teardown, holding a capacity slot behind it.

So the bounds below are deliberately short. They are not tuning: they convert
a silent stall into a fast, loud, recoverable error.
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


def _server_side_timeouts() -> str:
    """The `options` string that bounds a statement once it has started.

    Sent on connect so every session carries them, rather than issued as a
    `SET` some code path could forget. Zero disables one, which is libpq's own
    convention and the one `_positive_int` already follows.
    """
    wanted = (
        ("statement_timeout", settings.database_statement_timeout_ms),
        ("lock_timeout", settings.database_lock_timeout_ms),
        (
            "idle_in_transaction_session_timeout",
            settings.database_idle_transaction_timeout_ms,
        ),
    )
    return " ".join(f"-c {name}={value}" for name, value in wanted if value)


def _connect_args(url: str) -> dict:
    """Driver-level connection settings, for PostgreSQL only.

    Guarded by the scheme because `connect_timeout` and `options` are libpq
    parameters. Passing them to another driver — SQLite in a throwaway test,
    say — is a `TypeError` at connect time, which would turn a hardening
    measure into an outage of its own.
    """
    if not url.split("://", 1)[0].startswith("postgres"):
        return {}

    args: dict = {}
    if settings.database_connect_timeout:
        # The one that turns an unreachable host from an indefinite hang into
        # an error. libpq has no default for this.
        args["connect_timeout"] = settings.database_connect_timeout

    options = _server_side_timeouts()
    if options:
        args["options"] = options
    return args


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise DatabaseNotConfiguredError(
                "DATABASE_URL is not set. Copy backend/.env.example to "
                "backend/.env and set DATABASE_URL before using the database."
            )
        _engine = create_engine(
            settings.database_url,
            # Answers the connection-was-closed-underneath-us case, which
            # `pool_recycle` alone cannot: a proxy or failover can drop a
            # connection at any age.
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            # Waiting for a free connection is waiting, and this application
            # would rather fail a write than stall a call behind one.
            pool_timeout=settings.database_pool_timeout,
            connect_args=_connect_args(settings.database_url),
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def reset_engine() -> None:
    """Drop the cached engine and factory, disposing of any open connections.

    For tests that change a database setting, and for nothing else: the engine
    reads its bounds once, at creation, so a test that patches one and then
    asks for a connection would otherwise be handed the engine built before
    the patch and quietly assert nothing.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


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
