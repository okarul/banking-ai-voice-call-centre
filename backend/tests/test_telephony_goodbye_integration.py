"""Phase 6.6: the caller's goodbye, through the real event pump.

Every earlier attempt at this bug tested the bridge by calling
`bridge.on_realtime_event(...)` directly with an event we had constructed
ourselves. That proves the bridge's logic and nothing about the path the events
actually take — and the bug was always *in* that path: the bridge was reading a
representation the live session does not deliver.

So these tests drive `RealtimeManager._pump_events` over a fake session and let
it call the real `bridge.on_realtime_event`. What the session yields is modelled
on the installed SDK (openai-agents 0.20.0), whose `RealtimeSession._on_event`
does this on a completed transcription:

    prev_len = len(self._history)
    self._history = RealtimeSession._get_new_history(self._history, event)
    if len(self._history) > prev_len:
        await self._put_event(RealtimeHistoryAdded(...))
    else:
        await self._put_event(RealtimeHistoryUpdated(...))

The server creates the conversation item when the caller *starts* speaking, so
by the time the transcript completes the item already exists — the history does
not grow, and the SDK emits **history_updated**, not history_added. That is the
representation the bridge never read.
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.agents import speech
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.realtime.realtime_manager import RealtimeConnection, RealtimeManager
from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge
from app.telephony.lifecycle import CallState, EndReason
from app.telephony.media import LoopbackMediaTransport


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    session_manager.clear()
    wipe()


def run(coro):
    return asyncio.run(coro)


# === the SDK's shapes, as installed =========================================


class Info:
    """RealtimeEventInfo, which the bridge does not read but the SDK sends."""


class RawModelEvent:
    type = "raw_model_event"

    def __init__(self, data):
        self.data = data
        self.info = Info()


class TranscriptionCompleted:
    """agents.realtime.model_events.RealtimeModelInputAudioTranscriptionCompletedEvent"""

    type = "input_audio_transcription_completed"

    def __init__(self, item_id, transcript):
        self.item_id = item_id
        self.transcript = transcript


class RawServerEvent:
    """agents.realtime.model_events.RealtimeModelRawServerEvent"""

    type = "raw_server_event"

    def __init__(self, data):
        self.data = data


class Content:
    def __init__(self, *, transcript=None, text=None, kind="input_audio"):
        self.type = kind
        self.transcript = transcript
        self.text = text


class Item:
    """agents.realtime.items.RealtimeMessageItem"""

    type = "message"

    def __init__(self, item_id, role, text, *, status="completed"):
        self.item_id = item_id
        self.role = role
        self.status = status
        kind = "input_audio" if role == "user" else "audio"
        self.content = [Content(transcript=text, kind=kind)]


class HistoryAdded:
    type = "history_added"

    def __init__(self, item):
        self.item = item
        self.info = Info()


class HistoryUpdated:
    type = "history_updated"

    def __init__(self, history):
        self.history = list(history)
        self.info = Info()


class Simple:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class Pause:
    """Not an event. Lets the outbound pump actually run between events.

    Playback draining is work done by the bridge's own pump, not something an
    event announces. To place a history event genuinely *after* the queue has
    emptied, the stream has to stop and let that happen — so this marker is
    consumed by the fake session and never reaches the bridge.
    """

    def __init__(self, seconds=0.15):
        self.seconds = seconds


class FakeSession:
    """An async-iterable session, exactly what `_pump_events` consumes."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            if isinstance(event, Pause):
                await asyncio.sleep(event.seconds)
                continue
            yield event
            await asyncio.sleep(0)


# === the harness ============================================================


class Realtime:
    def __init__(self):
        self.messages = []
        # Phase 6.15 counts what actually reaches the model, because "the
        # caller's goodbye was recognised" and "the model stopped being asked
        # questions" turned out to be different facts.
        self.audio_frames = 0

    async def send_audio(self, session_id, audio):
        self.audio_frames += 1
        return None

    async def send_message(self, session_id, text):
        self.messages.append(text)


async def drive(events, *, call_id="integration"):
    """Run the real pump over `events` into a real bridge. Report the ending."""
    session = session_manager.create_session()
    ended = []

    async def on_call_ended(provider_call_id, banking_session_id, reason):
        ended.append(reason)
        await bridge.close()

    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=LoopbackMediaTransport(),
        realtime_manager=Realtime(),
        outbound_max_frames=200,
        on_call_ended=on_call_ended,
    )
    await bridge.start()

    manager = RealtimeManager()
    connection = RealtimeConnection(
        banking_session_id=session.session_id,
        realtime_session_id="rt-integration",
        session=FakeSession(events),
    )

    # The real pump, the real callback.
    await manager._pump_events(connection, bridge.on_realtime_event)
    await asyncio.sleep(0.15)

    reason = bridge.lifecycle.end_reason
    armed = bridge.conversation.goodbye_armed
    if not bridge.closed:
        await bridge.close()
    return ended, reason, armed


PCM = b"\x00\x10" * 480


def speaking(item_id="assistant-1", text="Thank you. Goodbye."):
    """The assistant answering: audio, its transcript, then generation ending."""
    return [
        Simple("audio", audio=Simple("audio", data=PCM)),
        HistoryAdded(Item(item_id, "assistant", text)),
        Simple("audio_end"),
    ]


def caller_started():
    return RawModelEvent(Simple("speech", data={"type": "input_audio_buffer.speech_started"}))


GOODBYE = "That's all, thank you, goodbye."


# === Part D: every representation, through the real pump ====================


def test_a_goodbye_delivered_as_a_raw_transcription_event_ends_the_call():
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                *speaking(),
                RawModelEvent(TranscriptionCompleted("user-1", GOODBYE)),
            ]
        )
    )

    assert armed is True
    assert ended == [EndReason.CALLER_GOODBYE.value], ended
    assert reason is EndReason.CALLER_GOODBYE


def test_a_goodbye_delivered_as_history_added_ends_the_call():
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                *speaking(),
                HistoryAdded(Item("user-1", "user", GOODBYE)),
            ]
        )
    )

    assert armed is True
    assert ended == [EndReason.CALLER_GOODBYE.value]
    assert reason is EndReason.CALLER_GOODBYE


def test_a_goodbye_delivered_only_as_history_updated_ends_the_call():
    """The representation the live session actually uses, and the live failure.

    No `history_added` for the caller and no raw transcription event: the item
    already existed, so the SDK filled in its transcript and re-emitted the
    whole history. Before Phase 6.6 nothing here reached the classifier.
    """
    history = [
        Item("user-1", "user", GOODBYE),
        Item("assistant-1", "assistant", "Thank you. Goodbye."),
    ]
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                HistoryUpdated(history),
            ]
        )
    )

    assert armed is True, "the caller's goodbye never reached the classifier"
    assert ended == [EndReason.CALLER_GOODBYE.value]
    assert reason is EndReason.CALLER_GOODBYE


def test_a_goodbye_delivered_as_a_nested_raw_server_event_ends_the_call():
    """The unwrapped server event, named as the installed SDK names it."""
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                *speaking(),
                RawModelEvent(
                    RawServerEvent(
                        {
                            "type": (
                                "conversation.item.input_audio_transcription"
                                ".completed"
                            ),
                            "transcript": GOODBYE,
                        }
                    )
                ),
            ]
        )
    )

    assert armed is True
    assert ended == [EndReason.CALLER_GOODBYE.value]
    assert reason is EndReason.CALLER_GOODBYE


def test_an_unrecognised_raw_server_event_is_ignored():
    """A shape we do not know must be ignored, not guessed at."""
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                RawModelEvent(RawServerEvent({"type": "response.created"})),
            ]
        )
    )

    assert armed is False
    assert ended == []
    assert reason is None


# --- duplicate safety across representations --------------------------------


def test_all_three_representations_of_one_goodbye_hang_up_exactly_once():
    """Raw, history_added and history_updated for the same utterance."""
    item = Item("user-1", "user", GOODBYE)
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                *speaking(),
                RawModelEvent(TranscriptionCompleted("user-1", GOODBYE)),
                HistoryAdded(item),
                HistoryUpdated([item, Item("assistant-1", "assistant", "Thank you. Goodbye.")]),
            ]
        )
    )

    assert ended == [EndReason.CALLER_GOODBYE.value], f"hung up {len(ended)} times"
    assert reason is EndReason.CALLER_GOODBYE


def test_a_repeated_history_updated_snapshot_does_not_hang_up_twice():
    """Every change re-sends the whole conversation. Only new content counts."""
    history = [
        Item("user-1", "user", GOODBYE),
        Item("assistant-1", "assistant", "Thank you. Goodbye."),
    ]
    ended, _, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                HistoryUpdated(history),
                HistoryUpdated(history),
                HistoryUpdated(history),
            ]
        )
    )

    assert ended == [EndReason.CALLER_GOODBYE.value], f"hung up {len(ended)} times"


# === Part E: the live failure, end to end ===================================


def test_the_exact_live_sequence_terminates_the_call_exactly_once():
    """Greeting, caller goodbye, bank's goodbye, generation end, drain, hang-up.

    No manual `hang_up()` anywhere. The call must end itself.
    """
    history_after_transcript = [Item("user-1", "user", GOODBYE)]
    history_after_reply = [
        Item("user-1", "user", GOODBYE),
        Item("assistant-2", "assistant", "Thank you. Goodbye."),
    ]

    events = [
        # 1-2. the greeting plays out
        Simple("audio", audio=Simple("audio", data=PCM)),
        HistoryAdded(Item("assistant-1", "assistant", "Thank you for calling ABC Demo Bank.")),
        Simple("audio_end"),
        # 3. the caller starts speaking
        caller_started(),
        # 4-5. their words arrive the way the live session delivers them
        HistoryUpdated(history_after_transcript),
        # 6-7. the assistant answers, and finishes generating
        Simple("audio", audio=Simple("audio", data=PCM)),
        HistoryUpdated(history_after_reply),
        Simple("audio_end"),
    ]

    ended, reason, armed = run(drive(events, call_id="live-sequence"))

    assert armed is True
    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value], f"ended {len(ended)} times"


# === Part F: the assistant terminal-goodbye safety net ======================


def test_the_bank_saying_goodbye_ends_the_call_even_with_no_caller_transcript():
    """The guarantee: an audible sign-off can never leave the line stranded.

    Transcription is deliberately absent — no raw event, no user history item.
    Only the assistant's own completed turn is delivered.
    """
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryAdded(Item("assistant-1", "assistant", "Thank you. Goodbye.")),
                Simple("audio_end"),
            ],
            call_id="no-transcript",
        )
    )

    assert armed is False, "this must be the assistant fallback, not the caller path"
    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value], f"ended {len(ended)} times"


def test_the_safety_net_also_works_through_history_updated():
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryUpdated([Item("assistant-1", "assistant", "Thanks. Good bye.")]),
                Simple("audio_end"),
            ],
            call_id="net-updated",
        )
    )

    assert ended == [EndReason.CALLER_GOODBYE.value]
    assert reason is EndReason.CALLER_GOODBYE


def test_the_canonical_closing_sentence_still_ends_the_call():
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryAdded(Item("assistant-1", "assistant", speech.GOODBYE_SPEECH)),
                Simple("audio_end"),
            ],
            call_id="canonical",
        )
    )

    assert ended == [EndReason.CALLER_GOODBYE.value]


@pytest.mark.parametrize(
    "sentence",
    [
        "You can say goodbye when you are finished.",
        "Would you like me to explain what goodbye means?",
        "I haven't said goodbye yet.",
        "Your savings balance is 12,450.75 SGD. Anything else?",
    ],
)
def test_an_assistant_sentence_that_is_not_an_ending_does_not_close(sentence):
    """The safety net must not fire on a call nobody asked to end."""
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryAdded(Item("assistant-1", "assistant", sentence)),
                Simple("audio_end"),
            ],
            call_id="not-an-ending",
        )
    )

    assert armed is False
    assert ended == [], f"closed the call on: {sentence}"
    assert reason is None


def test_the_safety_net_waits_for_the_goodbye_audio_to_finish():
    """Armed, not torn down. Queued audio is never cut off.

    The assistant's closing turn is delivered with no `audio_end`, so generation
    has not ended and playback cannot have completed. Nothing may hang up yet.
    """
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryAdded(Item("assistant-1", "assistant", "Thank you. Goodbye.")),
            ],
            call_id="still-playing",
        )
    )

    assert ended == [], "hung up while the goodbye was still being generated"
    assert reason is None


# === the caller's own words still rule ======================================


@pytest.mark.parametrize(
    "utterance",
    [
        "goodbye",
        "good bye",
        "bye",
        "that's all",
        "that is all",
        "that's all, thank you, goodbye",
        "thanks, bye",
        "no more questions",
        "nothing else",
        "end the call",
        "hang up",
        "I'm done",
        "I am done",
    ],
)
def test_every_explicit_ending_closes_the_call(utterance):
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                *speaking(),
                HistoryUpdated([Item("user-1", "user", utterance)]),
            ],
            call_id="ending",
        )
    )

    assert armed is True, f"not recognised as an ending: {utterance}"
    assert reason is EndReason.CALLER_GOODBYE


@pytest.mark.parametrize(
    "utterance",
    ["thank you", "thanks", "that's great, thanks", "thank you for your help"],
)
def test_courtesy_never_closes_the_call(utterance):
    """Answered with courtesy, as the bank actually would."""
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                *speaking(text="You're most welcome. Anything else I can help with?"),
                HistoryUpdated([Item("user-1", "user", utterance)]),
            ],
            call_id="courtesy",
        )
    )

    assert armed is False, f"courtesy treated as an ending: {utterance}"
    assert ended == []
    assert reason is None


# === the late assistant terminal goodbye ====================================
#
# `on_goodbye_spoken` only *arms* closing. That is right while the goodbye is
# still being generated or played, and wrong once it has finished: the closure
# then waits for a playback-drained event that has already happened and will
# never come again, and the call stands in CLOSING for ever.
#
# The same late-event class of bug Phase 6.4 fixed for caller transcripts, and
# it has the same fix — `arm_goodbye`, which already knows how to tell the two
# situations apart.


def test_a_terminal_goodbye_arriving_after_playback_drained_still_ends_the_call():
    """The regression. The assistant's turn is complete before its text lands.

    No caller transcript, and no audio or audio_end after the history event —
    nothing else can rescue this call.
    """
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                Pause(),                       # the queue drains; WAITING_FOR_CALLER
                HistoryUpdated([Item("assistant-1", "assistant", "Thank you. Goodbye.")]),
            ],
            call_id="late-terminal",
        )
    )

    assert armed is False, "this is the assistant fallback, not the caller path"
    assert reason is EndReason.CALLER_GOODBYE, (
        "the call was stranded after the bank had said goodbye"
    )
    assert ended == [EndReason.CALLER_GOODBYE.value], f"ended {len(ended)} times"


def test_the_same_late_ordering_through_history_added():
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                Pause(),
                HistoryAdded(Item("assistant-1", "assistant", "Thank you. Goodbye.")),
            ],
            call_id="late-added",
        )
    )

    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value]


def test_a_late_canonical_closing_sentence_also_ends_the_call():
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                Pause(),
                HistoryUpdated([Item("assistant-1", "assistant", speech.GOODBYE_SPEECH)]),
            ],
            call_id="late-canonical",
        )
    )

    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value]


def test_a_late_terminal_goodbye_after_a_caller_goodbye_ends_the_call_once():
    """Both paths, both late. One hang-up."""
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                Pause(),
                HistoryUpdated(
                    [
                        Item("user-1", "user", GOODBYE),
                        Item("assistant-1", "assistant", "Thank you. Goodbye."),
                    ]
                ),
            ],
            call_id="late-both",
        )
    )

    assert armed is True
    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value], f"ended {len(ended)} times"


def test_a_late_ordinary_assistant_sentence_still_does_not_close():
    """The safety net stays narrow even on the late path."""
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                Simple("audio_end"),
                Pause(),
                HistoryUpdated(
                    [Item("assistant-1", "assistant", "You can say goodbye when finished.")]
                ),
            ],
            call_id="late-ordinary",
        )
    )

    assert ended == []
    assert reason is None


def test_a_terminal_goodbye_before_audio_end_still_waits_for_playback():
    """The guarantee that must survive the correction: no truncation.

    The history event arrives while generation is still in progress, so closure
    is armed and the call ends only once `audio_end` and the drain follow.
    """
    ended, reason, _ = run(
        drive(
            [
                caller_started(),
                Simple("audio", audio=Simple("audio", data=PCM)),
                HistoryAdded(Item("assistant-1", "assistant", "Thank you. Goodbye.")),
                Pause(),                       # audio plays out; still generating
                Simple("audio_end"),
            ],
            call_id="waits-for-playback",
        )
    )

    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value]


# === Phase 6.15: armed is not the same as enforced =========================
#
# Live call `1790e545-1cca-1240-4790-eaa5afddeeef` recognised the caller's
# goodbye and then kept going:
#
#     seq 20  CUSTOMER  "Yeah, your balance is correct. Thank you. Goodbye."
#                       domain=CLOSING intent=END_CALL
#     seq 21  AGENT     "I do not hear anything from you. Thank you."
#     seq 23  CUSTOMER  "OK."
#     seq 24  CUSTOMER  "The balance is correct, thank you."
#     seq 25  AGENT     "You're most welcome. Is"          <- cut off
#     seq 26  CALLER_GOODBYE
#
# seq 21 is *not* the silence path. That path sets `_closing_for =
# CALLER_SILENT` and the call then ends CALLER_SILENT; this one ended
# CALLER_GOODBYE. `is_canonical_closing` and `is_terminal_goodbye` are both
# False for that sentence, so it armed nothing either. It is the model quoting
# a line that lives in its own instructions.
#
# What actually happened is simpler and worse: `arm_goodbye` arms closure and
# waits for playback, but nothing stops the caller's audio reaching the model.
# `_pump_caller_to_model` is an unconditional `while True: send_audio(...)`, so
# seq 23 and seq 24 were forwarded and the model answered them. And every reply
# that starts while CLOSING resets `_generation_ended`, which postpones the
# very drain that ends the call - so each extra answer pushes the hang-up
# further away, and seq 25 was still being generated when the line finally
# dropped.
#
# The contract this violates is its own: "A caller who speaks over the closing
# line does not stop it. The bank has said goodbye; reopening the conversation
# here would leave a call nothing ever ends."


def _terminal_bridge(call_id):
    """A bridge with the caller's goodbye already recognised."""
    session = session_manager.create_session()
    realtime = Realtime()
    transport = LoopbackMediaTransport()
    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=transport,
        realtime_manager=realtime,
        outbound_max_frames=200,
    )
    return bridge, realtime, transport


def test_no_caller_audio_reaches_the_model_after_the_terminal_decision():
    """The live defect, at its source.

    Once the caller has asked to leave, what they say next must not become a
    new question for the bank to answer.
    """

    async def scenario():
        bridge, realtime, transport = _terminal_bridge("term-audio")
        await bridge.start()
        try:
            transport.inbound.put(PCM)
            await asyncio.sleep(0.05)
            before = realtime.audio_frames
            assert before > 0, "the pump was not forwarding audio to begin with"

            bridge._read_caller_intent(GOODBYE)
            await asyncio.sleep(0.05)
            assert bridge.conversation.goodbye_armed is True

            for _ in range(5):
                transport.inbound.put(PCM)
            await asyncio.sleep(0.1)

            assert realtime.audio_frames == before, (
                f"{realtime.audio_frames - before} frames reached the model "
                "after the caller asked to end the call"
            )
        finally:
            await bridge.close()

    run(scenario())


def test_the_caller_speaking_after_the_goodbye_starts_no_new_response():
    """seq 23 and seq 24: heard, recorded, and not answered."""

    async def scenario():
        bridge, realtime, transport = _terminal_bridge("term-speech")
        await bridge.start()
        try:
            bridge._read_caller_intent(GOODBYE)
            await asyncio.sleep(0.05)

            # The caller carries on, exactly as they did live.
            bridge._on_caller_text("OK.")
            bridge._on_caller_text("The balance is correct, thank you.")
            for _ in range(5):
                transport.inbound.put(PCM)
            await asyncio.sleep(0.1)

            assert realtime.audio_frames == 0, (
                "the model was still being fed after the terminal decision"
            )
            assert bridge.lifecycle.state is CallState.CLOSING
        finally:
            await bridge.close()

    run(scenario())


def test_no_silence_prompt_follows_a_terminal_goodbye():
    """The silence cue must never be sent once closure is armed."""

    async def scenario():
        bridge, realtime, transport = _terminal_bridge("term-silence")
        bridge.lifecycle._silence_seconds = 0.05
        await bridge.start()
        try:
            bridge._read_caller_intent(GOODBYE)
            await asyncio.sleep(0.25)

            assert speech.SILENCE_CLOSING_CUE not in realtime.messages, (
                "a silent-caller cue was sent to a call that had said goodbye"
            )
            assert bridge.lifecycle.silence_prompts == 0
        finally:
            await bridge.close()

    run(scenario())


def test_the_live_shaped_race_still_ends_on_the_caller_goodbye():
    """END_CALL, the reply begins, the caller speaks again, the call ends.

    The ending must stay CALLER_GOODBYE and must still wait for the reply to
    finish playing - the 6.6/6.7 contract, unchanged.
    """
    ended, reason, armed = run(
        drive(
            [
                caller_started(),
                RawModelEvent(TranscriptionCompleted("user-1", GOODBYE)),
                # The bank's closing reply begins.
                Simple("audio", audio=Simple("audio", data=PCM)),
                # The caller talks over it, as they did live.
                caller_started(),
                RawModelEvent(TranscriptionCompleted("user-2", "OK.")),
                RawModelEvent(
                    TranscriptionCompleted(
                        "user-3", "The balance is correct, thank you."
                    )
                ),
                HistoryAdded(Item("assistant-close", "assistant",
                                  "Thank you for calling. Goodbye.")),
                Simple("audio_end"),
            ],
            call_id="term-race",
        )
    )

    assert armed is True
    assert reason is EndReason.CALLER_GOODBYE, reason
    assert ended == [EndReason.CALLER_GOODBYE.value], ended


def test_capacity_is_reclaimed_after_the_terminal_goodbye():
    """The slot must come back however much the caller said on the way out."""

    async def scenario():
        bridge, realtime, transport = _terminal_bridge("term-capacity")
        await bridge.start()
        bridge._read_caller_intent(GOODBYE)
        await asyncio.sleep(0.05)
        bridge._on_caller_text("OK.")
        await bridge.close()
        assert bridge.closed is True
        assert transport.ended is True

    run(scenario())
