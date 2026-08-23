"""When a telephone call speaks, listens, waits and ends.

Channel 1 runs this in the browser page: `frontend/call.js` arms the silence
timer, watches for the closing line and hangs up. A telephone has no page, so
for Channel 2 the same decisions have to be made here — deterministically, on
the server, per call.

    OPENING ──greeting──► ASSISTANT_SPEAKING
                                │ playback complete
                                ▼
                          WAITING_FOR_CALLER ──caller speaks──► CALLER_SPEAKING
                                │ 10s silence                        │
                                ▼                                    │
                             CLOSING ◄──assistant says goodbye───────┘
                                │ playback complete
                                ▼
                              CLOSED

**Silence is conversational, never network.** A caller who is listening sends
RTP the whole time — comfort noise, room tone, breathing — so absence of
packets is not absence of a person, and treating it as such would hang up on
someone mid-thought. The timer is armed only when the assistant has finished
speaking, and it is cancelled the moment the model reports the caller's voice.

**Playback completion is two facts, not one.** The model finishing *generating*
audio (`audio_end`) is not the caller finishing *hearing* it: several seconds of
speech are still queued for a telephone that plays 50 frames a second. Hanging
up on `audio_end` truncates the last sentence — which, for the goodbye and the
silence line, is precisely the sentence that matters. So playback is complete
only when generation has ended **and** the outbound queue has drained.

**There is no maximum call duration.** Channel 1 caps a browser call at fifteen
minutes; a bank does not hang up on a customer who is still transacting. A
Channel 2 call ends when the caller finishes, falls silent, disconnects, or
something fails — never because a clock ran out.

Everything here is per call. No module-level state, one timer task owned by one
instance, and every transition under that instance's lock.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

logger = logging.getLogger("app.telephony.lifecycle")

# How long the bank waits for a caller who has stopped speaking, once the
# assistant has finished its turn. Ten seconds is Channel 1's proven window.
#
# There is deliberately no second window after the closing line: Channel 1 asks
# "do you want to continue?" and waits again, but Channel 2 says its line and
# goes. A telephone caller who has already been silent for ten seconds has
# usually put the handset down.
SILENCE_SECONDS = 10.0


class CallState(str, Enum):
    """Where one call is in its conversation."""

    OPENING = "OPENING"
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"
    WAITING_FOR_CALLER = "WAITING_FOR_CALLER"
    CALLER_SPEAKING = "CALLER_SPEAKING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class EndReason(str, Enum):
    """Why a call finished. A category for the operator, never a message."""

    CALLER_GOODBYE = "CALLER_GOODBYE"
    CALLER_SILENT = "CALLER_SILENT"
    CALLER_DISCONNECTED = "CALLER_DISCONNECTED"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class CallLifecycle:
    """The conversational state of exactly one telephone call.

    Driven by model events and by the outbound audio queue draining. It decides
    two things and nothing else: when to prompt a silent caller, and when it is
    safe to hang up.

    It performs neither itself. `speak` and `hang_up` are supplied by the owner
    — the bridge — so this class stays testable without a model session, a
    socket or a bank.
    """

    def __init__(
        self,
        call_id: str,
        *,
        speak,
        hang_up,
        silence_seconds: float = SILENCE_SECONDS,
    ) -> None:
        self.call_id = call_id
        self._speak = speak
        self._hang_up = hang_up
        self._silence_seconds = silence_seconds

        self.state = CallState.OPENING
        self.end_reason: EndReason | None = None

        # True once the model says it has finished generating this turn's audio.
        # Playback is not complete until the queue has also drained.
        self._generation_ended = False
        # Set when a closing line is playing, so playback completion hangs up
        # instead of waiting for a caller who is about to be disconnected.
        self._closing_for: EndReason | None = None
        # True when the assistant's reply to the caller's most recent turn has
        # both finished generating and finished playing. False while a reply is
        # owed or in progress. It is the difference between a caller who has
        # just asked to leave and one whose goodbye has already been said —
        # states that otherwise look identical, and must not be closed alike.
        self._reply_complete = False

        self._silence_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

        # Counters, for the operator and the tests. Never audio, never speech.
        self.silence_prompts = 0
        self.turns_completed = 0

    # --- what the owner reports ---------------------------------------------

    async def on_assistant_audio(self) -> None:
        """The assistant has produced audio. It is speaking, so nobody waits."""
        async with self._lock:
            if self._closed:
                return
            if self.state is CallState.CLOSING:
                # A closing call does not reopen, so the state stays as it is.
                # Generation, though, has genuinely started again: this is the
                # closing line beginning to play. Recording that is what stops
                # its *first* frame being mistaken for its last — the queue
                # empties after that frame, and a `_generation_ended` left over
                # from the previous turn would hang up mid-goodbye.
                self._generation_ended = False
                self._reply_complete = False
                return
            self._generation_ended = False
            self._reply_complete = False
            self._cancel_silence()
            self.state = CallState.ASSISTANT_SPEAKING

    async def on_generation_ended(self) -> None:
        """The model has finished generating this turn's audio.

        Not the same as the caller having heard it. Recorded, and acted on when
        the queue drains.
        """
        async with self._lock:
            if self._closed:
                return
            self._generation_ended = True

    async def on_playback_drained(self) -> None:
        """The outbound queue is empty.

        Together with `on_generation_ended` this means the caller has heard
        everything the assistant said. Called by the outbound pump, which is
        the only thing that knows when the last frame actually went out.
        """
        async with self._lock:
            if self._closed or not self._generation_ended:
                return

            if self._closing_for is None:
                self.turns_completed += 1
                self._reply_complete = True
                self.state = CallState.WAITING_FOR_CALLER
                self._arm_silence()
                return

            # The closing line has finished playing. Now, and not before, the
            # call may be taken down.
            reason = self._closing_for
            self.state = CallState.CLOSED
            self._closed = True
            self._cancel_silence()

        # Outside the lock, deliberately. Hanging up runs the owner teardown,
        # which closes the bridge, which closes this lifecycle — and that needs
        # this same lock. Holding it across the call is a deadlock that stops
        # the call ever ending.
        await self._finish(reason)

    async def on_caller_speech_started(self, *, speech_started: bool = True) -> None:
        """The model reports the caller's voice. Cancel the wait immediately.

        `speech_started` separates the caller beginning to talk from their
        transcript arriving afterwards. Transcription runs as a separate pass
        and can complete long after the answer it prompted has been spoken, so
        a transcript is not evidence that anybody is speaking now.

        **A transcript alone changes nothing here.** It does not cancel the
        silence timer, does not claim the caller is speaking, and does not mark
        a reply owed. Acting on it would strand the call: an ordinary late
        transcript would move a waiting call into CALLER_SPEAKING and switch off
        the timer, leaving it waiting for a turn already taken and an answer
        already given, with the one thing that would have rescued it — the
        silence timeout — just turned off.

        What the transcript *is* good for happens elsewhere: the bridge reads it
        for intent, and a goodbye found in it arms closure through
        `arm_goodbye`, which decides for itself whether the reply has already
        been delivered.
        """
        async with self._lock:
            if self._closed or self.state is CallState.CLOSING:
                # A caller who speaks over the closing line does not stop it.
                # The bank has said goodbye; reopening the conversation here
                # would leave a call nothing ever ends.
                return
            if not speech_started:
                return
            self._reply_complete = False
            self._cancel_silence()
            self.state = CallState.CALLER_SPEAKING

    async def on_assistant_interrupted(self) -> None:
        """Barge-in. The caller cut in, so the assistant's turn is over."""
        async with self._lock:
            if self._closed or self.state is CallState.CLOSING:
                return
            self._generation_ended = False
            self._cancel_silence()
            self.state = CallState.CALLER_SPEAKING

    async def on_goodbye_spoken(self) -> None:
        """The assistant has begun its closing line.

        The hang-up does not happen here — it happens when that line has
        finished playing. Ending the call now would cut off the goodbye.
        """
        async with self._lock:
            if self._closed or self._closing_for is not None:
                return
            self._closing_for = EndReason.CALLER_GOODBYE
            self.state = CallState.CLOSING
            self._cancel_silence()
            logger.info("lifecycle[%s] closing: caller said goodbye", self.call_id)

    async def arm_goodbye(self) -> None:
        """The *caller* has asked to end the call. Close after the reply plays.

        The counterpart to `on_goodbye_spoken`, and now the primary trigger.
        Waiting for the assistant to reproduce a particular sentence made the
        hang-up depend on the model's wording: a paraphrased closing line was a
        call that never ended, which is exactly what callers hit. The caller's
        own words are deterministic, so closure is armed from those instead.

        Armed, not executed. Nothing is torn down here — the assistant still
        owes the caller a goodbye, and the call ends only when that reply has
        finished generating *and* finished playing, through the same
        `on_playback_drained` path every other clean ending uses.
        """
        async with self._lock:
            if self._closed or self._closing_for is not None:
                return
            self._closing_for = EndReason.CALLER_GOODBYE
            self._cancel_silence()

            if not (self._generation_ended and self._reply_complete):
                # A reply is owed or still playing. Wait for it, exactly as
                # before: the caller is owed their goodbye and cutting into
                # queued audio to deliver a hang-up would talk over it.
                self.state = CallState.CLOSING
                logger.info(
                    "lifecycle[%s] closing armed: caller asked to end the call",
                    self.call_id,
                )
                return

            # The reply this intent belongs to has already been generated in
            # full and already been heard in full. No further generation or
            # playback event is coming, so waiting for one leaves the call
            # standing in CLOSING for ever — which is the caller sitting on a
            # line the bank has finished with. Close it here instead.
            self.state = CallState.CLOSED
            self._closed = True
            logger.info(
                "lifecycle[%s] closing: caller asked to end the call after the "
                "reply had already played",
                self.call_id,
            )

        # Outside the lock, for the same reason as `on_playback_drained`:
        # hanging up tears down the bridge, which closes this lifecycle, which
        # needs this lock.
        await self._finish(EndReason.CALLER_GOODBYE)

    async def on_caller_disconnected(self) -> None:
        """The caller hung up. Nothing to play out; stop at once."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self.state = CallState.CLOSED
            self._cancel_silence()

        # Outside the lock, for the same reason as `on_playback_drained`.
        await self._finish(EndReason.CALLER_DISCONNECTED)

    # --- the silence timer ---------------------------------------------------

    def _arm_silence(self) -> None:
        """Start waiting for the caller. The lock must already be held."""
        self._cancel_silence()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop during shutdown
            return
        self._silence_task = loop.create_task(
            self._wait_for_silence(), name=f"silence-{self.call_id}"
        )

    def _cancel_silence(self) -> None:
        """Stop waiting. The lock must already be held."""
        if self._silence_task is not None:
            self._silence_task.cancel()
            self._silence_task = None

    async def _wait_for_silence(self) -> None:
        """Ten seconds of a caller not speaking, then say so and close."""
        try:
            await asyncio.sleep(self._silence_seconds)
        except asyncio.CancelledError:
            return

        async with self._lock:
            if self._closed or self.state is not CallState.WAITING_FOR_CALLER:
                return
            self._closing_for = EndReason.CALLER_SILENT
            self.state = CallState.CLOSING
            self._generation_ended = False
            self.silence_prompts += 1
            self._silence_task = None
            logger.info("lifecycle[%s] closing: caller silent", self.call_id)

        # Outside the lock: speaking reaches the model session, and holding a
        # lock across a network call would block every other transition on this
        # call behind it.
        await self._speak_closing_line()

    async def _speak_closing_line(self) -> None:
        try:
            await self._speak()
        except Exception as error:
            # The line could not be delivered, so waiting for it to finish
            # playing would wait for ever. End the call rather than strand it.
            logger.warning(
                "lifecycle[%s] closing line failed: %s",
                self.call_id,
                type(error).__name__,
            )
            async with self._lock:
                if self._closed:
                    return
                self._closed = True
                self.state = CallState.CLOSED
            await self._finish(EndReason.CALLER_SILENT)

    # --- ending --------------------------------------------------------------

    async def _finish(self, reason: EndReason) -> None:
        self.end_reason = reason
        try:
            await self._hang_up(reason)
        except Exception as error:  # pragma: no cover - teardown is best effort
            logger.error(
                "lifecycle[%s] hang-up failed: %s", self.call_id, type(error).__name__
            )

    async def close(self) -> None:
        """Release the lifecycle without hanging up. Safe to call repeatedly.

        Used when the call is being torn down for a reason the lifecycle did
        not decide — a provider event, a failed model session, a sweep.
        """
        async with self._lock:
            self._closed = True
            self.state = CallState.CLOSED
            self._cancel_silence()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def waiting_for_caller(self) -> bool:
        return self.state is CallState.WAITING_FOR_CALLER

    def describe(self) -> dict:
        """Operator-safe state. No speech, no identity, no audio."""
        return {
            "call_id": self.call_id,
            "state": self.state.value,
            "turns_completed": self.turns_completed,
            "silence_prompts": self.silence_prompts,
            "end_reason": self.end_reason.value if self.end_reason else None,
        }
