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
from app.telephony import reasons
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
    # A model session that would not open is not a busy switchboard. Reported
    # separately because it sends an operator somewhere completely different:
    # capacity means wait, this means look at the provider. Live, a provider
    # refusing every session was logged by the gateway as `rejected_capacity`,
    # and the one true fact — that nothing was wrong with capacity at all —
    # never reached anybody.
    REJECTED_REALTIME_UNAVAILABLE = "rejected_realtime_unavailable"
    # And the audio path failing is not a busy switchboard either. Same
    # reasoning as above, applied to the other supplier: an operator reading
    # this needs to know whether to look at the model provider or the gateway.
    REJECTED_MEDIA_UNAVAILABLE = "rejected_media_unavailable"
    ENDED = "ended"
    ALREADY_ENDED = "already_ended"
    UNKNOWN_CALL = "unknown_call"


@dataclass(frozen=True)
class EventResult:
    """The outcome of handling one event, with nothing customer-identifying."""

    outcome: Outcome
    provider_event_id: str
    agent_session_id: str | None = None
    # Present only on acceptance. Never on a duplicate or a refusal: those have
    # no call to attach audio to, so they are given no way to try.
    media_token: str | None = None

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


async def _on_call_ended(
    provider_call_id: str, banking_session_id: str, reason: str
) -> None:
    """The conversation finished, and the closing line has been heard.

    Distinct from `_on_call_lost`, which is a failure. This is a call that ran
    to a proper end — the caller said goodbye and the bank answered, or the
    caller fell silent and was told so. Either way the line has played out
    before anything is taken down, which is the whole point of waiting.
    """
    recorded = reasons.for_end_reason(reason)
    logger.info(
        "telephony call ended: %s (%s -> %s)", provider_call_id, reason, recorded
    )
    # Recorded *before* the teardown, and the order is load-bearing. Tearing
    # down closes the media socket, which is exactly what wakes the media
    # route's `finally` — and that writes `CALLER_HANGUP`. Both writers move
    # the row `WHERE ended_at IS NULL`, so whichever arrives first wins for
    # ever. Reversed, a goodbye would be recorded as a hang-up whenever the
    # loop happened to schedule the route first, and an operator would be told
    # the caller rang off in the middle of the bank's own closing line.
    # This call is synchronous, so nothing can interleave before it commits.
    recorder.close_phone_call(provider_call_id, reason=recorded)
    await tear_down(provider_call_id, banking_session_id)


async def _on_call_lost(
    provider_call_id: str,
    banking_session_id: str,
    cause: str = reasons.MEDIA_FAILURE,
) -> None:
    """A call whose media or model failed underneath it.

    Converges on the same teardown as every other ending, so a dropped model
    session releases its capacity slot immediately rather than waiting minutes
    for the idle sweep to notice a silent call.
    """
    logger.warning("telephony call lost: %s (%s)", provider_call_id, cause)
    # Before the teardown, for the reason given in `_on_call_ended`: the
    # teardown wakes the media route, and the route would otherwise record
    # this failure as a caller hang-up.
    recorder.close_phone_call(provider_call_id, reason=cause)
    await tear_down(provider_call_id, banking_session_id)


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
        # Recorded before the teardown. No socket ever attached here, so there
        # is no route to race with today — but the ordering is the same at
        # every site so that none of them has to be reasoned about separately.
        recorder.mark_phone_call_rejected(
            bridge.provider_call_id, reason=reasons.MEDIA_ATTACH_TIMEOUT
        )
        await tear_down(bridge.provider_call_id, bridge.banking_session_id)
        return

    # The socket is attached, but attached is not compatible. A gateway that
    # cannot answer a playback boundary would leave every turn waiting for an
    # acknowledgement it never sends, and the call would hang with nothing to
    # explain it. Refuse here instead, while a refusal is still cheap and
    # legible. This is a startup timeout, not a limit on how long a call may
    # last.
    compatible = await _far_end_is_compatible(
        bridge.transport, settings.telephony_protocol_timeout
    )
    if not compatible:
        if bridge.closed:
            return
        logger.error(
            "telephony media protocol not agreed: %s (gateway version %s)",
            bridge.provider_call_id,
            getattr(bridge.transport, "peer_version", None),
        )
        # Before the teardown, and here it matters most of all: a socket *is*
        # attached, so the route is live and blocked on `receive()`. Tearing
        # down first would let it record `CUSTOMER_ENDED` and flip the row from
        # REJECTED to COMPLETED — a refused call reported as a finished one,
        # which is the single most misleading thing this table could say.
        recorder.mark_phone_call_rejected(
            bridge.provider_call_id, reason=reasons.PROTOCOL_MISMATCH
        )
        await tear_down(bridge.provider_call_id, bridge.banking_session_id)
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
        # Recorded before the teardown, like every other ending. A gateway that
        # vanished without closing its socket leaves the route still attached,
        # so tearing down first would wake it and let `CUSTOMER_ENDED` land on
        # a call that no caller was on — which is the one thing an idle sweep
        # exists to be able to say.
        recorder.close_phone_call(
            bridge.provider_call_id, reason=reasons.IDLE_TIMEOUT
        )
        await tear_down(bridge.provider_call_id, bridge.banking_session_id)
        closed += 1
    return closed


async def _far_end_is_compatible(transport, timeout: float) -> bool:
    """Whether this transport's far end agreed a protocol we can work with.

    A transport with nothing on the other side to negotiate with — the loopback
    used in tests and local development — is compatible by definition, and one
    that predates negotiation entirely is treated the same way rather than
    refused.
    """
    wait = getattr(transport, "wait_for_protocol", None)
    if wait is None:
        return True
    return await wait(timeout)


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
        on_call_ended=_on_call_ended,
        # The credential outlives nothing: it expires with the window the
        # gateway has to attach, so a token that leaks is useless seconds later.
        media_token_ttl=settings.telephony_media_connect_timeout,
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
            reason = reasons.CAPACITY_REJECTED
        elif isinstance(error, asyncio.TimeoutError):
            reason = reasons.REALTIME_TIMEOUT
        else:
            reason = reasons.REALTIME_START_FAILURE
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
            Outcome.REJECTED_CAPACITY
            if at_capacity
            else Outcome.REJECTED_REALTIME_UNAVAILABLE,
            payload.provider_event_id,
            agent_session_id,
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
        # Recorded before the teardown, like every other refusal path.
        recorder.mark_phone_call_rejected(
            payload.provider_call_id, reason=reasons.MEDIA_UNAVAILABLE
        )
        await tear_down(payload.provider_call_id, session.session_id)
        logger.error("telephony media failed to start: %s", type(error).__name__)
        _audit(
            AuditEvent.CALL_REJECTED, payload, reason=reasons.MEDIA_UNAVAILABLE
        )
        # Not `REJECTED_CAPACITY`. The audio path failing has nothing to do
        # with how many calls are in progress, and reporting it as a full
        # switchboard is the same mistake this phase fixed on the realtime
        # path: it sends an operator to look at capacity while the database
        # row says the media failed.
        return EventResult(
            Outcome.REJECTED_MEDIA_UNAVAILABLE,
            payload.provider_event_id,
            agent_session_id,
        )

    # Waits for the caller's audio path, then greets them. Exits by itself when
    # the call ends, because ending the transport marks it ready.
    asyncio.ensure_future(_open_conversation(bridge))

    _audit(AuditEvent.CALL_ACCEPTED, payload, agent_session_id=agent_session_id)
    return EventResult(
        Outcome.ACCEPTED,
        payload.provider_event_id,
        agent_session_id,
        media_token=bridge.media_token,
    )


# Teardowns still running. A task referenced by nothing can be collected
# mid-release, which would reintroduce exactly the leak below by a second
# route, so each one is held until it finishes.
_releasing: set[asyncio.Task] = set()


async def tear_down(provider_call_id: str, banking_session_id: str) -> None:
    """Release everything one call holds, in any state, more than once safely.

    The single place a call's resources are freed, because a call can end from
    several directions at once — the caller hangs up, the provider sends an
    event, the model session drops, a timeout fires — and every one of them has
    to converge here rather than each releasing a different subset.

    **Shielded, because the thing that ends a call also cancels the task that
    has to clean up after it.** This runs from a `finally` in the media socket
    route, and from bridge tasks that a closing call is itself cancelling. A
    cancellation delivered while this coroutine is between two of its own
    `await`s used to abort it part-way through — reliably after the bridge had
    been taken out of the registry and reliably before the model session was
    closed. What that left behind was the worst possible half:

    * the provider session stayed open, billing and holding a connection;
    * its capacity slot was never returned, so `used_capacity()` never came
      back down;
    * and the idle sweep could not reclaim either, because the sweep walks
      `phone_call_registry` and the bridge was already gone from it.

    On a deployment with `REALTIME_MAX_ACTIVE_SESSIONS = 1` that is one dropped
    socket away from every subsequent caller being told the bank is full, until
    the process is restarted.

    So the release runs as its own task and this call merely *waits* for it.
    Cancelling the waiter — which is what a hang-up does — stops the waiting,
    not the releasing. Nothing here awaits the caller's task in turn, so a
    bridge closing its own pumps still completes: the pump's wait is cancelled,
    the release carries on.

    Deliberately *not* released: the persistent PIN lockout. That is customer
    security state and outlives the call by design. Clearing it on hang-up
    would make hanging up the way to reset it, which is the exact loop the
    lockout was built to close.
    """
    await _uninterruptible(
        _release_everything(provider_call_id, banking_session_id)
    )


async def end_media_call(provider_call_id: str, banking_session_id: str) -> None:
    """The media socket route's whole cleanup, as one indivisible thing.

    Called from that route's `finally`, which is reached exactly when the
    caller has gone — and often while the route's own task is being cancelled
    for the same reason. Releasing the call and recording that it ended are two
    steps of one ending, so they are shielded together: shielding only the
    first left a released call whose row stayed `ACTIVE` with no `ended_at`,
    which is a live call on the operations board and a finished one everywhere
    else.
    """
    await _uninterruptible(
        _release_and_record(provider_call_id, banking_session_id)
    )


async def _uninterruptible(work) -> None:
    """Run `work` to completion, whatever happens to the caller waiting on it.

    The work becomes its own task, so cancelling the waiter stops the waiting
    rather than the work. The task is held in `_releasing` because a task
    referenced by nothing can be collected mid-flight, which would reintroduce
    the same leak by a second route.
    """
    task = asyncio.ensure_future(work)
    _releasing.add(task)
    task.add_done_callback(_releasing.discard)
    await asyncio.shield(task)


async def _release_and_record(
    provider_call_id: str, banking_session_id: str
) -> None:
    """Release the call, then write down that the caller hung up."""
    await _release_everything(provider_call_id, banking_session_id)
    try:
        # Only recorded if nothing else closed this call first — the update
        # moves the row `WHERE ended_at IS NULL`. So a goodbye, a silence close
        # or a failure keeps its own reason, and a socket closing is read as a
        # caller hang-up only when it genuinely was one.
        recorder.close_phone_call(provider_call_id, reason=reasons.CALLER_HANGUP)
    except Exception as error:
        # The call is released either way, and an observability failure must
        # not replace whatever actually ended it.
        logger.error("telephony call record not closed: %s", type(error).__name__)


async def _release_everything(
    provider_call_id: str, banking_session_id: str
) -> None:
    """Hand back every resource one call holds. Never raises.

    Each release is attempted independently. They were a straight sequence, and
    a straight sequence has the same shape of fault as the cancellation above:
    one step failing strands every step after it, and the ones after it are the
    provider session and its capacity slot. A bridge that cannot close is not a
    reason to keep paying for a model session nobody is listening to.
    """
    bridge = await _released("bridge", phone_call_registry.remove(provider_call_id))
    if bridge is not None:
        await _released("media", bridge.close())

    # `close` returns the capacity slot and returns False when there was
    # nothing to close, so arriving here twice cannot release two slots. The
    # `release` is for a call that failed before it became a connection.
    await _released("realtime", voice_call_manager.close(banking_session_id))
    await _released("reservation", voice_call_manager.release(banking_session_id))
    session_manager.destroy_session(banking_session_id)


async def _released(what: str, release):
    """Await one release step, reporting a failure instead of propagating it.

    `CancelledError` derives from `BaseException` and is deliberately not
    caught: this runs inside the shielded task, so the only cancellation that
    can reach it is the loop shutting down, and that is not a moment to keep
    going.
    """
    try:
        return await release
    except Exception as error:
        logger.error(
            "telephony %s not released: %s", what, type(error).__name__
        )
        return None


async def _end_call(payload: InboundCallEvent) -> EventResult:
    """Close one telephone call and give its capacity slot back, exactly once.

    Scoped entirely by `provider_call_id`. An event naming a call that does not
    exist, or one that has already finished, changes nothing at all — it
    cannot reach another caller's session, and it cannot release a slot that
    this call does not hold.
    """
    # `PROVIDER_ENDED`, not `CUSTOMER_ENDED`. This handler runs on the
    # provider's `ended` webhook — the carrier telling us the call is over —
    # which is precisely what `PROVIDER_HANGUP` was defined to mean and, until
    # now, nothing wrote. A caller who physically hangs up is still recorded as
    # `CALLER_HANGUP`, because the media socket closes the moment the SIP BYE
    # lands and that path reaches the recorder first; the `WHERE ended_at IS
    # NULL` predicate makes whichever noticed first the one that stands.
    banking_session_id = recorder.close_phone_call(
        payload.provider_call_id, reason=reasons.PROVIDER_HANGUP
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

    _audit(AuditEvent.CALL_ENDED, payload, reason=reasons.PROVIDER_HANGUP)
    return EventResult(Outcome.ENDED, payload.provider_event_id)
