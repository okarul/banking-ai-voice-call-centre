"""One telephone call, wired to one model session, and to nothing else.

A bridge owns everything that belongs to a single call:

    provider_call_id -> banking_session_id -> media transport
                                           -> two pump tasks
                                           -> one outbound audio queue

and it owns *nothing* that belongs to any other call. That is the entire
security property of this module, and it is structural rather than careful:
there is no dictionary of calls here, no "current call", no module-level state
a second caller could reach. Two callers get two objects, and an object has no
route to its sibling.

The registry that does hold them all is `PhoneCallRegistry`, and it is a keyed
container, not shared state: entries are added and removed under a lock, and
nothing reads another entry's fields.

**The two pumps.** A call needs audio moving in both directions at once, so
each bridge runs two tasks:

    caller -> model    read a frame, convert it, hand it to this call's session
    model  -> caller   take queued assistant audio, convert it, send it back

They are per-bridge, so a call whose model is slow blocks its own pump and
nobody else's. A single shared pump over all calls would be smaller code and
would make one stalled caller everybody's problem.

**Where assistant audio comes from.** Not from polling. `RealtimeManager.start`
takes an `on_event` handler, and the bridge passes one bound to itself — so the
model's audio events for this call are appended to this call's queue by a
closure that has no name for any other queue.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.telephony import audio as codec
from app.telephony.media import BoundedAudioQueue, MediaTransport

logger = logging.getLogger("app.telephony.bridge")

# What is sent into the session to make the agent open the call.
#
# The wording of the greeting itself is **not** here — it lives in the agent's
# instructions (`app.agents.banking_realtime`), which is the one place that
# decides how this bank speaks. This is only the cue that a caller is now on
# the line, playing the part the browser plays when it sends `response.create`.
#
# The Agents SDK has no public way to request a bare response, so the cue is
# delivered as a short user turn, which is the supported path. Two consequences
# are worth being explicit about:
#
#   * The scope gate classifies it, as it classifies any user turn, and will
#     rule it out of scope. That is harmless and asserted by test: no tool can
#     run between the greeting and the caller's first words, and that first
#     real utterance re-rules the gate.
#   * It is not a transcript of anything the caller said. The phone channel
#     persists no transcript today; when it does, this cue must be excluded.
GREETING_CUE = "Hello?"


class PhoneCallBridge:
    """The live audio path for exactly one telephone call."""

    def __init__(
        self,
        *,
        provider_call_id: str,
        banking_session_id: str,
        transport: MediaTransport,
        realtime_manager,
        outbound_max_frames: int,
        on_call_lost=None,
    ) -> None:
        self.provider_call_id = provider_call_id
        self.banking_session_id = banking_session_id
        self.transport = transport
        self._realtime = realtime_manager

        # Assistant audio waiting to be played to this caller. Bounded: a model
        # that generates faster than a telephone can play must not be able to
        # grow this without limit.
        self.outbound = BoundedAudioQueue(
            max_frames=outbound_max_frames, name=f"out-{provider_call_id}"
        )

        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._close_lock = asyncio.Lock()

        # Called when a pump stops for a reason that is not this call ending
        # tidily — the model session dropped, the transport failed. Without it
        # the caller would sit in silence holding a capacity slot until the
        # idle sweep noticed, which is minutes away.
        self._on_call_lost = on_call_lost
        self._lost_signalled = False
        self._lost_task: asyncio.Task | None = None

        # Whether this caller has been greeted. One call, one greeting: a
        # provider that retries its "incoming" event must not make the bank say
        # hello twice down the same line.
        self._greeted = False

        # Monotonic, because a clock that can go backwards would make a live
        # call look idle. Read by the idle sweep.
        self.last_activity = time.monotonic()

        # Counters, for the operator and for the tests. Not audio.
        self.frames_from_caller = 0
        self.frames_to_caller = 0

    # --- the model's side ----------------------------------------------------

    def on_realtime_event(self, banking_session_id: str, event) -> None:
        """Handle one model event for **this** call.

        Bound to one bridge and passed to `RealtimeManager.start`, so the
        session id argument is this bridge's by construction. It is checked
        anyway: a handler that silently accepted somebody else's event would be
        the exact cross-call leak this design exists to prevent, and an
        assertion that never fires costs nothing.
        """
        if banking_session_id != self.banking_session_id:
            logger.error(
                "bridge[%s] refused an event for another session",
                self.provider_call_id,
            )
            return

        kind = getattr(event, "type", "")

        if kind == "audio":
            data = getattr(getattr(event, "audio", None), "data", None)
            if data:
                self.outbound.put(data)

        elif kind == "audio_interrupted":
            # Barge-in. The caller started speaking, so everything queued is a
            # sentence they have stopped listening to. Playing it out would
            # talk over them, and the model has already stopped generating it.
            self.outbound.clear()

    # --- the pumps -----------------------------------------------------------

    async def _pump_caller_to_model(self) -> None:
        """Caller audio, converted, into this call's model session."""
        try:
            while True:
                frame = await self.transport.receive_audio()
                if frame is None:
                    break
                self.frames_from_caller += 1
                self.last_activity = time.monotonic()
                try:
                    await self._realtime.send_audio(
                        self.banking_session_id, codec.telephony_to_model(frame)
                    )
                except Exception as error:
                    # The model session has gone. End this call; do not spin.
                    logger.info(
                        "bridge[%s] inbound pump stopping: %s",
                        self.provider_call_id,
                        type(error).__name__,
                    )
                    # The model session has gone, so this call cannot continue.
                    # Say so rather than leaving a silent call holding a slot.
                    self._signal_lost()
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "bridge[%s] inbound pump failed: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            self._signal_lost()

    async def _pump_model_to_caller(self) -> None:
        """Assistant audio, converted, back to this caller and no other."""
        try:
            while True:
                chunk = await self.outbound.get()
                if chunk is None:
                    break
                await self.transport.send_audio(codec.model_to_telephony(chunk))
                self.frames_to_caller += 1
                self.last_activity = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "bridge[%s] outbound pump failed: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            self._signal_lost()

    @property
    def greeted(self) -> bool:
        return self._greeted

    async def greet(self) -> bool:
        """Open the conversation, so the caller is not met with silence.

        A telephone caller hears nothing until somebody speaks, and the model
        will not speak until something prompts a turn — the browser page does
        this itself by sending `response.create` over its data channel. Nothing
        was doing it for the telephone, so a live caller would have connected
        successfully and waited in silence.

        The greeting is produced by the **agent**, through the same realtime
        session that carries the rest of the call, using the wording already in
        its instructions. It is deliberately not a recorded file or a separate
        audio path: a second way to make sound reach a caller would be a second
        thing to keep in step with the agent's actual behaviour, and the first
        to drift.

        Returns True if this call is the one that greeted, False if it had been
        greeted already or is closing. Guarded rather than trusted, because a
        duplicate provider event and a media socket attaching can both arrive
        at a moment that looks like the start of the call.
        """
        if self._greeted or self._closed:
            return False
        self._greeted = True

        try:
            await self._realtime.send_message(self.banking_session_id, GREETING_CUE)
        except Exception as error:
            # A caller who is not greeted still has a working call — they can
            # speak first, and the agent answers. Not worth ending a call over.
            self._greeted = False
            logger.warning(
                "bridge[%s] greeting could not be delivered: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            return False

        self.last_activity = time.monotonic()
        return True

    def _signal_lost(self) -> None:
        """Tell the owner this call has failed, exactly once.

        The task is deliberately **not** kept in `self._tasks`. Cleanup cancels
        everything in there, and the thing this schedules is what performs the
        cleanup — putting it in the list would have the teardown cancel itself
        halfway through.
        """
        if self._closed or self._lost_signalled or self._on_call_lost is None:
            return
        self._lost_signalled = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop during shutdown
            return
        # Held in an attribute so it is not garbage collected mid-flight.
        self._lost_task = loop.create_task(
            self._on_call_lost(self.provider_call_id, self.banking_session_id),
            name=f"phone-lost-{self.provider_call_id}",
        )

    async def start(self) -> None:
        """Open the transport and begin moving audio."""
        await self.transport.on_call_started()
        self._tasks = [
            asyncio.create_task(
                self._pump_caller_to_model(),
                name=f"phone-in-{self.provider_call_id}",
            ),
            asyncio.create_task(
                self._pump_model_to_caller(),
                name=f"phone-out-{self.provider_call_id}",
            ),
        ]

    # --- shutdown ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Stop the pumps and release the transport. Safe to call repeatedly.

        Idempotent by a lock and a flag rather than by hope: a call can end
        from three directions at once — the caller hangs up, the provider sends
        an event, the model session drops — and all three land here.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            self.outbound.close()

            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    # A pump that failed on the way down changes nothing: the
                    # call is over either way, and its exception must not stop
                    # the transport being released.
                    pass
            self._tasks = []

            try:
                await self.transport.on_call_ended()
            except Exception as error:
                logger.warning(
                    "bridge[%s] transport close failed: %s",
                    self.provider_call_id,
                    type(error).__name__,
                )

    def describe(self) -> dict:
        """Operator-safe state. No audio, no identity, no provider internals."""
        return {
            "provider_call_id": self.provider_call_id,
            "closed": self._closed,
            "frames_from_caller": self.frames_from_caller,
            "frames_to_caller": self.frames_to_caller,
            "outbound_queued": len(self.outbound),
            "outbound_dropped": self.outbound.dropped,
        }


class PhoneCallRegistry:
    """Every live bridge, keyed by provider call id.

    A keyed container, not shared state: a lookup returns one call's bridge and
    offers no way to reach another. Registration is atomic, so a duplicate
    provider event cannot produce a second bridge for a call that already has
    one — the same invariant the database holds for the call record, held here
    for the media path.
    """

    def __init__(self) -> None:
        self._bridges: dict[str, PhoneCallBridge] = {}
        self._lock = asyncio.Lock()

    async def register(self, bridge: PhoneCallBridge) -> bool:
        """Add a bridge. False if this call already had one."""
        async with self._lock:
            if bridge.provider_call_id in self._bridges:
                return False
            self._bridges[bridge.provider_call_id] = bridge
            return True

    def get(self, provider_call_id: str) -> PhoneCallBridge | None:
        return self._bridges.get(provider_call_id)

    def get_by_session(self, banking_session_id: str) -> PhoneCallBridge | None:
        for bridge in list(self._bridges.values()):
            if bridge.banking_session_id == banking_session_id:
                return bridge
        return None

    async def remove(self, provider_call_id: str) -> PhoneCallBridge | None:
        """Take a bridge out of the registry. None if it was already gone."""
        async with self._lock:
            return self._bridges.pop(provider_call_id, None)

    def active_count(self) -> int:
        return len(self._bridges)

    def active_call_ids(self) -> list[str]:
        return sorted(self._bridges)

    def all_bridges(self) -> list["PhoneCallBridge"]:
        """A snapshot, for the idle sweep. A copy, so removal during
        iteration cannot break it."""
        return list(self._bridges.values())

    async def close_all(self) -> int:
        """Close every bridge. Used on shutdown and by tests."""
        async with self._lock:
            bridges = list(self._bridges.values())
            self._bridges.clear()
        for bridge in bridges:
            await bridge.close()
        return len(bridges)


# One registry for the process, holding per-call entries. The alternative — a
# registry created per request — would lose track of calls between the event
# that starts one and the socket that carries it.
phone_call_registry = PhoneCallRegistry()
