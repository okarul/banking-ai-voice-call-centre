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
import secrets
import time

from app.agents import speech
from app.telephony import audio as codec
from app.telephony.conversation import ConversationState
from app.telephony.lifecycle import CallLifecycle, EndReason
from app.telephony.media import BoundedAudioQueue, MediaTransport

logger = logging.getLogger("app.telephony.bridge")


def _item_text(item) -> str:
    """The text of one history item, however the SDK shaped it."""
    parts = []
    for entry in getattr(item, "content", None) or []:
        for attribute in ("transcript", "text"):
            value = getattr(entry, attribute, None) or (
                entry.get(attribute) if isinstance(entry, dict) else None
            )
            if value:
                parts.append(str(value))
    return " ".join(parts)

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
        on_call_ended=None,
        media_token_ttl: float = 15.0,
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
        # Called when the *conversation* ends — a goodbye played out, or a
        # silent caller prompted and released. Distinct from `on_call_lost`,
        # which is a failure.
        self._on_call_ended = on_call_ended
        self._lost_signalled = False
        self._lost_task: asyncio.Task | None = None

        # Whether this caller has been greeted. One call, one greeting: a
        # provider that retries its "incoming" event must not make the bank say
        # hello twice down the same line.
        self._greeted = False

        # Monotonic, because a clock that can go backwards would make a live
        # call look idle. Read by the idle sweep.
        self.last_activity = time.monotonic()

        # The credential that lets a gateway attach audio to *this* call.
        #
        # Opaque and random rather than signed: this process already holds
        # per-call state, so there is nothing to gain from a stateless token and
        # a great deal to lose — a random value carries no claims, cannot be
        # forged from a leaked key, and can be revoked the instant it is used.
        #
        # It says nothing about who is calling. There is no customer id in it,
        # no authentication state, and nothing derived from either, because a
        # media credential that carried identity would make attaching a socket
        # a way to assert one.
        self._media_token = secrets.token_urlsafe(32)
        self._media_token_expires_at = time.monotonic() + media_token_ttl
        self._media_token_used = False

        # Counters, for the operator and for the tests. Not audio.
        self.frames_from_caller = 0
        self.frames_to_caller = 0
        self.duplicate_tool_calls = 0

        # When this call speaks, listens, waits and ends. Channel 1 runs the
        # equivalent in the browser page; a telephone has no page, so it is
        # decided here — per call, with its own timer.
        self.lifecycle = CallLifecycle(
            provider_call_id,
            speak=self._speak_silence_line,
            hang_up=self._end_call,
        )

        # Everything this call knows about itself, in one typed place.
        from app.sessions import session_manager as _sessions

        self.conversation = ConversationState(
            provider_call_id=provider_call_id,
            banking_session_id=banking_session_id,
            session_manager=_sessions,
        )

        # Tools already executed in this turn, so a repeated call with the same
        # arguments reads the customer balance once rather than twice. A banking
        # read is idempotent; a duplicate one is still a second disclosure and a
        # second audit line.
        self._tools_this_turn: set[str] = set()

        # Lifecycle transitions scheduled from the synchronous event handler.
        self._transitions: set[asyncio.Task] = set()

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
            payload = getattr(event, "audio", None)
            data = getattr(payload, "data", None)
            if data and self._admit_response(getattr(payload, "response_id", None)):
                self.outbound.put(data)
                self.conversation.assistant_speaking = True
                self._schedule(self.lifecycle.on_assistant_audio())

        elif kind == "audio_end":
            # The model has finished *generating* this turn. The caller has not
            # finished *hearing* it — several seconds may still be queued for a
            # telephone that plays fifty frames a second.
            self.conversation.complete_turn()
            self._schedule(self.lifecycle.on_generation_ended())
            if not len(self.outbound):
                # Nothing left to play: the last frame already went out, so no
                # further drain will be reported and completion is now.
                self._schedule(self.lifecycle.on_playback_drained())

        elif kind == "audio_interrupted":
            # Barge-in. The caller started speaking, so everything queued is a
            # sentence they have stopped listening to. Playing it out would
            # talk over them, and the model has already stopped generating it.
            self.outbound.clear()
            # Release the response id as well. The interrupted response is over,
            # so the next one the model starts is a legitimate new answer rather
            # than a second voice — without this, barge-in would leave a response
            # active for ever and every later answer would be suppressed.
            self.conversation.active_response_id = None
            self.conversation.assistant_speaking = False
            self._schedule(self.lifecycle.on_assistant_interrupted())

        elif kind == "history_added":
            self._on_history(getattr(event, "item", None))

        elif kind == "tool_start":
            self._note_tool(event)

        elif kind == "raw_model_event":
            self._on_raw(getattr(event, "data", None))

    # --- reading the conversation -------------------------------------------

    def _on_raw(self, data) -> None:
        """Caller-speech signals, which are how silence is actually measured.

        Never packet absence: a caller who is listening sends RTP the whole
        time, so silence on the wire is not silence in the room. What counts is
        the model reporting a voice.
        """
        raw_type = getattr(data, "type", None)
        if raw_type in ("turn_started", "input_audio_transcription_completed"):
            self._begin_caller_turn()
            return

        inner = getattr(data, "data", None)
        if (
            isinstance(inner, dict)
            and inner.get("type") == "input_audio_buffer.speech_started"
        ):
            self._begin_caller_turn()

    def _begin_caller_turn(self) -> None:
        """A new logical caller turn.

        Duplicate suppression is scoped to a turn, which is what stops it
        refusing a caller who legitimately asks the same question twice: the
        second ask is a new turn, so the same tool and the same answer are
        allowed again.
        """
        self.conversation.begin_caller_turn()
        self._tools_this_turn.clear()
        self._schedule(self.lifecycle.on_caller_speech_started())

    # --- one turn, one response ---------------------------------------------

    def _admit_response(self, response_id: str | None) -> bool:
        """Whether this audio belongs to the response this call is playing.

        The model can be prompted more than once for a single caller turn — a
        retried cue, a racing trigger, a duplicated SDK callback — and each
        extra prompt is a second response generating audio at the same time as
        the first. Played out, that is two assistants talking over each other
        down one telephone line.

        The first response id seen becomes this turn's answer; audio from any
        other id is dropped until that one ends. Identity comes from the model
        rather than from our own counter, so a duplicated callback carrying the
        same id is admitted (it is the same answer) while a genuinely second
        response is not.
        """
        if response_id is None:
            # Nothing to distinguish responses by. Admit it: dropping audio on
            # a stream that cannot be identified would silence real answers.
            return True

        if response_id in self.conversation.rejected_response_ids:
            # Already refused on this turn. It stays refused for the rest of
            # the turn, or its tail would start playing as soon as the admitted
            # response finished.
            self.conversation.duplicate_responses_suppressed += 1
            return False

        active = self.conversation.active_response_id
        if active is None:
            self.conversation.active_response_id = response_id
            return True
        if active == response_id:
            return True

        self.conversation.rejected_response_ids.add(response_id)
        self.conversation.duplicate_responses_suppressed += 1
        logger.warning(
            "bridge[%s] suppressed a second concurrent response for one turn",
            self.provider_call_id,
        )
        return False

    def may_request_response(self) -> bool:
        """Whether this call may prompt the model to speak right now.

        Guards the places *we* create a response — the greeting cue and the
        closing cue. A cue sent while a response is already generating produces
        exactly the overlap `_admit_response` then has to throw away, so it is
        cheaper and clearer to not ask twice.
        """
        return self.conversation.active_response_id is None

    def _on_history(self, item) -> None:
        """Watch for the assistant's closing line.

        The agent decides to say goodbye, following its instructions. This
        notices that it has, so the call can be taken down once the line has
        finished playing — the same division Channel 1 uses, where the page
        watches for the closing line rather than deciding on it.
        """
        role = getattr(item, "role", None)
        text = _item_text(item)
        if not text:
            return

        if role == "user":
            self.conversation.last_user_turn = text
            return
        if role != "assistant":
            return

        self.conversation.last_agent_response = text
        if text.rstrip().endswith("?"):
            self.conversation.last_agent_question = text
        if speech.is_closing_line(text):
            self.conversation.closing = True
            self._schedule(self.lifecycle.on_goodbye_spoken())

    def _note_tool(self, event) -> None:
        """Record a tool call, and say whether it is a repeat of this turn."""
        tool = getattr(event, "tool", None)
        name = getattr(tool, "name", None) or str(tool)
        signature = f"{name}:{getattr(event, 'arguments', '')}"
        # Scoped to the turn, so the same enquiry on a later turn runs again.
        key = f"{self.conversation.turn_counter}:{signature}"
        if key in self._tools_this_turn:
            logger.warning(
                "bridge[%s] duplicate tool in one turn: %s",
                self.provider_call_id,
                name,
            )
            self.duplicate_tool_calls += 1
            self.conversation.duplicate_tools_suppressed += 1
            return
        self._tools_this_turn.add(key)

    def _schedule(self, coroutine) -> None:
        """Run a lifecycle transition from this synchronous event handler.

        The handler is called by the event pump and cannot await. Tasks are
        held so they are not garbage collected mid-flight, and discarded when
        they finish.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop during shutdown
            coroutine.close()
            return
        task = loop.create_task(coroutine)
        self._transitions.add(task)
        task.add_done_callback(self._transitions.discard)

    # --- what the lifecycle asks for ----------------------------------------

    async def _speak_silence_line(self) -> None:
        """Prompt the model to say the closing line to a silent caller."""
        await self._realtime.send_message(
            self.banking_session_id, speech.SILENCE_CLOSING_CUE
        )

    async def _end_call(self, reason: EndReason) -> None:
        """The lifecycle has decided this call is over."""
        logger.info(
            "bridge[%s] lifecycle ended the call: %s", self.provider_call_id, reason.value
        )
        if self._on_call_ended is not None:
            await self._on_call_ended(
                self.provider_call_id, self.banking_session_id, reason.value
            )

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
                if not len(self.outbound):
                    # The queue is empty and this frame has gone out. If the
                    # model has also finished generating, the caller has now
                    # heard everything — which is the only safe moment to hang
                    # up on a closing line.
                    await self.lifecycle.on_playback_drained()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "bridge[%s] outbound pump failed: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            self._signal_lost()

    # --- the media credential ------------------------------------------------

    @property
    def media_token(self) -> str:
        """The token to hand the gateway. Returned once, over the signed channel."""
        return self._media_token

    def consume_media_token(self, supplied: str | None) -> bool:
        """Check a token and spend it. False means do not accept this socket.

        Single-use on purpose. A media socket is attached once per call, so a
        second presentation of the same token is either a gateway bug or a
        replay, and neither should be able to take over a call that is already
        carrying audio.

        Constant-time comparison, for the same reason the webhook signature
        uses one: a plain `==` returns as soon as two characters differ, and how
        long it took is a measurement of how much of the token was right.
        """
        if self._closed or self._media_token_used or not supplied:
            return False
        if time.monotonic() > self._media_token_expires_at:
            return False
        if not secrets.compare_digest(supplied, self._media_token):
            return False
        self._media_token_used = True
        return True

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
        if not self.may_request_response():
            # Something is already speaking. Greeting now would put two voices
            # on the line at once.
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

        **The task running this is never cancelled.** A clean ending arrives
        from inside one of the very tasks being stopped: the outbound pump
        drains the queue, the lifecycle sees playback complete, and the teardown
        that follows runs *on the pump's own stack*. Cancelling the whole list
        would cancel the caller mid-teardown, so the transport was never
        released, the media socket stayed open, and the gateway never saw the
        call end — which is what left the telephone connected after the bank had
        said goodbye.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            # Whatever is running this. It gets stopped by returning, not by
            # being cancelled from within itself.
            current = asyncio.current_task()

            await self.lifecycle.close()
            self._stop_tasks(self._transitions, current)
            self._transitions.clear()

            self.outbound.close()

            await self._stop_and_await(self._tasks, current)
            self._tasks = []

            try:
                await self.transport.on_call_ended()
            except Exception as error:
                logger.warning(
                    "bridge[%s] transport close failed: %s",
                    self.provider_call_id,
                    type(error).__name__,
                )

    @staticmethod
    def _stop_tasks(tasks, current) -> None:
        """Cancel every task except the one asking."""
        for task in list(tasks):
            if task is not current:
                task.cancel()

    @staticmethod
    async def _stop_and_await(tasks, current) -> None:
        """Cancel the others and wait for them to actually stop.

        Awaiting the current task would be awaiting ourselves, which never
        returns; awaiting the others is what stops a cancelled pump being left
        half-torn-down while the transport is released underneath it.
        """
        others = [task for task in list(tasks) if task is not current]
        for task in others:
            task.cancel()
        for task in others:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # A pump that failed on the way down changes nothing: the call
                # is over either way, and its exception must not stop the
                # transport being released.
                pass

    def describe(self) -> dict:
        """Operator-safe state. No audio, no identity, no provider internals."""
        return {
            "provider_call_id": self.provider_call_id,
            "closed": self._closed,
            # Deliberately no media token: this is what an operator sees.
            "media_attached": self._media_token_used,
            "frames_from_caller": self.frames_from_caller,
            "frames_to_caller": self.frames_to_caller,
            "outbound_queued": len(self.outbound),
            "outbound_dropped": self.outbound.dropped,
            "duplicate_tool_calls": self.duplicate_tool_calls,
            **self.lifecycle.describe(),
            # Operational fields only. The full conversation state — which
            # includes the verified customer once there is one — is
            # `self.conversation.describe()`, for whoever legitimately needs an
            # identity. This is the media view, and it carries none.
            **self.conversation.operational_summary(),
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
