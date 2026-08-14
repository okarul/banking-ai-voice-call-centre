"""Writing operational records, without ever getting in the way.

Every public function here is wrapped so that it cannot raise. That is not
defensive habit, it is the requirement: a customer mid-call must not lose their
balance enquiry because an audit row would not insert. When something fails,
the category is logged and the call carries on.

    call starts        -> start_session()
    caller verified    -> record_authentication()
    turn classified    -> record_turn()
    tool runs          -> record_tool_call()
    usage arrives      -> record_usage()
    call ends          -> end_session()
    admission refused  -> record_rejection()

Nothing here is on the critical path of an answer: each function opens its own
short-lived database session, writes, and closes.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.observability.estimates import estimate_carbon_grams, estimate_cost_usd
from app.observability.redaction import redact_transcript

logger = logging.getLogger("app.observability")

# Statuses an operator sees. Deliberately few.
ACTIVE = "ACTIVE"
AUTHENTICATING = "AUTHENTICATING"
WAITING = "WAITING"
COMPLETED = "COMPLETED"
DISCONNECTED = "DISCONNECTED"
ERROR = "ERROR"
REJECTED = "REJECTED"

VERIFIED = "VERIFIED"
PENDING = "PENDING"
FAILED = "FAILED"
LOCKED = "LOCKED"

CAPABILITY_BY_DOMAIN = {
    "ACCOUNT": "Account Services",
    "LOAN": "Loan Services",
    "AUTHENTICATION": "Authentication Workflow",
}
DEFAULT_CAPABILITY = "Supervisor"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(operation: str):
    """Decorator: log and swallow. Observability never breaks a call."""

    def wrap(function):
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as error:
                # Type only. A provider or database message could carry detail
                # that does not belong in a log.
                logger.error(
                    "observability %s failed: %s", operation, type(error).__name__
                )
                return None

        guarded.__name__ = function.__name__
        guarded.__doc__ = function.__doc__
        return guarded

    return wrap


def _next_agent_session_id(db) -> str:
    """AGT-000001, AGT-000002, ... A short handle an operator can read out."""
    highest = db.scalar(select(func.max(AgentSession.id))) or 0
    return f"AGT-{highest + 1:06d}"


def _find(db, banking_session_id: str) -> AgentSession | None:
    """The live record for a banking session, newest first."""
    return db.scalars(
        select(AgentSession)
        .where(AgentSession.banking_session_id == banking_session_id)
        .order_by(AgentSession.id.desc())
    ).first()


# --- lifecycle ---------------------------------------------------------------


@_safe("start_session")
def start_session(banking_session_id: str) -> str | None:
    """Record that a voice call has begun. Returns its operator-facing id."""
    now = _now()
    with session_scope() as db:
        agent_session_id = _next_agent_session_id(db)
        db.add(
            AgentSession(
                agent_session_id=agent_session_id,
                banking_session_id=banking_session_id,
                status=ACTIVE,
                auth_status=PENDING,
                authenticated=False,
                started_at=now,
                capability=DEFAULT_CAPABILITY,
                tool_call_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    return agent_session_id


@_safe("record_authentication")
def record_authentication(
    banking_session_id: str,
    *,
    customer_id: str | None,
    authenticated: bool,
    locked: bool = False,
    failed: bool = False,
) -> None:
    """Update who is on the call, once the backend has actually decided.

    `customer_id` is written only when authentication succeeded. A caller who
    merely claimed an identity has not established one, and the dashboard must
    not show an unverified claim as though it were a customer.
    """
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None:
            return

        if authenticated:
            record.customer_id = customer_id
            record.authenticated = True
            record.auth_status = VERIFIED
            record.status = ACTIVE
        elif locked:
            record.auth_status = LOCKED
        elif failed:
            record.auth_status = FAILED
        else:
            record.auth_status = PENDING
            record.status = AUTHENTICATING
        record.updated_at = _now()


@_safe("record_turn")
def record_turn(
    banking_session_id: str,
    *,
    domain: str | None = None,
    intent: str | None = None,
) -> None:
    """Record what the call is currently about."""
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None:
            return
        if domain:
            record.current_domain = domain
            record.capability = CAPABILITY_BY_DOMAIN.get(domain, DEFAULT_CAPABILITY)
        if intent:
            record.last_intent = intent
        record.updated_at = _now()


@_safe("record_tool_call")
def record_tool_call(
    banking_session_id: str,
    tool_name: str,
    *,
    status: str = "OK",
    duration_ms: int | None = None,
) -> None:
    """Count one banking tool. Its arguments are deliberately not stored."""
    now = _now()
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None:
            return
        record.tool_call_count = (record.tool_call_count or 0) + 1
        record.updated_at = now
        db.add(
            AgentToolEvent(
                session_pk=record.id,
                agent_session_id=record.agent_session_id,
                tool_name=tool_name,
                status=status,
                duration_ms=duration_ms,
                created_at=now,
            )
        )


@_safe("record_usage")
def record_usage(
    banking_session_id: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Accumulate reported token usage onto this call, and only this call.

    Usage is added to the record found by banking session id, so one caller's
    tokens can never land on another's row. Nothing is estimated: when the
    provider reports no usage, the columns stay null and the dashboard shows
    N/A.
    """
    if input_tokens is None and output_tokens is None:
        return
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None:
            return

        if input_tokens is not None:
            record.input_tokens = (record.input_tokens or 0) + int(input_tokens)
        if output_tokens is not None:
            record.output_tokens = (record.output_tokens or 0) + int(output_tokens)
        record.total_tokens = (record.input_tokens or 0) + (record.output_tokens or 0)

        record.estimated_cost_usd = estimate_cost_usd(
            record.input_tokens, record.output_tokens
        )
        record.estimated_carbon_grams = estimate_carbon_grams(record.total_tokens)
        record.updated_at = _now()


@_safe("record_message")
def record_message(
    banking_session_id: str,
    *,
    role: str,
    content: str | None,
    message_type: str = "SPEECH",
) -> None:
    """Store one safe transcript line.

    The content is redacted *before* it reaches the database, not on the way
    out: a PIN that is written down and filtered at display time has still been
    written down.
    """
    safe = redact_transcript(content, role=role)
    now = _now()
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None:
            return
        db.add(
            ConversationMessage(
                session_pk=record.id,
                agent_session_id=record.agent_session_id,
                role=role,
                safe_content=safe,
                message_type=message_type,
                created_at=now,
            )
        )
        record.updated_at = now


@_safe("end_session")
def end_session(
    banking_session_id: str,
    *,
    reason: str = "CUSTOMER_ENDED",
    error_category: str | None = None,
) -> None:
    """Close the record: end time, duration, and why the call finished.

    Called on every termination path — manual hang-up, spoken goodbye, silence
    timeout, maximum duration, provider failure, forced cleanup. A record with
    no end time is a call that is still running, so this must not be missed.
    """
    now = _now()
    with session_scope() as db:
        record = _find(db, banking_session_id)
        if record is None or record.ended_at is not None:
            return

        started = record.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        record.ended_at = now
        record.duration_seconds = (
            max(0, int((now - started).total_seconds())) if started else None
        )
        record.status = ERROR if error_category else COMPLETED
        record.disconnect_reason = reason
        record.error_category = error_category
        record.updated_at = now


@_safe("reconcile_active_sessions")
def reconcile_active_sessions() -> int:
    """Close out calls that cannot possibly still be running.

    Banking sessions live in memory, so a backend restart ends every call it
    was carrying — but the operational rows survive in PostgreSQL and would
    otherwise sit at ACTIVE for ever. An operator watching the board would see
    phantom agents that no customer is on, and the Active Agents count would
    never come back down.

    Run once at startup. Anything still open at that moment belongs to a
    process that no longer exists.
    """
    now = _now()
    closed = 0
    with session_scope() as db:
        stale = db.scalars(
            select(AgentSession).where(
                AgentSession.ended_at.is_(None), AgentSession.status != REJECTED
            )
        ).all()

        for record in stale:
            started = record.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            record.ended_at = now
            record.duration_seconds = (
                max(0, int((now - started).total_seconds())) if started else None
            )
            record.status = DISCONNECTED
            record.disconnect_reason = "FORCED_CLEANUP"
            record.updated_at = now
            closed += 1

    if closed:
        logger.info("reconciled %s agent session(s) left open by a restart", closed)
    return closed


@_safe("record_rejection")
def record_rejection(reason: str = "CAPACITY_REJECTED") -> str | None:
    """Record a call that was refused admission and never became a session.

    It has no customer and no banking session — that is the point of refusing
    it — so the row exists purely so an operator can see that somebody tried
    and was turned away.
    """
    now = _now()
    with session_scope() as db:
        agent_session_id = _next_agent_session_id(db)
        db.add(
            AgentSession(
                agent_session_id=agent_session_id,
                banking_session_id=None,
                customer_id=None,
                authenticated=False,
                auth_status=PENDING,
                status=REJECTED,
                started_at=now,
                ended_at=now,
                duration_seconds=0,
                disconnect_reason=reason,
                capability=DEFAULT_CAPABILITY,
                tool_call_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    return agent_session_id
