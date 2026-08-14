"""Helpers shared by the banking service tools.

Identity resolution is delegated to the authorization guards, so every tool
enforces the same rules and none of them accepts a customer id from its caller.
"""

from decimal import Decimal

from app.authorization import AuthorizationError, require_authenticated_customer
from app.sessions import SessionManager


def failure(reason: str, **extra) -> dict:
    """Structured failure result. Never carries a stack trace."""
    return {"success": False, "reason": reason, **extra}


def money(value: Decimal) -> str:
    """Serialise a Decimal without going through binary floating point."""
    return str(value)


def authenticated_session(session_id: str, manager: SessionManager):
    """Return (context, None) when authorized, else (None, failure dict).

    Wraps the central guard so tools can keep returning structured results
    instead of raising. Callers must run this before touching banking data.
    """
    try:
        context = require_authenticated_customer(session_id, manager=manager)
    except AuthorizationError as error:
        return None, error.to_dict()

    return context, None


def remember_context(manager, session, domain: str, key: str, value: str) -> None:
    """Record the current domain and selection on this session only.

    The existing context is copied before being changed, so one session's
    context can never become another's.
    """
    context = dict(session.conversation_context)
    context[key] = value
    manager.update_session(
        session.session_id,
        current_domain=domain,
        conversation_context=context,
    )
