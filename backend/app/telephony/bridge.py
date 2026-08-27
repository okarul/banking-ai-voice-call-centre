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
import hashlib
import logging
import secrets
import time

from app.config import settings
from app.agents import intents, speech
from app.auth import authentication
from app.telephony import audio as codec
from app.telephony.conversation import ConversationState
from app.telephony.lifecycle import CallLifecycle, EndReason
from app.telephony import reasons
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
        # Playback boundaries. The backend's queue emptying is not the caller
        # having heard anything — the gateway is still pacing those bytes onto
        # RTP — so completion waits for the gateway to say so.
        self._boundary_counter = 0
        self._pending_boundary: str | None = None
        # History items already acted on, as "item id:text digest". Guards
        # against `history_updated` snapshots replaying the whole conversation
        # on every change. Never holds the text itself.
        self._seen_history: set[str] = set()
        # The assistant turn currently being streamed, held until it is
        # finished so the trace records one sentence rather than its drafts.
        self._agent_turn: dict | None = None

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
                # More audio for this turn, so any boundary already asked about
                # is stale: it was asked before this arrived.
                self._pending_boundary = None
                self._schedule(self.lifecycle.on_assistant_audio())

        elif kind == "audio_end":
            # The model has finished *generating* this turn. The caller has not
            # finished *hearing* it — several seconds may still be queued for a
            # telephone that plays fifty frames a second.
            self.conversation.complete_turn()
            self._schedule(self._generation_finished())

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
            # The turn is abandoned, so any boundary outstanding for it is
            # stale. A late acknowledgement must not complete a turn the
            # caller talked over.
            self._pending_boundary = None
            self._schedule(self.lifecycle.on_assistant_interrupted())

        elif kind == "history_added":
            self._on_history(getattr(event, "item", None))

        elif kind == "history_updated":
            # The same conversation change, delivered differently. Which one
            # arrives is the SDK's choice, not ours — see `_on_history_snapshot`.
            self._on_history_snapshot(getattr(event, "history", None))

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
        if raw_type == "input_audio_transcription_completed":
            # The transcript of speech that began earlier. Still a turn
            # boundary — duplicate suppression is scoped to one — but not the
            # caller starting to talk, and the difference decides whether a
            # late goodbye can end a call whose reply has already played.
            self._begin_caller_turn(speech_started=False)

            # One of the places the caller's spoken words arrive. Not the
            # only one, which is the whole lesson of this area: see
            # `_on_caller_text`.
            self._on_caller_text(getattr(data, "transcript", "") or "")
            return

        if raw_type == "raw_server_event":
            # The server event unwrapped one level further. The installed SDK
            # (openai-agents 0.20.0) names it
            # `conversation.item.input_audio_transcription.completed`; read
            # defensively, because this is the representation most likely to
            # differ between versions, and a shape we do not recognise must be
            # ignored rather than guessed at.
            self._on_raw_server_event(getattr(data, "data", None))
            return
        if raw_type == "turn_started":
            self._begin_caller_turn()
            return

        inner = getattr(data, "data", None)
        if (
            isinstance(inner, dict)
            and inner.get("type") == "input_audio_buffer.speech_started"
        ):
            self._begin_caller_turn()

    def _begin_caller_turn(self, *, speech_started: bool = True) -> None:
        """A new logical caller turn.

        Duplicate suppression is scoped to a turn, which is what stops it
        refusing a caller who legitimately asks the same question twice: the
        second ask is a new turn, so the same tool and the same answer are
        allowed again.

        `speech_started` is False when it is the transcript that arrived rather
        than the voice that started. The turn bookkeeping is identical; what
        differs is whether the lifecycle should consider a reply owed again.
        """
        self.conversation.begin_caller_turn()
        self._tools_this_turn.clear()
        self._schedule(
            self.lifecycle.on_caller_speech_started(speech_started=speech_started)
        )

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

    async def _generation_finished(self) -> None:
        """Record that the model has stopped, then ask about playback.

        One scheduled unit rather than two, because the second step reads what
        the first one writes. Scheduled separately, correctness would depend on
        which task the event loop happened to run first — and losing that race
        is unrecoverable for a turn whose queue is already empty, which is every
        short or silent one: the boundary is never issued, no pump event is left
        to retry from, and the call waits for an acknowledgement that cannot
        come.
        """
        # A backstop, not the signal. A message that told us it was completed
        # has already been written; one that never carries a status - an older
        # transport, a loopback in a test - is written here, because otherwise
        # nothing would ever write it.
        held = self._agent_turn
        if held is not None and held.get("status") is None:
            self._flush_agent_turn()
        await self.lifecycle.on_generation_ended()
        await self._close_if_authentication_is_over()
        await self._request_playback_boundary()

    async def _close_if_authentication_is_over(self) -> None:
        """End the call once authentication has stopped accepting attempts.

        The backend decides this, not the model. The assistant is instructed to
        say the session will end — and live, it said exactly that while the
        line stayed open, because nothing connected `authentication_locked` to
        the lifecycle. A bank that announces an ending and does not deliver one
        has told the caller something untrue about their own call.

        Armed here rather than executed: this runs when the model has *finished
        generating* its closing line, not when the caller has heard it. Closure
        goes through the same `arm_goodbye` path a spoken goodbye uses, so the
        line still plays out in full and the gateway still acknowledges the
        playback boundary before anything is torn down. There is deliberately
        no second hang-up mechanism — one way to end a call is the only way to
        keep the ending correct.

        Both limits end the call, and they end it the same way — but they are
        **recorded differently**, because they are different facts. Three
        failures inside this call is a caller who forgot their PIN and may ring
        back; five against the id across calls is a lock that a redial will not
        move. Filing the first as the second would report an ordinary forgotten
        PIN as a security event.

        The scope is read from the session, which authentication wrote when it
        decided. No database round trip: this runs on the loop that paces the
        caller's audio, and Phase 6.9.1 exists because of what a query here
        costs.
        """
        if not self.conversation.authentication_locked:
            return

        scope = authentication.lock_scope(self.conversation._session())
        reason = (
            EndReason.AUTHENTICATION_LOCKED
            if scope == authentication.LOCK_SCOPE_PERSISTENT
            else EndReason.AUTH_ATTEMPTS_EXHAUSTED
        )
        logger.info(
            "bridge[%s] authentication is over (%s); closing after the final line",
            self.provider_call_id,
            reason.value,
        )
        # Bounded, because this is the one ending whose closing line the
        # backend has committed to without knowing the model will produce it.
        await self.lifecycle.arm_goodbye(
            reason, deadline=settings.telephony_auth_close_timeout
        )

    async def _request_playback_boundary(self) -> None:
        """Ask the gateway to report when this turn has reached the caller.

        Only once both halves are true: the model has finished generating, and
        every byte of it has left our queue. Either can happen first — audio
        keeps arriving after `audio_end` on a long answer, and a short one
        drains before `audio_end` arrives — so both paths call this and
        whichever completes the pair asks the question.
        """
        if self._closed:
            return
        if not self.lifecycle.generation_ended or len(self.outbound):
            return
        if self._pending_boundary is not None:
            return

        self._boundary_counter += 1
        boundary_id = str(self._boundary_counter)
        self._pending_boundary = boundary_id

        ask = getattr(self.transport, "send_playback_boundary", None)
        asked = await ask(boundary_id) if ask is not None else False
        if asked:
            logger.info(
                "bridge[%s] playback boundary %s issued",
                self.provider_call_id,
                boundary_id,
            )
            return

        # Nobody to ask: a transport that does not pace, or a socket already
        # gone. Its own queue is then the only playout there is.
        self._pending_boundary = None
        await self.lifecycle.on_playback_drained()

    def on_playback_acknowledged(self, boundary_id: str) -> None:
        """The gateway has finished pacing this turn onto RTP.

        Now, and not when our queue emptied, the caller has heard the whole
        turn. Anything that does not match the boundary outstanding right now
        is ignored: a duplicate, or an acknowledgement for a turn the caller
        interrupted, must not complete the turn in progress.
        """
        if self._pending_boundary is None or boundary_id != self._pending_boundary:
            logger.info(
                "bridge[%s] stale playback acknowledgement %s ignored",
                self.provider_call_id,
                boundary_id,
            )
            return

        logger.info(
            "bridge[%s] playback acknowledged %s",
            self.provider_call_id,
            boundary_id,
        )
        self._pending_boundary = None
        self._schedule(self.lifecycle.on_playback_drained())

    def _on_raw_server_event(self, server_event) -> None:
        """A raw server event, which may or may not be a finished transcript."""
        if server_event is None:
            return
        if isinstance(server_event, dict):
            kind = server_event.get("type")
            transcript = server_event.get("transcript")
        else:
            kind = getattr(server_event, "type", None)
            transcript = getattr(server_event, "transcript", None)
        if kind != "conversation.item.input_audio_transcription.completed":
            return
        self._on_caller_text(transcript or "")

    def _on_caller_text(self, text: str) -> None:
        """Everything the caller said, however it reached us.

        The single funnel, and the reason this method exists at all. The same
        spoken sentence can arrive as a raw transcription event, as a
        `history_added` user item, or — because the server usually creates the
        conversation item when speech *starts* and only fills in the transcript
        later — as a `history_updated` snapshot with no `history_added` at all.
        Reading one representation and calling it "the caller's words" is what
        left explicit goodbyes undetected through three attempts at this bug.

        Repeated delivery of the same utterance is safe: `_read_caller_intent`
        returns as soon as closure is armed, and `arm_goodbye` returns once a
        closing reason is set, so the call ends exactly once.

        The text itself is never logged. Callers speak their PIN down this same
        path.
        """
        if not text:
            return
        self.conversation.last_user_turn = text
        self._read_caller_intent(text)

    def _on_history_snapshot(self, history) -> None:
        """`history_updated` carries the whole conversation, not the change.

        Only the most recent turn of each role can be new, so only those are
        examined; each is passed on once, keyed by item id and a digest of its
        text so that a transcript being filled in later counts as new while a
        repeated identical snapshot does not. The digest is kept rather than
        the text because these items carry spoken PINs.
        """
        if not history:
            return

        latest = {}
        for item in history:
            if getattr(item, "type", "message") != "message":
                continue
            role = getattr(item, "role", None)
            if role in ("user", "assistant"):
                latest[role] = item

        for role, item in latest.items():
            text = _item_text(item)
            if not text:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            key = f"{getattr(item, 'item_id', '')}:{digest}"
            if key in self._seen_history:
                continue
            self._seen_history.add(key)
            self._on_history_item(
                role,
                text,
                getattr(item, "item_id", None),
                getattr(item, "status", None),
            )

    def _on_history(self, item) -> None:
        """Read each completed turn, and decide whether the call is ending.

        The decision is the caller's. Their transcript is classified by the
        same deterministic rules the banking agent uses, so "goodbye", "bye" and
        "that's all" arm closure while "thank you" — courtesy, not instruction —
        leaves the line open.

        Reading it from the *assistant* is what failed live. It required the
        model to reproduce a particular sentence closely enough to be
        recognised, and a paraphrased goodbye was a call that never hung up.
        That check survives as a fallback, restricted to the bank's own closing
        sentence, so an assistant turn that merely mentions the word cannot end
        a call nobody asked to end.
        """
        text = _item_text(item)
        if not text:
            return
        self._on_history_item(
            getattr(item, "role", None),
            text,
            getattr(item, "item_id", None),
            getattr(item, "status", None),
        )

    def _note_agent_text(self, item_id, text: str, status=None) -> None:
        """Hold the newest text for the assistant turn being spoken.

        The model streams a sentence in growing pieces, and the snapshot
        handler above is keyed on the text so that a transcript filled in later
        counts as new — which it must, or a goodbye would never be recognised.
        For the trace that is the wrong shape: it turned one sentence into

            "Let me check"
            "Let me check that for your"
            "Let me check that for your savings account and then I'll share..."

        three rows deep. So nothing is written while a turn is still growing.
        The newest text is held, and the turn is written once, finished, by
        `_flush_agent_turn`.

        Kept per item, and a new item flushes the previous one: two turns can
        follow each other with no generation boundary in between.

        The longest text wins rather than the latest, because snapshots carry
        the whole conversation and nothing guarantees the order two of them
        arrive in. A turn can only grow, so the longest is the most complete.
        """
        if not text:
            return

        # Nothing to hold, and nothing to schedule, when the trace is off -
        # which is the default. Checked here rather than inside `trace.record`
        # so a disabled trace costs the media path no buffer, no task and no
        # thread hop: this runs on the audio event loop, for every sentence the
        # bank says, on every call.
        if not settings.trace_enabled:
            return

        held = self._agent_turn
        if held is not None and held["item_id"] == item_id:
            if len(text) > len(held["text"]):
                held["text"] = text
            if status is not None:
                held["status"] = status
        else:
            # A different turn. Whatever was being held is finished.
            self._flush_agent_turn()
            self._agent_turn = {
                "item_id": item_id,
                "text": text,
                "status": status,
            }

        # The provider says this message is finished, so it will not grow
        # again. This is the signal to write it - not the end of generation,
        # which arrives while the transcript is still being filled in and left
        # the live trace holding "Thank you for calling ABC".
        if self._agent_turn["status"] == "completed":
            self._flush_agent_turn()

    def _flush_agent_turn(self) -> None:
        """Write the held assistant turn, if there is one. Never twice."""
        held = self._agent_turn
        self._agent_turn = None
        if held is not None:
            self._trace_agent_turn(held["text"])

    def _trace_agent_turn(self, text: str) -> None:
        """Write down what the bank said, for the replay.

        The caller's side is traced from the scope ruling, which is the same
        funnel on both channels. The assistant has no such funnel, and this is
        the one place a completed assistant turn arrives already deduplicated.

        The greeting cue is excluded. It is a synthetic user turn this module
        injects to make the agent speak first (see `GREETING_CUE`), and a trace
        that showed it as something a caller said would be a trace that
        invents a customer utterance.

        Scheduled, never awaited: this runs on the audio event loop, and the
        write goes to PostgreSQL.
        """
        if not text or text.strip() == GREETING_CUE:
            return

        from app.observability import trace

        session = self.conversation._session()

        async def record_agent_turn() -> None:
            await asyncio.to_thread(
                trace.record,
                self.banking_session_id,
                trace.TraceEvent(
                    kind=trace.KIND_TURN,
                    speaker=trace.SPEAKER_AGENT,
                    utterance=trace.utterance_for(
                        text, speaker=trace.SPEAKER_AGENT
                    ),
                    auth_status=trace.auth_status(session),
                    customer_ref=trace.customer_ref(session),
                    event_type="agent_turn",
                ),
                session=session,
            )

        self._schedule(record_agent_turn())

    def _on_history_item(self, role, text: str, item_id=None, status=None) -> None:
        """One completed turn, from whichever history event delivered it."""
        if role == "assistant":
            self._note_agent_text(item_id, text, status)
        if role == "user":
            self._on_caller_text(text)
            return
        if role != "assistant":
            return

        self.conversation.last_agent_response = text
        if text.rstrip().endswith("?"):
            self.conversation.last_agent_question = text

        # Three ways a call may close on an assistant turn, in order of
        # authority. The caller asked to leave, so this reply is the goodbye
        # whatever its wording. Or it is the bank's canonical closing sentence.
        # Or — the safety net — the reply simply *ends* by saying goodbye.
        #
        # That last one exists because a bank that has audibly signed off must
        # never leave the line open. It is deliberately terminal-only: an
        # assistant sentence that merely uses the word closes nothing.
        #
        # None of these hang up here. They hand the decision to
        # `arm_goodbye`, which releases the SIP leg only once generation has
        # ended and playback has drained — so queued goodbye audio is never cut
        # off — and closes at once when both have *already* happened.
        #
        # That second case is why this is `arm_goodbye` and not
        # `on_goodbye_spoken`. Arming alone is correct only while the closing
        # line is still being generated or played. A history event carrying the
        # goodbye can arrive after `audio_end` and after the queue has emptied,
        # and there is then no further drain to complete the closure: the call
        # would stand in CLOSING for ever. Same late-event trap as the caller's
        # transcript, same answer — the lifecycle already knows how to tell the
        # two situations apart, and that knowledge is not duplicated here.
        if (
            self.conversation.goodbye_armed
            or speech.is_canonical_closing(text)
            or speech.is_terminal_goodbye(text)
        ):
            self.conversation.closing = True
            self._schedule(self.lifecycle.arm_goodbye())

    def _read_caller_intent(self, text: str) -> None:
        """Arm closure if the caller explicitly asked to end the call.

        Only ever arms. The hang-up itself stays with the lifecycle, which
        waits for the assistant's reply to finish generating and finish playing
        — so the caller hears the goodbye they were owed before the line drops.
        """
        if self.conversation.goodbye_armed:
            return
        if intents.classify(text).intent is not intents.Intent.END_CALL:
            return

        self.conversation.goodbye_armed = True
        self.conversation.closing = True
        logger.info(
            "bridge[%s] caller asked to end the call; closing after the reply",
            self.provider_call_id,
        )
        self._schedule(self.lifecycle.arm_goodbye())

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
                    self._signal_lost(reasons.REALTIME_RUNTIME_FAILURE)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "bridge[%s] inbound pump failed: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            self._signal_lost(reasons.MEDIA_FAILURE)

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
                    # Our queue is empty and this frame has gone out — into the
                    # gateway, which has not finished playing it. Ask.
                    await self._request_playback_boundary()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "bridge[%s] outbound pump failed: %s",
                self.provider_call_id,
                type(error).__name__,
            )
            self._signal_lost(reasons.MEDIA_FAILURE)

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

    def _signal_lost(self, cause: str | None = None) -> None:
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
            self._on_call_lost(
                self.provider_call_id,
                self.banking_session_id,
                cause or reasons.MEDIA_FAILURE,
            ),
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

            # A last sentence still being held is still a sentence the bank
            # said. Written before the pumps stop, so the replay ends where the
            # call did.
            self._flush_agent_turn()

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
