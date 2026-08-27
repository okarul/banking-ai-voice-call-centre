"""Can this process serve a call right now, and if not, which part is missing.

Liveness and readiness answer different questions and lead to different
actions. Liveness failing means restart this process. Readiness failing means
stop sending it traffic — the process is fine, something it depends on is not.
Conflating them turns a database blip into a restart loop.

Two rules shape everything here.

**Nothing costs money.** The model provider is reported from configuration
alone: whether a key and a model name are present. Opening a realtime session
to prove the provider works would make this endpoint billable, and a monitoring
system polling every ten seconds would then be an expense. The one thing
readiness genuinely cannot tell you is whether that provider will accept the
next session — which is exactly how a live deployment was lost to an empty
account — so `describe_connection_failure` covers that at the point of failure
instead.

**Nothing sensitive is reported.** No key, no fragment of a key, no connection
string, no hostname, no exception text. Each check answers ready or not, with a
short reason drawn from a fixed vocabulary.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import settings
from app.database.connection import session_scope

logger = logging.getLogger("app.observability.readiness")


def _database() -> dict:
    """Reachable and answering, established with the cheapest query there is."""
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception as error:
        # Type only. A connection error carries the connection string.
        logger.error("readiness: database unreachable (%s)", type(error).__name__)
        return {"ready": False, "reason": "unreachable"}


# What startup made of the schema, if startup has run in this process. `None`
# means it has not — a direct call in a test, say — and the check below then
# answers from the database rather than from an assumption.
_startup_schema_failure: str | None = None


def record_schema_initialisation(*, ready: bool, reason: str | None = None) -> None:
    """Remember how startup's schema initialisation went.

    Called once, from the lifespan. A process that could not build its schema
    must not then report itself ready to take calls: the database is reachable,
    `SELECT 1` answers, and the tables the application needs are not there.
    That is precisely the shape of the Phase 6.12.1 live defect.
    """
    global _startup_schema_failure
    _startup_schema_failure = None if ready else (reason or "initialisation_failed")


def _schema() -> dict:
    """Whether every table this application declares actually exists.

    Answered from the database, not from a flag, so it stays true for a process
    whose startup never ran and for one whose database changed underneath it.
    One reflection query, on an endpoint a monitor polls every few seconds.

    Only counts are reported. A table name is not a secret, but this module's
    rule is a fixed vocabulary and a number, and there is no reason to make an
    exception for the one check most likely to fire during a bad deployment.
    """
    if _startup_schema_failure is not None:
        return {"ready": False, "reason": _startup_schema_failure}

    try:
        from sqlalchemy import inspect

        from app.database.connection import get_engine
        from app.database.models import Base

        existing = set(inspect(get_engine()).get_table_names())
        missing = set(Base.metadata.tables) - existing
        if missing:
            # Names go to the log, where an operator can act on them; the
            # response says how many.
            logger.error(
                "readiness: schema incomplete, %d table(s) missing: %s",
                len(missing),
                ", ".join(sorted(missing)),
            )
            return {"ready": False, "reason": "tables_missing", "missing": len(missing)}
        return {"ready": True}
    except Exception as error:
        # Type only. A connection error carries the connection string.
        logger.error("readiness: schema unverifiable (%s)", type(error).__name__)
        return {"ready": False, "reason": "unverifiable"}


def _realtime_configuration() -> dict:
    """Configured, not proven. See the module docstring for why."""
    if not getattr(settings, "openai_api_key", None):
        return {"ready": False, "reason": "no_api_key"}
    if not getattr(settings, "realtime_model", None):
        return {"ready": False, "reason": "no_model"}
    return {"ready": True, "model": settings.realtime_model, "probed": False}


def _telephony() -> dict:
    """Whether the telephone channel is switched on and usable if it is.

    A channel that is off is not unready — plenty of deployments run browser
    calls only — so it reports its state and stays out of the verdict.
    """
    if not getattr(settings, "telephony_enabled", False):
        return {"ready": True, "enabled": False}

    if not getattr(settings, "telephony_webhook_secret", None):
        return {"ready": False, "enabled": True, "reason": "no_webhook_secret"}

    from app.telephony.media import PROTOCOL_VERSION

    return {
        "ready": True,
        "enabled": True,
        "media_protocol_version": PROTOCOL_VERSION,
        "transport": settings.telephony_media_transport,
    }


def _capacity() -> dict:
    """How much of the concurrency ceiling is in use. Never a reason to refuse.

    A full switchboard is a busy service, not an unready one, so this is
    reported for the operator and excluded from the verdict.
    """
    try:
        from app.realtime.browser_calls import voice_call_manager

        return {
            "ready": True,
            "in_use": voice_call_manager.used_capacity(),
            "ceiling": settings.realtime_max_active_sessions,
        }
    except Exception as error:
        logger.error("readiness: capacity unreadable (%s)", type(error).__name__)
        # Still ready. "Never a reason to refuse" has to hold for the failure
        # branch too, or the promise is only kept while nothing goes wrong:
        # `readiness_report` takes `all()` over these, so returning False here
        # would pull a process with a healthy database, a configured provider
        # and a working telephone channel out of rotation because a counter
        # could not be read. The operator is told, and the verdict is not.
        return {"ready": True, "reason": "capacity_unavailable"}


def readiness_report() -> dict:
    """Every dependency, and one verdict over them."""
    checks = {
        "database": _database(),
        "schema": _schema(),
        "realtime": _realtime_configuration(),
        "telephony": _telephony(),
        "capacity": _capacity(),
    }
    return {
        "ready": all(check["ready"] for check in checks.values()),
        "checks": checks,
    }
