"""The per-turn trace: what happened on one call, in order, safely.

A live call that goes wrong is diagnosed from a sequence of decisions, not from
a pile of counters. `agent_sessions` says the call ended `PROVIDER_ENDED` with
six tool calls; `agent_tool_events` says `get_account_balance` failed twice
taking four seconds each. Neither says *why*, and the four seconds between them
is where the answer lives. Phase 6.11.1 was found by adding that context by
hand, from production logs, after the fact. This module is that context, kept
on purpose.

**What is recorded.** What the backend decided, turn by turn: the scope ruling
and its category, the enquiry being held across authentication, the
authentication state, which tool ran with which sanitised arguments, what it
returned and how long it took, and how the call ended.

**What is not.** No model reasoning, no chain of thought, no internal
deliberation - only observable decisions and state. And, by default, no speech
at all: `TELEPHONY_TRACE_UTTERANCES` is off, so Channel 2 keeps the property it
has always had (checklist Q-121). Turned on for a UAT, an utterance is stored
only in the form `app.observability.redaction` already produces for the
browser's transcript - a PIN turn is "[PIN REDACTED]", never the digits.

**Append-only.** Nothing here is updated after it is written, so a trace cannot
be quietly rewritten to agree with a later theory.

**Never breaks a call.** Every entry point swallows its own failures the way
`app.observability.business` does. A tracing outage is an operator's problem;
it is not the customer's, and it must never turn a working balance into a
failed one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, CallTraceEvent
from app.observability.redaction import (
    CUSTOMER_ID_PLACEHOLDER,
    PIN_PLACEHOLDER,
    redact_transcript,
)
from app.redaction import redact

logger = logging.getLogger("app.observability.trace")

# One stable string to alert on, matching MIRROR_FAILED next door.
TRACE_FAILED = "TRACE_WRITE_FAILED"

# The kinds of thing a trace records.
KIND_TURN = "TURN"
KIND_TOOL = "TOOL"
KIND_AUTH = "AUTH"
KIND_LIFECYCLE = "LIFECYCLE"

SPEAKER_CUSTOMER = "CUSTOMER"
SPEAKER_AGENT = "AGENT"
SPEAKER_SYSTEM = "SYSTEM"

# Where the per-call sequence counter lives on the banking session. Kept on the
# session rather than derived from `MAX(sequence)`, which would need a read
# before every write and would still race two events into one number.
SEQUENCE_KEY = "trace_sequence"
TURN_KEY = "trace_turn"

# Where the resolved call row is remembered, so it is looked up once per call
# rather than once per event.
#
# Every trace event has to be anchored to this call's `agent_sessions` row, and
# resolving that by query each time put a database round trip on the path of
# every turn and every tool call. With several calls in flight it was enough to
# delay assistant audio measurably - which is a bad trade for a diagnostic.
#
# `False` means "this banking session has no call row". Both channels create
# that row as part of admitting the call, before anything can be traced, so a
# miss means there is no call to attach to rather than one that has not arrived
# yet - and remembering the miss is what keeps a session with no row (a direct
# realtime session in a test, say) from paying for a lookup on every event.
ANCHOR_KEY = "trace_anchor"

# Tool arguments that may be recorded, and nothing else.
#
# An allowlist rather than a denylist, because the argument that must never be
# stored is `spoken_pin`, and a denylist is a promise that nobody will ever add
# a second one. These four are drawn from fixed vocabularies - account and loan
# types, a row count - and none of them is a credential.
SAFE_ARGUMENTS = frozenset({"account_type", "loan_type", "limit"})

# What a value is replaced with when it is not on the allowlist.
REDACTED_ARGUMENT = "[redacted]"

# Longest sanitised argument string stored, matching the column.
_MAX_ARGUMENTS = 200


@dataclass
class TraceEvent:
    """One row, before it is written. Every field optional but `kind`."""

    kind: str
    speaker: str | None = None
    utterance: str | None = None
    turn: int | None = None
    domain: str | None = None
    intent: str | None = None
    scope_category: str | None = None
    scope_allowed: bool | None = None
    refusal_reason: str | None = None
    pending_operation: str | None = None
    account_type: str | None = None
    loan_type: str | None = None
    auth_status: str | None = None
    customer_ref: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    tool_status: str | None = None
    failure_reason: str | None = None
    duration_ms: int | None = None
    event_type: str | None = None
    disconnect_reason: str | None = None
    idempotency_key: str | None = None
    extra: dict = field(default_factory=dict)


def _never_fails(operation: str):
    """Log and swallow. A trace outage is not a banking outage."""

    def wrap(function):
        def guarded(*args, **kwargs):
            if not settings.trace_enabled:
                return None
            try:
                return function(*args, **kwargs)
            except Exception as error:
                logger.error(
                    "%s %s failed: %s", TRACE_FAILED, operation, type(error).__name__
                )
                return None

        guarded.__name__ = function.__name__
        guarded.__doc__ = function.__doc__
        return guarded

    return wrap


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- what may be stored -----------------------------------------------------


def sanitize_arguments(arguments) -> str | None:
    """The recordable form of a tool's arguments, or None if there are none.

    Values are kept only for allowlisted names, and only when they are short
    scalars from the vocabularies those names use. Everything else keeps its
    *name* - which is the diagnostic half - and loses its value.

    `spoken_pin` and `spoken_customer_id` are not on the allowlist and never
    will be, so a PIN cannot reach this table even if a future tool starts
    passing one through a path nobody reviewed.
    """
    if not isinstance(arguments, dict) or not arguments:
        return None

    safe = {}
    for name, value in arguments.items():
        if name not in SAFE_ARGUMENTS:
            safe[name] = REDACTED_ARGUMENT
            continue
        if value is None or isinstance(value, (int, float, bool)):
            safe[name] = value
            continue
        if isinstance(value, str) and len(value) <= 40:
            # Redacted even here: an allowlisted name is not a promise about
            # what somebody put in it.
            safe[name] = redact(value)
            continue
        safe[name] = REDACTED_ARGUMENT

    rendered = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    return rendered[:_MAX_ARGUMENTS]


# What the bank is waiting to hear, when it is waiting for a credential.
CREDENTIAL_PIN = "PIN"
CREDENTIAL_CUSTOMER_ID = "CUSTOMER_ID"


def expected_credential(session, decision=None) -> str | None:
    """The credential this caller turn is answering, from session state.

    **This is the rule that keeps a PIN out of the database, and it does not
    read the words.** A live call transcribed "four eight two one" into Urdu
    script; `looks_like_pin` knows Latin digits and English number words, saw
    neither, and the trace stored the caller's PIN verbatim. The authentication
    path had understood it perfectly well - `submit_pin` succeeded on the same
    turn - so the bank knew what had just been said while the redaction layer
    did not.

    Nothing about that is fixable by adding more number words. A PIN can arrive
    in any language, any script, any spelling, mis-transcribed, or as digits,
    and the only thing that reliably identifies it is that **the bank asked for
    one and has not had it yet**. So that is what is asked here.

    `candidate_customer_id` is set by a successful `submit_customer_id` and
    cleared by nothing until the call ends, so between that and verification
    the bank is waiting for a PIN. A caller who says something else in that
    window is over-redacted, which is the right direction to be wrong in.
    """
    if session is None or getattr(session, "authenticated", False):
        return None

    category = getattr(getattr(decision, "category", None), "value", None)

    if getattr(session, "candidate_customer_id", None):
        # Unless the caller plainly asked the bank something instead. Four
        # digits cannot become a balance enquiry: a supported intent needs an
        # action word and a domain, so nothing credential-shaped can leave by
        # this door - while a caller who says "actually, my savings balance?"
        # mid-verification still reads as the question they asked.
        return None if _is_banking_enquiry(category, decision) else CREDENTIAL_PIN
    if category == "AUTHENTICATION":
        return CREDENTIAL_CUSTOMER_ID

    # The bank has asked who is calling - it is holding an enquiry it cannot
    # answer yet - and this turn is not itself an enquiry. It is the answer.
    if category in (None, "NON_BANKING_REQUEST", "SOCIAL") and _enquiry_held(session):
        return CREDENTIAL_CUSTOMER_ID

    return None


_OWN_ENQUIRY_CATEGORIES = frozenset(
    {"OWN_ACCOUNT_ENQUIRY", "OWN_TRANSACTION_ENQUIRY", "OWN_LOAN_ENQUIRY"}
)


def _is_banking_enquiry(category, decision) -> bool:
    """Whether the caller asked the bank a supported question on this turn."""
    if category not in _OWN_ENQUIRY_CATEGORIES:
        return False
    intent = getattr(getattr(decision, "intent", None), "value", None)
    return bool(intent) and intent != "UNKNOWN"


def _enquiry_held(session) -> bool:
    from app import pending_request

    return pending_request.recall(session) is not None


def utterance_for(
    text: str | None, *, speaker: str, session=None, decision=None, expected=...
) -> str | None:
    """The recordable form of something that was said, or None.

    None unless an operator has turned utterances on, and never the raw words
    even then.

    A credential the bank is *expecting* is replaced whole, before anything is
    written, on the strength of the authentication state rather than the shape
    of the words - see `expected_credential`. Everything else goes through the
    same redaction the browser's transcript uses.
    """
    if not settings.telephony_trace_utterances:
        return None
    if not text or not text.strip():
        return None

    if speaker != SPEAKER_AGENT:
        # A frozen answer from the moment the turn was ruled wins over asking
        # again now: by now the credential may have been accepted, and the
        # question would answer "none expected" about the very words that were
        # the credential. See `reserve_turn`.
        if expected is ...:
            expected = expected_credential(session, decision)
        if expected == CREDENTIAL_PIN:
            return PIN_PLACEHOLDER
        if expected == CREDENTIAL_CUSTOMER_ID:
            return CUSTOMER_ID_PLACEHOLDER

    role = SPEAKER_AGENT if speaker == SPEAKER_AGENT else SPEAKER_CUSTOMER
    return redact_transcript(text, role=role)[:2000]


# How a caller turn reads in a replay, when the scope category alone would
# mislead. These are presentation, not policy: the gate's own ruling is stored
# beside them, untouched.
EVENT_AUTH_INPUT = "auth_input"
EVENT_CLOSING = "closing"
EVENT_SOCIAL = "social"
EVENT_CALLER_TURN = "caller_turn"

DOMAIN_AUTHENTICATION = "AUTHENTICATION"
DOMAIN_CLOSING = "CLOSING"
DOMAIN_SOCIAL = "SOCIAL"

INTENT_CUSTOMER_ID_INPUT = "CUSTOMER_ID_INPUT"
INTENT_PIN_INPUT = "PIN_INPUT"


def describe_turn(session, decision, transcript: str | None, expected=...) -> dict:
    """What this caller turn *was*, for somebody reading the call back.

    The scope gate answers one question — may this turn reach banking data —
    and answers it in its own vocabulary. That vocabulary is exactly right for
    the gate and misleading in a transcript: the four digits a caller reads out
    when the bank asks for their PIN are ruled `NON_BANKING_REQUEST`, which is
    true (a PIN is not a banking enquiry) and reads like a refusal of something
    the caller never asked for. A goodbye fares no better: "no, that is all,
    thank you" is more than one courtesy phrase, so it misses `SOCIAL` and
    lands in the same place.

    So the turn is *described* here as well as ruled. `scope_category` and
    `scope_allowed` keep the gate's real answer — they are the record of a real
    decision, and the whole of Phase 6.11 lives in being able to read them —
    and `intent`, `domain` and `event_type` say what the turn actually was.

    Nothing here changes what the gate decides, what the model is told, or what
    the bank does. It changes only how the call reads afterwards.
    """
    category = getattr(getattr(decision, "category", None), "value", None)
    intent = getattr(getattr(decision, "intent", None), "value", None)
    domain = getattr(getattr(decision, "domain", None), "value", None)

    if expected is ...:
        expected = expected_credential(session, decision)

    # 1. The PIN, which the bank asked for a moment ago. Recognised from
    #    session state alone: the words may be in any script, and a live call
    #    proved they may be in one nothing here can read.
    if expected == CREDENTIAL_PIN:
        return {
            "intent": INTENT_PIN_INPUT,
            "domain": DOMAIN_AUTHENTICATION,
            "event_type": EVENT_AUTH_INPUT,
        }

    # 2. The customer id, by the same state rule.
    if expected == CREDENTIAL_CUSTOMER_ID:
        return {
            "intent": INTENT_CUSTOMER_ID_INPUT,
            "domain": DOMAIN_AUTHENTICATION,
            "event_type": EVENT_AUTH_INPUT,
        }

    # 3. Ending the call. The deterministic classifier recognises every way a
    #    caller says it, including the ones `_is_social` is too strict for.
    if _is_closing(transcript):
        return {
            "intent": "END_CALL",
            "domain": DOMAIN_CLOSING,
            "event_type": EVENT_CLOSING,
        }

    # 4. Ordinary courtesy, and which kind. "Hello" and "thank you" are both
    #    social and are not the same moment in a call.
    if category == "SOCIAL":
        return {
            "intent": _social_kind(transcript) or intent or "SOCIAL",
            "domain": DOMAIN_SOCIAL,
            "event_type": EVENT_SOCIAL,
        }

    return {"intent": intent, "domain": domain, "event_type": EVENT_CALLER_TURN}


def _awaiting_pin(session) -> bool:
    """Whether the bank has asked this caller for a PIN and not yet had one."""
    if session is None:
        return False
    return bool(
        getattr(session, "candidate_customer_id", None)
    ) and not getattr(session, "authenticated", False)


def _looks_like_pin(transcript: str | None) -> bool:
    """The shape of a spoken credential. The words themselves are not kept."""
    if not transcript:
        return False
    from app.observability.redaction import looks_like_pin

    return looks_like_pin(transcript)


def _social_kind(transcript: str | None) -> str | None:
    """GREETING or THANKS, from the classifier that already knows the words."""
    if not transcript:
        return None
    from app.agents.intents import social_turn

    return social_turn(transcript)


def _is_closing(transcript: str | None) -> bool:
    """Whether the caller asked to finish, however they put it."""
    if not transcript:
        return False
    from app.agents.intents import Intent, classify

    return classify(transcript).intent is Intent.END_CALL


def customer_ref(session) -> str | None:
    """The safe reference for whoever the backend believes is calling.

    Read from the session, and only once the PIN check has established it - a
    claimed identity is not an identity, and a trace that recorded claims would
    be a trace that lies about who was on the call.
    """
    if session is None or not getattr(session, "authenticated", False):
        return None
    return getattr(session, "customer_id", None)


def auth_status(session) -> str:
    """VERIFIED / LOCKED / PENDING, from session state alone."""
    if session is None:
        return "UNKNOWN"
    if getattr(session, "authentication_locked", False):
        return "LOCKED"
    return "VERIFIED" if getattr(session, "authenticated", False) else "PENDING"


# --- ordering ---------------------------------------------------------------


def _anchor(session):
    """The remembered call row for this session: a tuple, False, or None.

    None means "not looked up yet". False means "looked up, and there is no
    call row" - which is a real answer and worth remembering, because the
    alternative is querying for a row that will never exist on every event.
    """
    if session is None:
        return None
    return session.conversation_context.get(ANCHOR_KEY)


def _remember_anchor(session, anchor) -> None:
    if session is not None:
        session.conversation_context[ANCHOR_KEY] = anchor


def _next_sequence(session) -> int | None:
    """The next replay position for this call, or None if it must be looked up.

    Held on the banking session, so it costs no read and cannot collide with
    another call's numbering. The last events of a call - the disconnect, most
    of all - arrive *after* the session has been destroyed, and numbering those
    zero would sort the ending first. Those fall back to a read; see `record`.
    """
    if session is None:
        return None
    context = session.conversation_context
    nxt = int(context.get(SEQUENCE_KEY, 0)) + 1
    context[SEQUENCE_KEY] = nxt
    return nxt


def _sequence_after(db, session_pk: int) -> int:
    """One past the highest position already recorded for this call."""
    highest = db.scalars(
        select(CallTraceEvent.sequence)
        .where(CallTraceEvent.session_pk == session_pk)
        .order_by(CallTraceEvent.sequence.desc())
    ).first()
    return int(highest or 0) + 1


def reserve_turn(session, decision) -> dict | None:
    """Freeze what this turn *is*, at the moment it is ruled.

    Two things are captured, and both have to be, because the write happens
    later and the call moves on in between.

    **The credential the bank was expecting.** This is the one that matters.
    A caller says their PIN; the pump rules the turn; the model calls
    `submit_pin`; it succeeds; the session becomes authenticated - and only
    then does the write reach the database. Asking "is a credential expected?"
    at that point answers *no*, because the PIN has just been accepted, and the
    words would be stored in clear. Asked here, while the bank is still waiting
    for it, the answer is yes. The same applies one step earlier, where
    `submit_customer_id` sets the candidate and turns the id turn into a PIN
    turn if the question is asked too late.

    **The replay position**, so the caller's words keep their place ahead of
    the tool they caused. The live trace read `submit_customer_id`, then the
    auth transition, then the words - backwards.

    Nothing is delayed by any of this: it is a dictionary, built synchronously,
    and the write still happens off the event loop.
    """
    if not settings.trace_enabled:
        return None
    return {
        "sequence": _next_sequence(session),
        "expected": expected_credential(session, decision),
    }


def reserve_sequence(session) -> int | None:
    """Take this turn's replay position at the moment it is *ruled*.

    A caller turn is classified synchronously, inside the realtime event pump,
    before the model can reach for a tool. Its trace row is written later, on a
    worker thread - and the position used to be taken there, so a replay showed

        submit_customer_id  TOOL
        AUTH
        CUSTOMER_ID_INPUT   TURN

    the caller's words arriving after the tool they caused. Reserving the
    position here puts the turn back where it happened. Nothing is delayed: the
    reservation is an integer, and the write still happens off the loop.
    """
    if not settings.trace_enabled:
        return None
    return _next_sequence(session)


def open_turn(session) -> int:
    """Count one more caller turn on this call, and return its number."""
    if session is None:
        return 0
    context = session.conversation_context
    turn = int(context.get(TURN_KEY, 0)) + 1
    context[TURN_KEY] = turn
    return turn


def current_turn(session) -> int | None:
    if session is None:
        return None
    return session.conversation_context.get(TURN_KEY)


# --- writing ----------------------------------------------------------------


@_never_fails("record")
def record(
    banking_session_id: str,
    event: TraceEvent,
    *,
    session=None,
    sequence: int | None = None,
) -> None:
    """Write one trace event. Blocking; call it off the audio event loop.

    The row is anchored to the call's `agent_sessions` record, so a trace can
    never outlive the call it describes: the foreign key cascades.
    """
    anchor = _anchor(session)
    if anchor is False:
        # Known to have no call row. Costs nothing to skip.
        return

    # A position reserved when the turn was ruled keeps the turn ahead of the
    # tool it caused; anything else is numbered as it is written.
    if sequence is None:
        sequence = _next_sequence(session)
    now = _now()

    with session_scope() as db:
        if anchor is None:
            record_row = db.scalars(
                select(AgentSession).where(
                    AgentSession.banking_session_id == banking_session_id
                )
            ).first()
            anchor = (
                (record_row.id, record_row.agent_session_id, record_row.provider_call_id)
                if record_row is not None
                else False
            )
            _remember_anchor(session, anchor)

        if anchor is False:
            # No call to attach to. Nothing is invented here.
            return

        session_pk, agent_session_id, provider_call_id = anchor
        position = sequence if sequence is not None else _sequence_after(db, session_pk)

        row = CallTraceEvent(
            session_pk=session_pk,
            agent_session_id=agent_session_id,
            provider_call_id=provider_call_id,
            sequence=position,
            turn=event.turn if event.turn is not None else current_turn(session),
            created_at=now,
            kind=event.kind,
            speaker=event.speaker,
            utterance=event.utterance,
            domain=event.domain,
            intent=event.intent,
            scope_category=event.scope_category,
            scope_allowed=event.scope_allowed,
            refusal_reason=event.refusal_reason,
            pending_operation=event.pending_operation,
            account_type=event.account_type,
            loan_type=event.loan_type,
            auth_status=event.auth_status,
            customer_ref=event.customer_ref,
            tool_name=event.tool_name,
            tool_arguments=event.tool_arguments,
            tool_status=event.tool_status,
            failure_reason=event.failure_reason,
            duration_ms=event.duration_ms,
            event_type=event.event_type,
            # An ending reports the reason the bank recorded for this call, not
            # one the calling path supplied: every ending writes its reason to
            # the call row before converging on teardown, and a trace that
            # guessed would disagree with the record it sits beside.
            disconnect_reason=(
                event.disconnect_reason
                or (
                    db.scalars(
                        select(AgentSession.disconnect_reason).where(
                            AgentSession.id == session_pk
                        )
                    ).first()
                    if event.kind == KIND_LIFECYCLE
                    else None
                )
            ),
            idempotency_key=event.idempotency_key,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # This event has already been recorded. The same utterance reaches
            # the application in more than one representation and a retried
            # provider event is ordinary; the second write is a no-op, not a
            # duplicate row and not an error.
            db.rollback()
            return


# --- reading ----------------------------------------------------------------


def _row_to_dict(row: CallTraceEvent) -> dict:
    """One event, as an operator reads it. Nulls dropped for legibility."""
    payload = {
        "sequence": row.sequence,
        "turn": row.turn,
        "at": row.created_at.isoformat() if row.created_at else None,
        "kind": row.kind,
        "speaker": row.speaker,
        "utterance": row.utterance,
        "domain": row.domain,
        "intent": row.intent,
        "scope_category": row.scope_category,
        "scope_allowed": row.scope_allowed,
        "refusal_reason": row.refusal_reason,
        "pending_operation": row.pending_operation,
        "account_type": row.account_type,
        "loan_type": row.loan_type,
        "auth_status": row.auth_status,
        "customer_ref": row.customer_ref,
        "tool_name": row.tool_name,
        "tool_arguments": row.tool_arguments,
        "tool_status": row.tool_status,
        "failure_reason": row.failure_reason,
        "duration_ms": row.duration_ms,
        "event_type": row.event_type,
        "disconnect_reason": row.disconnect_reason,
    }
    return {name: value for name, value in payload.items() if value is not None}


def _summarise(events: list[dict]) -> dict:
    """What actually happened on this call, derived from its own trace.

    `agent_sessions.current_domain` and `last_intent` are deliberately the
    *last turn*, not the outcome: a refused turn shows as `GENERAL/SCOPE` so an
    operator watching the board sees that something was turned away rather than
    that a loan was discussed. That is the right answer to the question they
    ask, and it is why a call that answered a balance perfectly well and then
    heard "goodbye" ends up labelled by the goodbye.

    Rewriting those to look tidier would falsify a proven record. So the
    outcome is computed here instead, from the events, where it costs nothing
    and claims nothing the trace does not show.
    """
    banking = [
        event
        for event in events
        if event.get("kind") == KIND_TOOL
        and event.get("tool_name", "").startswith("get_")
    ]
    answered = [event for event in banking if event.get("tool_status") == "OK"]
    refused = [event for event in banking if event.get("tool_status") == "FAILED"]
    verified = any(
        event.get("auth_status") == "VERIFIED" for event in events
    )

    return {
        "verified": verified,
        "banking_enquiries": len(banking),
        "answered": len(answered),
        "refused": len(refused),
        "operations": sorted({event["tool_name"] for event in answered}),
        "refusal_reasons": sorted(
            {event["failure_reason"] for event in refused if event.get("failure_reason")}
        ),
        "turns": max(
            (event["turn"] for event in events if event.get("turn")), default=0
        ),
        "ended": next(
            (
                event.get("disconnect_reason")
                for event in reversed(events)
                if event.get("kind") == KIND_LIFECYCLE
            ),
            None,
        ),
    }


def for_call(provider_call_id: str) -> dict | None:
    """One telephone call's trace, in replay order, or None if there is none.

    Ordered by `sequence`, not by clock: two events can share a timestamp, and
    a replay that puts a tool result before its own call is worse than none.
    """
    with session_scope() as db:
        call = db.scalars(
            select(AgentSession)
            .where(AgentSession.provider_call_id == provider_call_id)
            .order_by(AgentSession.id.desc())
        ).first()
        if call is None:
            return None

        rows = list(
            db.scalars(
                select(CallTraceEvent)
                .where(CallTraceEvent.session_pk == call.id)
                .order_by(CallTraceEvent.sequence, CallTraceEvent.id)
            )
        )

        events = [_row_to_dict(row) for row in rows]
        return {
            "summary": _summarise(events),
            "call": {
                "provider_call_id": call.provider_call_id,
                "agent_session_id": call.agent_session_id,
                "channel": call.channel,
                "status": call.status,
                "auth_status": call.auth_status,
                "authenticated": call.authenticated,
                "customer_id": call.customer_id,
                "started_at": call.started_at.isoformat() if call.started_at else None,
                "ended_at": call.ended_at.isoformat() if call.ended_at else None,
                "duration_seconds": call.duration_seconds,
                "tool_call_count": call.tool_call_count,
                "disconnect_reason": call.disconnect_reason,
                "current_domain": call.current_domain,
                "last_intent": call.last_intent,
            },
            "utterances_recorded": bool(settings.telephony_trace_utterances),
            "events": events,
        }


# --- retention --------------------------------------------------------------


@_never_fails("purge_expired")
def purge_expired(*, now: datetime | None = None) -> int:
    """Delete traces older than the retention window. Returns how many went.

    A trace exists to explain a call that has just happened. Keeping it beyond
    that turns a diagnostic aid into a second transcript archive nobody decided
    to build, which is why there is no unlimited setting: `TRACE_RETENTION_DAYS`
    has a conservative default and `_positive_int` refuses zero.

    Called from the same idle sweep that reclaims abandoned calls, so there is
    no timer to supervise and the cost falls on a path that is already running.
    """
    cutoff = (now or _now()) - timedelta(days=settings.trace_retention_days)
    with session_scope() as db:
        result = db.execute(
            delete(CallTraceEvent).where(CallTraceEvent.created_at < cutoff)
        )
        return int(result.rowcount or 0)
