"""Lifecycle for realtime voice calls, keyed by banking session.

One banking session may have at most one realtime call attached to it:

    SESSION-<uuid>  (customer identity, authentication, context)
            |
            +-- REALTIME-<uuid>  (audio in, audio out)

The banking session stays the source of truth. A realtime call is an interface
onto it and nothing more, which is why closing a call never destroys the
session behind it — those are two separate lifecycles and Phase 10's End Call
button will drive them in order.

There is no module-level customer state here. Connections live in a dictionary
keyed by banking session id, guarded by a lock, in the same shape as the
existing SessionManager. Nothing about a customer is ever stored at module
level, so two calls can never see each other.

How the OpenAI connection is opened is injected (`connect`), so the lifecycle
can be tested exhaustively without a network, an API key or paid usage.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from app.realtime.context import BankingRealtimeContext
from app.realtime.events import log_event
from app.observability import business
from app.realtime.turn_gate import (
    open_turn,
    record_turn,
    record_unintelligible_turn,
)
from app.sessions import SessionManager, SessionNotFoundError
from app.sessions import session_manager as default_manager

logger = logging.getLogger("app.realtime")

# Where the per-call set of already-recorded turns lives on the session.
_RECORDED_TURNS = "_recorded_turn_items"
# The caller turn frozen at speech onset, before the model heard it.
_TURN_ANCHOR = "_trace_turn_anchor"


class RealtimeStage(str, Enum):
    """How far a model session got before it failed.

    An operator reading "connection failed" cannot tell a wrong URL from an
    expired key from a provider outage. The stage narrows it to one of four
    places before they open anything else.
    """

    CONNECTING = "CONNECTING"
    SESSION_CREATE = "SESSION_CREATE"
    SESSION_CONFIGURE = "SESSION_CONFIGURE"
    RUNNING = "RUNNING"


# Provider error codes worth telling an operator apart, because each sends them
# somewhere completely different. Matched against the close reason, which is the
# only place the provider explains itself before hanging up.
#
# `credit_balance_exhausted` is here because it cost this project a live
# deployment: the socket opened, the provider refused and closed, and the
# application reported nothing but `ConnectionClosedError`. Standard API
# connectivity checks returned 200 the whole time, so it looked like a code
# defect for as long as the reason was being discarded.
_PROVIDER_HINTS = (
    "credit_balance_exhausted",
    "insufficient_quota",
    "invalid_api_key",
    "account_deactivated",
    "model_not_found",
    "beta_api_shape_disabled",
    "rate_limit_exceeded",
)


def describe_connection_failure(error: Exception) -> str:
    """A safe one-line account of why a model session would not open.

    Carries the exception type, the WebSocket close code, and — only when it
    matches a known provider code — what the provider said. Never the URL,
    never a header, never the key: a close reason is provider text, so it is
    matched against a list rather than echoed.
    """
    parts = [type(error).__name__]

    code = getattr(error, "code", None)
    if isinstance(code, int):
        parts.append(f"close={code}")

    reason = getattr(error, "reason", None) or str(error)
    if isinstance(reason, str):
        lowered = reason.lower()
        for hint in _PROVIDER_HINTS:
            if hint in lowered:
                parts.append(f"provider={hint}")
                break

    return " ".join(parts)


class Reason:
    """Reasons a realtime lifecycle operation can fail."""

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    REALTIME_ALREADY_ACTIVE = "REALTIME_ALREADY_ACTIVE"
    REALTIME_NOT_ACTIVE = "REALTIME_NOT_ACTIVE"
    REALTIME_NOT_CONFIGURED = "REALTIME_NOT_CONFIGURED"
    REALTIME_CONNECTION_FAILED = "REALTIME_CONNECTION_FAILED"
    REALTIME_AT_CAPACITY = "REALTIME_AT_CAPACITY"


# What the caller is told. Short, and about the bank rather than about its
# suppliers: no provider name, no limit figure, no configuration, no error code.
MESSAGES = {
    Reason.SESSION_NOT_FOUND: "No active banking session with that id.",
    Reason.REALTIME_ALREADY_ACTIVE: "A voice call is already active on this session.",
    Reason.REALTIME_NOT_ACTIVE: "No voice call is active on this session.",
    Reason.REALTIME_NOT_CONFIGURED: "Realtime voice is not configured on this server.",
    Reason.REALTIME_CONNECTION_FAILED: "Could not open the voice connection.",
    Reason.REALTIME_AT_CAPACITY: (
        "Voice banking is temporarily busy. Please try again shortly."
    ),
}


class RealtimeSessionError(Exception):
    """Raised when a realtime call cannot be started, used or closed.

    Named to avoid confusion with `agents.realtime.RealtimeError`, which is an
    event type from the SDK rather than an exception. Carries a
    machine-readable reason and a message that never contains a key, a PIN or
    any provider detail.
    """

    def __init__(self, reason: str, **details) -> None:
        self.reason = reason
        self.message = MESSAGES.get(reason, "Voice call unavailable.")
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"success": False, "reason": self.reason, **self.details}


def new_realtime_session_id() -> str:
    """Return a unique realtime call identifier, e.g. REALTIME-9f4c2e78-...."""
    return f"REALTIME-{uuid.uuid4()}"


@dataclass
class RealtimeConnection:
    """One live voice call and the banking session it is bound to."""

    banking_session_id: str
    realtime_session_id: str
    session: Any
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pump: asyncio.Task | None = None

    def to_safe_dict(self) -> dict:
        """Safe view for development endpoints.

        Deliberately carries no API key, no customer identity and no audio.
        """
        return {
            "session_id": self.banking_session_id,
            "realtime_session_id": self.realtime_session_id,
            "started_at": self.started_at.isoformat(),
            "active": True,
        }


Connector = Callable[[BankingRealtimeContext], Awaitable[Any]]
EventHandler = Callable[[str, Any], Any]


def _user_text(item: Any) -> str:
    """What the caller said, from a user history item.

    Content entries carry either a transcript (spoken) or text (typed). Used to
    classify the turn and then discarded — it is never stored or logged, because
    on the authentication turns it contains the spoken PIN.
    """
    parts = []
    for entry in getattr(item, "content", None) or []:
        said = getattr(entry, "transcript", None) or getattr(entry, "text", None)
        if said:
            parts.append(said)
    return " ".join(parts)


async def open_openai_session(context: BankingRealtimeContext):
    """Open a real OpenAI Realtime session for one banking call.

    Transport is the SDK's server-side WebSocket model: this Python process
    connects to OpenAI directly. No browser and no WebRTC is involved, and the
    API key never leaves this process. Imports are local so the rest of the
    application — and the whole deterministic test suite — does not depend on
    the OpenAI SDK being importable.
    """
    from agents.realtime import RealtimeRunner

    from app.config import settings
    from app.realtime.banking_realtime import build_banking_agent, run_config

    if not settings.realtime_configured:
        raise RealtimeSessionError(Reason.REALTIME_NOT_CONFIGURED)

    runner = RealtimeRunner(starting_agent=build_banking_agent(), config=run_config())
    session = await runner.run(
        context=context,
        model_config={"api_key": settings.openai_api_key},
    )
    await session.enter()
    return session


class RealtimeManager:
    """Starts, tracks and closes realtime voice calls."""

    def __init__(
        self,
        *,
        connect: Connector | None = None,
        manager: SessionManager = default_manager,
        max_active: int | None = None,
    ) -> None:
        self._connect = connect or open_openai_session
        self._manager = manager
        self._connections: dict[str, RealtimeConnection] = {}
        # Slots claimed by callers whose provider connection is still being
        # opened. They hold capacity but are not yet callable connections.
        self._reserved: set[str] = set()
        self._lock = asyncio.Lock()
        # None means "read the configured limit each time", so an operator can
        # change REALTIME_MAX_ACTIVE_SESSIONS without rebuilding the manager.
        # An explicit value is for tests.
        self._max_active = max_active

    @property
    def max_active(self) -> int:
        """How many calls may be open at once. 0 means no limit."""
        if self._max_active is not None:
            return self._max_active
        from app.config import settings

        return settings.realtime_max_active_sessions

    def used_capacity(self) -> int:
        """Slots in use: established calls plus attempts still connecting.

        Reservations are counted because opening a provider connection takes
        seconds, and a slot that is being filled is not free. Counting only
        established connections would let every caller who arrives during that
        window pass the check.

        Banking sessions are deliberately not counted. A session with no voice
        call attached consumes no provider capacity, and one that has ended
        consumes none either.
        """
        return len(self._connections) + len(self._reserved)

    def at_capacity(self) -> bool:
        """Whether a new call would exceed the configured ceiling."""
        limit = self.max_active
        return bool(limit) and self.used_capacity() >= limit

    # --- admission --------------------------------------------------------

    def _admit(self, banking_session_id: str) -> None:
        """Claim one slot. **The lock must already be held.**

        Check and reserve happen in the same critical section, which is the
        whole point: reading the count, deciding, and taking the slot cannot be
        interleaved with another caller doing the same.
        """
        if self._manager.get_session(banking_session_id) is None:
            raise RealtimeSessionError(Reason.SESSION_NOT_FOUND)

        if banking_session_id in self._connections or (
            banking_session_id in self._reserved
        ):
            raise RealtimeSessionError(Reason.REALTIME_ALREADY_ACTIVE)

        if self.at_capacity():
            logger.warning(
                "realtime[%s] refused: at capacity (%s in use, limit %s)",
                banking_session_id,
                self.used_capacity(),
                self.max_active,
            )
            raise RealtimeSessionError(Reason.REALTIME_AT_CAPACITY)

        self._reserved.add(banking_session_id)

    async def reserve(self, banking_session_id: str) -> None:
        """Atomically claim a capacity slot before doing anything expensive.

        Callers that must spend something before connecting — the browser path
        mints a paid client secret first — reserve here, so the spend only
        happens for a caller that has actually been admitted. The reservation is
        consumed by `start(reserved=True)`, or returned by `release()`.
        """
        async with self._lock:
            self._admit(banking_session_id)

    async def release(self, banking_session_id: str) -> None:
        """Return an unused reservation. Safe to call when none is held."""
        async with self._lock:
            self._reserved.discard(banking_session_id)

    async def release_all(self) -> int:
        """Drop every outstanding reservation. Returns how many were held.

        A reservation is a slot claimed by a caller that has not finished
        connecting, so it is deliberately invisible to `close_all()`, which
        deals in live calls. That leaves reservations as the one piece of
        capacity state a shutdown or a test teardown would otherwise miss.
        """
        async with self._lock:
            count = len(self._reserved)
            self._reserved.clear()
            return count

    # --- queries ----------------------------------------------------------

    def get(self, banking_session_id: str) -> RealtimeConnection | None:
        """The live call on this banking session, or None."""
        return self._connections.get(banking_session_id)

    def is_active(self, banking_session_id: str) -> bool:
        return banking_session_id in self._connections

    def active_count(self) -> int:
        return len(self._connections)

    def active_session_ids(self) -> list[str]:
        return sorted(self._connections)

    def require(self, banking_session_id: str) -> RealtimeConnection:
        """The live call, or raise REALTIME_NOT_ACTIVE."""
        connection = self._connections.get(banking_session_id)
        if connection is None:
            raise RealtimeSessionError(Reason.REALTIME_NOT_ACTIVE)
        return connection

    # --- lifecycle --------------------------------------------------------

    async def start(
        self,
        banking_session_id: str,
        *,
        on_event: EventHandler | None = None,
        reserved: bool = False,
        connect: Connector | None = None,
    ) -> RealtimeConnection:
        """Open a voice call on an existing banking session.

        The banking session need not be authenticated: verifying the caller by
        voice is the first thing the call does. It must exist, though — a voice
        call is never the thing that creates a customer session.

        Three phases, and only the first and last hold the lock:

        1. **reserve** — check capacity and take a slot, atomically
        2. **connect** — talk to the provider, *outside* the lock
        3. **register** — convert the reservation into a live connection

        Connecting outside the lock matters: a live provider handshake takes
        several seconds, and holding the lock across it would serialise every
        caller behind the one currently connecting. Three students starting
        together would wait eight, sixteen and twenty-four seconds instead of
        eight each.

        Pass `reserved=True` if the caller already holds a slot from
        `reserve()`. Either way this method consumes the reservation: on success
        it becomes a connection, on any failure it is released.

        If the connection cannot be opened, the banking session is left exactly
        as it was: no realtime id is recorded, no state is cleared, and the slot
        is given back immediately.

        `connect` overrides how *this one call* reaches a provider, defaulting
        to the manager's own connector. That exists so both channels can share
        a single manager, and therefore a single capacity ceiling, while
        needing different things from it: a browser call registers a local
        stand-in because its audio belongs to the page, and a telephone call
        opens a real server-side model session because its audio belongs to
        this process. Giving each channel its own manager would be tidier to
        read and would split the ceiling in two, which is the one thing the
        capacity design must not do.
        """
        if not reserved:
            await self.reserve(banking_session_id)

        context = BankingRealtimeContext(
            session_id=banking_session_id, manager=self._manager
        )

        try:
            session = await (connect or self._connect)(context)
        except RealtimeSessionError:
            await self.release(banking_session_id)
            raise
        except Exception as error:
            # Nothing has been written to the banking session yet, so it is
            # already intact.
            await self.release(banking_session_id)
            logger.error(
                "realtime[%s] connection failed at %s: %s",
                banking_session_id,
                RealtimeStage.CONNECTING,
                describe_connection_failure(error),
            )
            raise RealtimeSessionError(Reason.REALTIME_CONNECTION_FAILED) from error

        connection = RealtimeConnection(
            banking_session_id=banking_session_id,
            realtime_session_id=new_realtime_session_id(),
            session=session,
        )

        async with self._lock:
            # The slot stops being a reservation and starts being a call. Both
            # are counted, so capacity never dips between the two.
            self._reserved.discard(banking_session_id)

            try:
                self._manager.update_session(
                    banking_session_id,
                    realtime_session_id=connection.realtime_session_id,
                )
            except SessionNotFoundError as error:
                # The call ended while the connection was being opened.
                await self._shutdown(connection)
                raise RealtimeSessionError(Reason.SESSION_NOT_FOUND) from error

            self._connections[banking_session_id] = connection

            # Pumped whenever there is a stream to pump, handler or not: the
            # scope gate is fed from this stream, so it must run even when
            # nobody is watching the events.
            #
            # A browser call has no stream on this side — its audio and its
            # events belong to the page's own peer connection, and its turns are
            # classified through POST /api/call/scope. Starting a pump on one
            # would spawn a task per call that could only fail.
            if hasattr(session, "__aiter__"):
                connection.pump = asyncio.create_task(
                    self._pump_events(connection, on_event)
                )

            logger.info(
                "realtime[%s] started as %s",
                banking_session_id,
                connection.realtime_session_id,
            )
            return connection

    async def close(self, banking_session_id: str) -> bool:
        """End the voice call, leaving the banking session in place.

        Returns False if there was no call, so ending one twice is harmless.
        This never destroys the customer session and never touches any other
        session's connection.
        """
        async with self._lock:
            connection = self._connections.pop(banking_session_id, None)
            if connection is None:
                return False

            await self._shutdown(connection)

            try:
                self._manager.update_session(
                    banking_session_id, realtime_session_id=None
                )
            except SessionNotFoundError:
                # The banking session has already ended. Nothing to clear.
                pass

            logger.info(
                "realtime[%s] closed %s",
                banking_session_id,
                connection.realtime_session_id,
            )
            return True

    async def close_all(self) -> int:
        """Close every live call. Used on shutdown and by tests."""
        count = 0
        for banking_session_id in list(self._connections):
            if await self.close(banking_session_id):
                count += 1
        return count

    # --- conversation -----------------------------------------------------

    async def send_audio(self, banking_session_id: str, audio: bytes) -> None:
        """Send one chunk of caller audio (PCM16) into the call."""
        connection = self.require(banking_session_id)
        await connection.session.send_audio(audio)

    async def send_message(self, banking_session_id: str, text: str) -> None:
        """Send a text message into the call, mainly for development checks."""
        connection = self.require(banking_session_id)
        await connection.session.send_message(text)

    async def interrupt(self, banking_session_id: str) -> None:
        """Stop the assistant's current spoken response.

        Ordinary barge-in is handled by the model's own semantic VAD; this is
        for an explicit interruption such as a Mute or Stop control.
        """
        connection = self.require(banking_session_id)
        await connection.session.interrupt()

    # --- internals --------------------------------------------------------

    async def _pump_events(
        self, connection: RealtimeConnection, on_event: EventHandler | None
    ) -> None:
        """Drive the scope gate from the event stream, and forward events on.

        The pump runs whether or not anyone passed a handler, because the gate
        depends on it: if events were only drained when a caller wanted to watch
        them, a call with no observer would answer banking questions with no
        turn ever classified.
        """
        try:
            async for event in connection.session:
                log_event(connection.banking_session_id, event)

                turn = self._feed_gate(connection.banking_session_id, event)
                if turn is not None:
                    # Off this loop. `to_thread` hands the write to a worker,
                    # so the audio pumps sharing this loop keep running while
                    # PostgreSQL is busy. Awaited rather than left to run free:
                    # a task nobody holds can be collected mid-write, and on
                    # teardown the pump's cancellation would abandon it. Awaited
                    # here, a cancel arrives at this point *after* the worker
                    # has been handed the work, so the row is still written.
                    await asyncio.to_thread(business.record_turn_decision, *turn)

                if on_event is None:
                    continue
                result = on_event(connection.banking_session_id, event)
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A failed pump must not take down the banking session.
            logger.error(
                "realtime[%s] event stream ended: %s",
                connection.banking_session_id,
                type(error).__name__,
            )

    def _feed_gate(self, banking_session_id: str, event: Any):
        """Open and rule on caller turns as the events for them arrive.

        Two signals matter, and nothing else here does:

        * the caller starts speaking — the previous turn's ruling stops applying
        * the caller's transcript is ready — this turn is classified

        Text turns carry no audio and no transcription, so a user history item
        is classified directly. A failure here must never break the call, but it
        must also never quietly open the gate, so the turn is left pending and
        the tools refuse.

        Stays synchronous, and stays free of I/O. The ruling has to be in force
        before the next tool call is admitted, so it cannot be deferred — which
        is exactly why nothing slow may happen here. What the operations record
        needs is *returned* instead, for the pump to persist off this loop.

        Returns `(session, decision)` when this event produced a turn worth
        recording, or None. The same utterance reaches this method in more than
        one representation, so the return is deduplicated per conversation item:
        the ruling is applied every time, the row is written once.
        """
        session = self._manager.get_session(banking_session_id)
        if session is None:
            return None

        try:
            kind = getattr(event, "type", "")

            if kind == "raw_model_event":
                data = getattr(event, "data", None)
                raw_type = getattr(data, "type", None)
                if raw_type == "input_audio_transcription_completed":
                    text = getattr(data, "transcript", "") or ""
                    if not text.strip():
                        # Heard, and empty. Resolve the turn rather than leaving
                        # it open: `open_turn` has no other closer, so a turn
                        # left pending here stays pending for the whole call.
                        record_unintelligible_turn(session)
                        return None
                    decision = record_turn(session, text)
                    return self._turn_to_record(session, data, text, decision)

                inner = getattr(data, "data", None)
                if not isinstance(inner, dict):
                    return None

                raw_kind = inner.get("type")
                if raw_kind == "input_audio_buffer.speech_started":
                    open_turn(session)
                    # The caller has started talking and the model has not yet
                    # heard them, so nothing can have acted on this turn. That
                    # makes this the only instant at which the trace can ask
                    # what the bank was waiting for and get an answer the rest
                    # of the turn cannot contradict. See `trace.anchor_turn`.
                    from app.observability import trace

                    session.conversation_context[_TURN_ANCHOR] = trace.anchor_turn(
                        session
                    )
                elif raw_kind == (
                    "conversation.item.input_audio_transcription.failed"
                ):
                    # The provider tried to transcribe and could not. There is
                    # no typed SDK event for this — only `.completed` is mapped
                    # — so without reading the raw form the turn this opened is
                    # never closed by anything.
                    record_unintelligible_turn(session)
                return None

            if kind == "history_added":
                item = getattr(event, "item", None)
                if getattr(item, "role", None) == "user":
                    text = _user_text(item)
                    if text:
                        decision = record_turn(session, text)
                        return self._turn_to_record(session, item, text, decision)
        except Exception as error:
            logger.error(
                "realtime[%s] scope gate could not read an event: %s",
                banking_session_id,
                type(error).__name__,
            )
        return None

    @staticmethod
    def _turn_to_record(session, carrier: Any, text: str, decision):
        """Whether this turn still needs writing down, keyed by its item.

        The transcription event and the history item for one utterance carry
        the same `item_id`, so that is the key: the second representation to
        arrive finds the turn already recorded and asks for nothing. Keyed on
        the id rather than on the words, because two identical questions asked
        on different turns are two turns and must both be recorded.

        The seen-set lives on the banking session, so it is bounded by the
        length of one call and disappears with it.
        """
        if decision is None:
            return None

        item_id = getattr(carrier, "item_id", None)
        if not item_id:
            # No id to key on — fall back to the words, which at least stops a
            # single utterance being written twice within one turn.
            item_id = f"turn{getattr(session, 'turn_counter', '')}:{hash(text)}"

        seen = session.conversation_context.setdefault(_RECORDED_TURNS, set())
        if item_id in seen:
            return None
        seen.add(item_id)
        # Taken when the caller started speaking, before the model could reach
        # for anything. Consumed here so a turn uses its own anchor and no
        # other.
        anchor = session.conversation_context.pop(_TURN_ANCHOR, None)
        # The words travel with the ruling, for the trace. Whether they are
        # written down at all is `app.observability.trace`'s decision, not
        # this layer's - it only stops them being unavailable.
        #
        # The replay position and the credential expectation both come from the
        # anchor taken at speech onset where there is one, so the caller's words
        # keep their place ahead of the tool they cause even when the model
        # acted before the transcript came back.
        from app.observability import trace

        return session, decision, text, trace.resolve_turn(session, decision, anchor)

    async def _shutdown(self, connection: RealtimeConnection) -> None:
        """Release provider resources for one call, tolerating failures."""
        if connection.pump is not None:
            connection.pump.cancel()
            try:
                await connection.pump
            except (asyncio.CancelledError, Exception):
                pass
            connection.pump = None

        try:
            await connection.session.close()
        except Exception as error:
            logger.error(
                "realtime[%s] close failed: %s",
                connection.banking_session_id,
                type(error).__name__,
            )


# There is deliberately no module-level manager here.
#
# There used to be, from when the development router was its only consumer.
# `browser_calls` later created its own for the browser connector, and when the
# telephone channel arrived that one was named `voice_call_manager` and made the
# application-wide ceiling. The original was never retired, and the development
# router went on using it - so two independent counters each read
# `REALTIME_MAX_ACTIVE_SESSIONS` and the process would hold twice the configured
# number of provider sessions, while readiness reported one of the two.
#
# A capacity ceiling only means anything if there is one of it. The single
# authoritative manager is `app.realtime.browser_calls.voice_call_manager`;
# every route that can open a provider session goes through that one.
#
# Importing it here would be circular - `browser_calls` imports this module for
# the class - so it is not re-exported. Ask for it where it lives.
