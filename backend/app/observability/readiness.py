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
        return {"ready": False, "reason": "unreadable"}


def readiness_report() -> dict:
    """Every dependency, and one verdict over them."""
    checks = {
        "database": _database(),
        "realtime": _realtime_configuration(),
        "telephony": _telephony(),
        "capacity": _capacity(),
    }
    return {
        "ready": all(check["ready"] for check in checks.values()),
        "checks": checks,
    }
