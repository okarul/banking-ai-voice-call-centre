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

import asyncio
import time

from app.config import settings
from app.observability import recorder
from app.observability.events import AuditEvent, safe_event
from app.realtime.browser_calls import voice_call_manager
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge, phone_call_registry
from app.telephony.channels import Channel
from app.telephony.media import LoopbackMediaTransport, WebSocketMediaTransport
from app.telephony.schemas import InboundCallEvent, TelephonyEventType

logger = logging.getLogger("app.telephony")


async def open_phone_realtime_session(context):
    """Open the model session that will talk to one telephone caller.

    A thin named seam over the existing server-side connector. It exists so the
    phone path has one place to be substituted in tests, and so the browser
    path's connector can never be reached for a telephone call by accident.
    """
    from app.realtime.realtime_manager import open_openai_session

    return await open_openai_session(context)


def build_transport():
    """The media transport for one call, chosen by configuration.

    Provider specifics stop here. Everything above this line deals in µ-law
    frames and knows nothing about sockets, gateways or SIP.
    """
    frames = settings.telephony_audio_queue_frames
    if settings.telephony_media_transport == "loopback":
        return LoopbackMediaTransport(max_frames=frames)
    return WebSocketMediaTransport(max_frames=frames)


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


async def _on_call_lost(provider_call_id: str, banking_session_id: str) -> None:
    """A call whose media or model failed underneath it.

    Converges on the same teardown as every other ending, so a dropped model
    session releases its capacity slot immediately rather than waiting minutes
    for the idle sweep to notice a silent call.
    """
    logger.warning("telephony call lost: %s", provider_call_id)
    await tear_down(provider_call_id, banking_session_id)
    recorder.close_phone_call(provider_call_id, reason="PROVIDER_FAILURE")


async def _open_conversation(bridge: PhoneCallBridge) -> None:
    """Wait for the caller's audio path, then have the agent say hello.

    Two jobs that have to happen in this order, which is why they are one task
    rather than two racing ones:

    1. **Wait for the media to be ready.** A WebSocket transport exists from
       the moment the call is registered, but the gateway attaches its socket a
       moment later, and anything sent before that is discarded. Greeting on
       registration would mean greeting into a socket nobody is holding.
    2. **Greet.** Otherwise the caller connects successfully and hears silence
       until they speak first, which is not how a bank answers the telephone.

    If the media never arrives the call is given up rather than left holding a
    capacity slot — one gateway failure must not cost a five-slot bank a slot
    for the length of the idle timeout.
    """
    ready = await bridge.transport.wait_until_ready(
        settings.telephony_media_connect_timeout
    )

    if not ready:
        if bridge.closed:
            # The call ended by itself while we waited. Nothing to give up on.
            return
        logger.warning("telephony media never attached: %s", bridge.provider_call_id)
        await tear_down(bridge.provider_call_id, bridge.banking_session_id)
        recorder.mark_phone_call_rejected(
            bridge.provider_call_id, reason="MEDIA_ATTACH_TIMEOUT"
        )
        return

    await bridge.greet()


async def sweep_idle_calls() -> int:
    """Close calls that have carried no audio for the configured timeout.

    The backstop for the ending that never arrives: a provider that forgets to
    send an `ended` event, a gateway that vanishes without closing its socket.
    Neither is exotic, and both would otherwise hold a capacity slot until the
    process restarted.

    Run on each new call rather than on a timer, matching how the browser
    channel sweeps: no background loop to supervise, and the check happens at
    the only moment a leaked slot actually costs anybody anything.
    """
    timeout = settings.telephony_idle_call_timeout
    if not timeout:
        return 0

    now = time.monotonic()
    closed = 0
    for bridge in phone_call_registry.all_bridges():
        if now - bridge.last_activity < timeout:
            continue
        logger.info("telephony call idle, closing: %s", bridge.provider_call_id)
        await tear_down(bridge.provider_call_id, bridge.banking_session_id)
        recorder.close_phone_call(bridge.provider_call_id, reason="SILENCE_TIMEOUT")
        closed += 1
    return closed


async def _register_incoming(payload: InboundCallEvent) -> EventResult:
    """Admit a new telephone call, or discover that it is already admitted."""
    # Reclaim anything abandoned before deciding this caller cannot be served.
    await sweep_idle_calls()

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

    # The media path and the model session, built for this call and reachable
    # from nothing else. The bridge is created before the model session because
    # the session needs the bridge's event handler: that is what routes this
    # caller's assistant audio to this caller's queue and no other.
    bridge = PhoneCallBridge(
        provider_call_id=payload.provider_call_id,
        banking_session_id=session.session_id,
        transport=build_transport(),
        realtime_manager=voice_call_manager,
        outbound_max_frames=settings.telephony_audio_queue_frames,
        on_call_lost=_on_call_lost,
    )

    try:
        # `start` reserves a capacity slot and opens the model session in one
        # step, on the single application-wide manager, so a telephone call
        # consumes the same slot a browser call would. `connect` gives this call
        # a real server-side session where a browser call needs only a stand-in;
        # `on_event` binds the model's output to this bridge.
        #
        # Registering rather than merely reserving matters for cleanup: a
        # reservation is invisible to `close_all()` and to the idle sweep, so a
        # call whose end event never arrived would hold a slot until restart.
        await asyncio.wait_for(
            voice_call_manager.start(
                session.session_id,
                on_event=bridge.on_realtime_event,
                connect=open_phone_realtime_session,
            ),
            timeout=settings.telephony_realtime_connect_timeout,
        )
    except Exception as error:
        # Nothing half-open is left behind: the claim becomes a visible
        # refusal, the session is destroyed, and no slot is held. The claim row
        # stays so the same provider call cannot be admitted by a retry that
        # arrives a moment later when a slot has freed — a caller who was told
        # the lines were busy has been told, and a second answer to the same
        # call would be a surprise, not a recovery.
        # Unwind everything this attempt created, in the reverse order it was
        # created. `start` releases the capacity slot on any failure of its own,
        # so what is left here is the bridge, the session and the claim row.
        await bridge.close()
        await voice_call_manager.release(session.session_id)
        session_manager.destroy_session(session.session_id)

        at_capacity = (
            isinstance(error, RealtimeSessionError)
            and error.reason == Reason.REALTIME_AT_CAPACITY
        )
        if at_capacity:
            reason = "CAPACITY_REJECTED"
        elif isinstance(error, asyncio.TimeoutError):
            reason = "REALTIME_TIMEOUT"
        else:
            reason = "UNAVAILABLE"
        if not at_capacity:
            # Type only. A provider or SDK message may carry request detail.
            logger.error("telephony call could not start: %s", type(error).__name__)
        # Recorded with the reason that actually applied. Defaulting this to
        # CAPACITY_REJECTED told an operator the bank was full when the real
        # cause was a model session that would not open — which is the opposite
        # of the diagnosis, and would send them to look at the wrong thing.
        recorder.mark_phone_call_rejected(payload.provider_call_id, reason=reason)
        _audit(AuditEvent.CALL_REJECTED, payload, reason=reason)
        return EventResult(
            Outcome.REJECTED_CAPACITY, payload.provider_event_id, agent_session_id
        )

    # Registered only once the call is fully built. A bridge in the registry is
    # a call that can carry audio, so putting it there any earlier would let a
    # media socket attach to a call whose model session had not opened.
    if not await phone_call_registry.register(bridge):
        # Another event built this call between the database claim and here.
        # The claim makes that all but impossible; unwinding anyway is cheaper
        # than reasoning about whether "all but" is good enough.
        await bridge.close()
        await voice_call_manager.close(session.session_id)
        session_manager.destroy_session(session.session_id)
        _audit(AuditEvent.CALL_RECEIVED, payload, reason="DUPLICATE_IGNORED")
        return EventResult(Outcome.DUPLICATE, payload.provider_event_id)

    try:
        await bridge.start()
    except Exception as error:
        await tear_down(payload.provider_call_id, session.session_id)
        recorder.mark_phone_call_rejected(
            payload.provider_call_id, reason="MEDIA_UNAVAILABLE"
        )
        logger.error("telephony media failed to start: %s", type(error).__name__)
        _audit(AuditEvent.CALL_REJECTED, payload, reason="MEDIA_UNAVAILABLE")
        return EventResult(
            Outcome.REJECTED_CAPACITY, payload.provider_event_id, agent_session_id
        )

    # Waits for the caller's audio path, then greets them. Exits by itself when
    # the call ends, because ending the transport marks it ready.
    asyncio.ensure_future(_open_conversation(bridge))

    _audit(AuditEvent.CALL_ACCEPTED, payload, agent_session_id=agent_session_id)
    return EventResult(Outcome.ACCEPTED, payload.provider_event_id, agent_session_id)


async def tear_down(provider_call_id: str, banking_session_id: str) -> None:
    """Release everything one call holds, in any state, more than once safely.

    The single place a call's resources are freed, because a call can end from
    several directions at once — the caller hangs up, the provider sends an
    event, the model session drops, a timeout fires — and every one of them has
    to converge here rather than each releasing a different subset.

    Deliberately *not* released: the persistent PIN lockout. That is customer
    security state and outlives the call by design. Clearing it on hang-up
    would make hanging up the way to reset it, which is the exact loop the
    lockout was built to close.
    """
    bridge = await phone_call_registry.remove(provider_call_id)
    if bridge is not None:
        await bridge.close()

    # `close` returns the capacity slot and returns False when there was
    # nothing to close, so arriving here twice cannot release two slots. The
    # `release` is for a call that failed before it became a connection.
    await voice_call_manager.close(banking_session_id)
    await voice_call_manager.release(banking_session_id)
    session_manager.destroy_session(banking_session_id)


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
    await tear_down(payload.provider_call_id, banking_session_id)

    _audit(AuditEvent.CALL_ENDED, payload, reason="CUSTOMER_ENDED")
    return EventResult(Outcome.ENDED, payload.provider_event_id)
