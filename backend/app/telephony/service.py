"""Turning a verified provider event into a call the existing bank understands.

This is the whole of Channel 2's application logic, and it is deliberately
thin. It contains no banking rules, no authentication, no authorization and no
customer lookup — it registers that a telephone call exists, takes a capacity
slot for it, and hands the same `SessionManager` session every other channel
uses to the same services every other channel uses.

The ordering of the two side effects is the load-bearing part:

    1. claim the call in the database   (atomic, cheap, authoritative)
    2. then take a capacity slot

Not the other way round. Reserving first would mean every duplicate retry
briefly consumes a slot before discovering it was a duplicate, so a provider
retrying under load could turn away real callers with capacity it was never
entitled to. Claiming first means a retry is rejected by the unique index
before it can cost anything.

What this module cannot do is as important as what it does. It never sets
`customer_id`, never marks a session authenticated, and never reads the caller
number for any purpose. A provider event establishes exactly one fact — a
telephone call exists — and the caller on it is a stranger until the same
deterministic PIN check every browser caller passes says otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.observability import recorder
from app.observability.events import AuditEvent, safe_event
from app.realtime.browser_calls import browser_call_manager
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.sessions import session_manager
from app.telephony.channels import Channel
from app.telephony.schemas import InboundCallEvent, TelephonyEventType

logger = logging.getLogger("app.telephony")


class Outcome(str, Enum):
    """What happened to an event. Reported to the operator, not to the caller."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED_CAPACITY = "rejected_capacity"
    ENDED = "ended"
    ALREADY_ENDED = "already_ended"
    UNKNOWN_CALL = "unknown_call"


@dataclass(frozen=True)
class EventResult:
    """The outcome of handling one event, with nothing customer-identifying."""

    outcome: Outcome
    provider_event_id: str
    agent_session_id: str | None = None

    @property
    def duplicate(self) -> bool:
        return self.outcome in (Outcome.DUPLICATE, Outcome.ALREADY_ENDED)


def _audit(event: AuditEvent, payload: InboundCallEvent, **fields) -> None:
    """Record one lifecycle moment, through the allow-list.

    `safe_event` drops anything not explicitly permitted, so the caller's
    number cannot reach a log line even if somebody passes it here later.
    """
    logger.info(
        "telephony event: %s",
        safe_event(
            event,
            channel=Channel.PHONE.value,
            provider_call_id=payload.provider_call_id,
            provider_event_id=payload.provider_event_id,
            **fields,
        ),
    )


async def handle_event(payload: InboundCallEvent) -> EventResult:
    """Dispatch one verified, well-formed event to its handler."""
    _audit(AuditEvent.CALL_VALIDATED, payload)

    if payload.event_type is TelephonyEventType.INCOMING:
        return await _register_incoming(payload)
    return await _end_call(payload)


async def _register_incoming(payload: InboundCallEvent) -> EventResult:
    """Admit a new telephone call, or discover that it is already admitted."""
    # A banking session must exist before capacity can be claimed for it — the
    # admission check refuses to reserve a slot for a session it cannot find.
    # It is created unauthenticated, which is the only state a provider event
    # can put it in.
    session = session_manager.create_session()

    try:
        agent_session_id = recorder.claim_phone_call(
            session.session_id,
            provider_call_id=payload.provider_call_id,
            provider_event_id=payload.provider_event_id,
        )
    except recorder.DuplicateProviderCall:
        # The retry path, and the ordinary one under a provider that retries
        # aggressively. Nothing has been consumed: no slot was taken, and the
        # session created a moment ago is thrown away.
        session_manager.destroy_session(session.session_id)
        _audit(AuditEvent.CALL_RECEIVED, payload, reason="DUPLICATE_IGNORED")
        return EventResult(Outcome.DUPLICATE, payload.provider_event_id)

    try:
        # `start` reserves a slot and registers the call in one step, the same
        # way the browser path does. Registering rather than merely reserving
        # matters for cleanup: a reservation is invisible to `close_all()` and
        # to the idle sweep, so a telephone call whose end event never arrived
        # would hold a capacity slot until the process restarted. A registered
        # call can be closed by every route that already closes calls.
        #
        # There is no media object on this side in Phase 2 — the same is true
        # of a browser call, whose audio path belongs to the page — so the
        # stand-in the manager creates is exactly the right shape. Phase 3
        # replaces it with a real SIP media session.
        await browser_call_manager.start(session.session_id)
    except RealtimeSessionError as error:
        # Nothing half-open is left behind: the claim becomes a visible
        # refusal, the session is destroyed, and no slot is held. The claim row
        # stays so the same provider call cannot be admitted by a retry that
        # arrives a moment later when a slot has freed — a caller who was told
        # the lines were busy has been told, and a second answer to the same
        # call would be a surprise, not a recovery.
        recorder.mark_phone_call_rejected(payload.provider_call_id)
        session_manager.destroy_session(session.session_id)
        reason = (
            "CAPACITY_REJECTED"
            if error.reason == Reason.REALTIME_AT_CAPACITY
            else "UNAVAILABLE"
        )
        _audit(AuditEvent.CALL_REJECTED, payload, reason=reason)
        return EventResult(
            Outcome.REJECTED_CAPACITY, payload.provider_event_id, agent_session_id
        )

    _audit(
        AuditEvent.CALL_ACCEPTED, payload, agent_session_id=agent_session_id
    )
    return EventResult(Outcome.ACCEPTED, payload.provider_event_id, agent_session_id)


async def _end_call(payload: InboundCallEvent) -> EventResult:
    """Close one telephone call and give its capacity slot back, exactly once.

    Scoped entirely by `provider_call_id`. An event naming a call that does not
    exist, or one that has already finished, changes nothing at all — it
    cannot reach another caller's session, and it cannot release a slot that
    this call does not hold.
    """
    banking_session_id = recorder.close_phone_call(
        payload.provider_call_id, reason="CUSTOMER_ENDED"
    )

    if banking_session_id is None:
        # Either unknown or already closed. Both are no-ops, and both are
        # answered the same way so that an event naming somebody else's call
        # cannot be used to discover whether that call exists.
        _audit(AuditEvent.CALL_ENDED, payload, reason="NO_OPEN_CALL")
        return EventResult(Outcome.ALREADY_ENDED, payload.provider_event_id)

    # The conditional update above returned a row, so this is the one execution
    # that closed this call, and therefore the one that may hand the slot back.
    await browser_call_manager.close(banking_session_id)
    session_manager.destroy_session(banking_session_id)

    _audit(AuditEvent.CALL_ENDED, payload, reason="CUSTOMER_ENDED")
    return EventResult(Outcome.ENDED, payload.provider_event_id)
