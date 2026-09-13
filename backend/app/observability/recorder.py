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

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.observability.estimates import estimate_carbon_grams, estimate_cost_usd
from app.observability.redaction import redact_transcript
from app.telephony.channels import Channel, normalise_channel

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
def start_session(
    banking_session_id: str,
    *,
    channel: Channel | str = Channel.WEBRTC,
    provider_call_id: str | None = None,
    provider_event_id: str | None = None,
) -> str | None:
    """Record that a voice call has begun. Returns its operator-facing id.

    `channel` is how the audio arrived and nothing more. It is never read to
    decide who the caller is: `customer_id` stays null here and is only written
    by `record_authentication`, after the deterministic PIN check has passed.

    The two provider identifiers are stored for correlation and, later, for
    recognising a retry. Neither says anything about identity: a provider is
    naming a call, not a customer.
    """
    now = _now()
    with session_scope() as db:
        agent_session_id = _next_agent_session_id(db)
        db.add(
            AgentSession(
                agent_session_id=agent_session_id,
                banking_session_id=banking_session_id,
                channel=normalise_channel(channel).value,
                provider_call_id=provider_call_id,
                provider_event_id=provider_event_id,
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
def reconcile_active_sessions(
    owned_banking_sessions: set[str] | None = None,
) -> int:
    """Close out calls that cannot possibly still be running.

    Banking sessions live in memory, so a backend restart ends every call it
    was carrying — but the operational rows survive in PostgreSQL and would
    otherwise sit at ACTIVE for ever. An operator watching the board would see
    phantom agents that no customer is on, and the Active Agents count would
    never come back down.

    Run once at startup. Anything still open at that moment belongs to a
    process that no longer exists — **except** the calls this process is
    holding right now, which is what `owned_banking_sessions` names.

    That exception is not hypothetical. Phase 7.4A.1 measured a second
    application context starting while the first still carried a call: the live
    call was stamped `FORCED_CLEANUP`, meaning "left behind by a crash", while
    the caller was still talking. An open row is not evidence of a dead owner.

    Ownership is passed in rather than looked up, because this is the
    observability layer and it must not need to know that telephony exists.
    `app.process_ownership.reconcile_on_startup` is the caller that knows both,
    and the entry point a test should drive when it means "a process started".

    Omitting the argument reconciles everything, which is the right default for
    a caller that genuinely holds nothing — and what every existing test that
    drives this directly already expects.
    """
    now = _now()
    closed = 0
    owned = owned_banking_sessions or set()
    with session_scope() as db:
        conditions = [
            AgentSession.ended_at.is_(None),
            AgentSession.status != REJECTED,
        ]
        if owned:
            # A row with no banking session id cannot be one this process is
            # carrying, and must stay eligible for repair: `NOT IN` over a NULL
            # evaluates to NULL and would silently exclude it.
            conditions.append(
                or_(
                    AgentSession.banking_session_id.is_(None),
                    AgentSession.banking_session_id.notin_(owned),
                )
            )
        stale = db.scalars(select(AgentSession).where(*conditions)).all()

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
def record_rejection(
    reason: str = "CAPACITY_REJECTED",
    *,
    channel: Channel | str = Channel.WEBRTC,
    provider_call_id: str | None = None,
    provider_event_id: str | None = None,
) -> str | None:
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
                channel=normalise_channel(channel).value,
                provider_call_id=provider_call_id,
                provider_event_id=provider_event_id,
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


# --- telephone call registration ---------------------------------------------
#
# Everything above this line is observability, and every function is wrapped in
# `@_safe` so that a failed write can never fail a customer's call.
#
# The two functions below deliberately break that rule, and the reason is worth
# stating plainly: these writes are not a record of a decision, they *are* the
# decision. The unique index on `provider_call_id` is what stops one telephone
# call being answered twice, and the conditional status update is what stops one
# hang-up releasing a capacity slot twice. A swallowed exception here would
# report success to the caller while the guarantee silently did not hold — which
# is precisely the failure mode idempotency exists to prevent.
#
# So they raise, and `app.telephony.service` decides what a failure means.


# How many times a claim will retry a lost race for an operator-facing name.
# Small on purpose: each retry is one contended insert, and a provider that
# genuinely sent this many simultaneous calls is better served by a retry of
# its own than by us looping.
_CLAIM_ATTEMPTS = 5


class DuplicateProviderCall(Exception):
    """This provider call or event has already been registered."""

    def __init__(self, provider_call_id: str) -> None:
        super().__init__(provider_call_id)
        self.provider_call_id = provider_call_id


# The indexes that mean "we have seen this call before". Any *other* integrity
# violation means something else went wrong, and must not be reported as a
# duplicate — see `claim_phone_call`.
_PROVIDER_INDEXES = (
    "uq_agent_sessions_provider_call_id",
    "uq_agent_sessions_provider_event_id",
)


def _is_duplicate_provider_conflict(error: IntegrityError) -> bool:
    """Whether this integrity error is a provider id already in use."""
    text = str(getattr(error, "orig", error))
    return any(name in text for name in _PROVIDER_INDEXES)


def claim_phone_call(
    banking_session_id: str,
    *,
    provider_call_id: str,
    provider_event_id: str,
) -> str:
    """Register one telephone call, or refuse because it is already registered.

    The claim is the insert itself. Checking for an existing row first and
    inserting if absent would read correctly and behave wrongly: two workers
    handling the same retry would both find nothing, both insert, and both
    proceed to take a capacity slot for one telephone call. Here the database
    decides — the partial unique indexes admit exactly one of them, and the
    loser gets an `IntegrityError` that becomes `DuplicateProviderCall`.

    Note what is *not* set: `customer_id` stays null and `authenticated` stays
    false. A provider telling us a call exists has not told us who is on it.
    """
    now = _now()

    # `_next_agent_session_id` derives AGT-000123 from `max(id)`, which two
    # transactions running at the same instant will read identically. That
    # collides on the `agent_session_id` unique constraint — an integrity error
    # that has nothing to do with duplication.
    #
    # Treating every integrity error as a duplicate would be worse than the
    # collision it papered over: two genuinely different telephone calls
    # arriving together would see one of them acknowledged as a repeat and
    # silently never registered, leaving a real caller connected to nothing. So
    # the constraint is identified, and only a provider-id conflict counts as a
    # duplicate. A name collision is retried, because the next read of `max(id)`
    # sees the row the winner just committed.
    for _ in range(_CLAIM_ATTEMPTS):
        try:
            with session_scope() as db:
                agent_session_id = _next_agent_session_id(db)
                db.add(
                    AgentSession(
                        agent_session_id=agent_session_id,
                        banking_session_id=banking_session_id,
                        channel=Channel.PHONE.value,
                        provider_call_id=provider_call_id,
                        provider_event_id=provider_event_id,
                        customer_id=None,
                        authenticated=False,
                        auth_status=PENDING,
                        status=ACTIVE,
                        started_at=now,
                        capability=DEFAULT_CAPABILITY,
                        tool_call_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            return agent_session_id
        except IntegrityError as error:
            if _is_duplicate_provider_conflict(error):
                raise DuplicateProviderCall(provider_call_id) from error
            last_error = error

    # Every attempt lost the race for a name. Raising is correct: the caller
    # turns this into a generic failure, and the provider retries. Reporting a
    # duplicate here would strand the call instead.
    raise last_error


def close_phone_call(provider_call_id: str, *, reason: str = "CUSTOMER_ENDED"):
    """End the call with this provider id, exactly once.

    Returns the banking session id if this call was open and this is the call
    that closed it, or None if there was nothing to close — either because no
    such call exists, or because it has already ended.

    The distinction matters because the caller uses the return value to decide
    whether to hand a capacity slot back. Two `ended` events for one call must
    release one slot, so the transition to a closed status has to be the thing
    that is atomic, not a status check followed by an update. `UPDATE ... WHERE
    ended_at IS NULL` does that in one statement: the row moves once, and only
    the statement that moved it gets a row back.

    Scoped to `provider_call_id`, so an event naming a call that is not this
    one cannot reach into anybody else's.
    """
    now = _now()
    with session_scope() as db:
        result = db.execute(
            update(AgentSession)
            .where(
                AgentSession.provider_call_id == provider_call_id,
                AgentSession.channel == Channel.PHONE.value,
                AgentSession.ended_at.is_(None),
            )
            .values(
                ended_at=now,
                status=COMPLETED,
                disconnect_reason=reason,
                updated_at=now,
            )
            .returning(AgentSession.banking_session_id, AgentSession.started_at)
        ).first()

        if result is None:
            return None

        banking_session_id, started = result
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            db.execute(
                update(AgentSession)
                .where(AgentSession.provider_call_id == provider_call_id)
                .values(duration_seconds=max(0, int((now - started).total_seconds())))
            )
        return banking_session_id


@_safe("mark_phone_call_rejected")
def mark_phone_call_rejected(
    provider_call_id: str, *, reason: str = "CAPACITY_REJECTED"
) -> None:
    """Turn an already-claimed call into a refusal.

    Safe to swallow, unlike the two above: the claim has already happened, the
    capacity slot has already been handed back, and this only corrects what an
    operator sees on the board.
    """
    now = _now()
    with session_scope() as db:
        db.execute(
            update(AgentSession)
            .where(
                AgentSession.provider_call_id == provider_call_id,
                AgentSession.ended_at.is_(None),
            )
            .values(
                status=REJECTED,
                ended_at=now,
                duration_seconds=0,
                disconnect_reason=reason,
                banking_session_id=None,
                updated_at=now,
            )
        )
