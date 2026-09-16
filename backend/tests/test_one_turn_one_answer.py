"""Phase 7.4D: one caller turn, one caller-visible answer.

Fail-before-fix evidence for a live acceptance failure at commit b800e6a.

Provider call `56f012b6-2b4e-1240-4790-eaa5afddeeef`, agent session AGT-000116.
The caller asked for their balance, the agent asked for the demo customer ID -
and then asked for it again, out loud, while the caller was part-way through
saying it. They hung up. `authenticated=false`, `tool_call_count=0`,
`CUSTOMER_ENDED`. The same duplication is visible in two earlier calls on this
build, `7e473a9d-2b4c` and `8f7823c5-2b4d`, which happened to complete anyway.

The persisted trace:

    seq 4  02:16:58.937283  AGENT  "Certainly, before I access your banking
                                    information, may I have your demo customer
                                    ID, please?"   turn=2
    seq 5  02:16:58.943685  AGENT  "Certainly. Before I access your banking
                                    information, may I have your demo customer
                                    ID, please?"   turn=2

Six milliseconds apart, same turn, no caller turn between them, no tool event,
no auth event. The wording differs only in punctuation - two separate
generations of one intention, not two renderings of one generation.

**What the guard protects, and what it does not.** `_admit_response` keeps two
*responses* from talking over each other: the first response id seen owns the
turn and any other id is dropped. That is the right rule for its case and it
works - the control tests below still pass. But the model can also emit two
output *items* inside a single response, and every item of one response shares
that response's id. So both are admitted, both are queued, and the caller hears
the question twice: once, then again over the top of their answer.

The audio path never looks at item identity at all. `RealtimeAudio` carries
`item_id` on the event itself, and the bridge reads only `response_id`, from
the inner payload. Measured on this commit: one response, two items, two frames
queued, `duplicate_responses_suppressed == 0`, no warning logged.

The invariant these tests pin is ownership, not text:

    ONE CUSTOMER TURN -> AT MOST ONE CALLER-VISIBLE AGENT RESPONSE

until a new caller turn, a tool-result continuation, a backend-owned
clarification, or an explicit lifecycle action. Two items of one response are
none of those.
"""

import pytest

from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge
from app.telephony.media import LoopbackMediaTransport

PCM = b"\x01" * 160


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


class Payload:
    """The inner `audio` payload, which is where `response_id` lives."""

    def __init__(self, data, response_id):
        self.type = "audio"
        self.data = data
        self.response_id = response_id


class Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class Content:
    def __init__(self, transcript):
        self.transcript = transcript
        self.text = None


class Item:
    def __init__(self, item_id, text, role="assistant", status="completed"):
        self.item_id = item_id
        self.role = role
        self.type = "message"
        self.status = status
        self.content = [Content(text)]


def audio(response_id, item_id, data=PCM):
    """One audio event, shaped as `agents.realtime.events.RealtimeAudio` is.

    `item_id` sits on the event and `response_id` on the inner payload - which
    is exactly why reading only the payload loses the ability to tell two
    output items of one response apart.
    """
    return Event("audio", audio=Payload(data, response_id), item_id=item_id)


class FakeRealtime:
    def __init__(self):
        self.sent = []

    async def send_message(self, session_id, text):
        self.sent.append(text)

    async def send_audio(self, session_id, data):
        return None


def build_bridge():
    session = session_manager.create_session()
    return PhoneCallBridge(
        provider_call_id="call-one-turn",
        banking_session_id=session.session_id,
        transport=LoopbackMediaTransport(),
        realtime_manager=FakeRealtime(),
        outbound_max_frames=200,
    )


ASK_A = (
    "Certainly, before I access your banking information, may I have your "
    "demo customer ID, please?"
)
ASK_B = (
    "Certainly. Before I access your banking information, may I have your "
    "demo customer ID, please?"
)


# === the live failure =======================================================


def test_one_response_with_two_items_is_spoken_once():
    """Call 56f012b6, offline and deterministic.

    Both items belong to one response, so `_admit_response` admits both and the
    caller hears the question twice. What must reach the line is one answer.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    # The agent asks for the ID: item 1 of response R.
    bridge.on_realtime_event(sid, Event("history_added", item=Item("item_1", ASK_A)))
    bridge.on_realtime_event(sid, audio("resp_R", "item_1"))
    after_first = len(bridge.outbound)
    assert after_first == 1, "the first answer must reach the caller"

    # Six milliseconds later, a second item of the SAME response says it again.
    bridge.on_realtime_event(sid, Event("history_added", item=Item("item_2", ASK_B)))
    bridge.on_realtime_event(sid, audio("resp_R", "item_2"))

    assert len(bridge.outbound) == after_first, (
        "a second output item of the same response reached the caller. The "
        "agent asked for the demo customer ID twice, the second time over the "
        "caller's answer - live call 56f012b6-2b4e-1240-4790-eaa5afddeeef."
    )


def test_the_second_item_is_recorded_as_suppressed():
    """An operator must be able to see that it happened.

    The counter already exists for the two-responses case; a second item is the
    same event class and belongs in the same count, or this failure stays
    invisible on the board exactly as it was.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, audio("resp_R", "item_1"))
    bridge.on_realtime_event(sid, audio("resp_R", "item_2"))

    assert bridge.conversation.duplicate_responses_suppressed >= 1, (
        "the duplicate second answer was neither stopped nor counted"
    )


def test_a_slow_caller_is_not_asked_again_while_they_are_speaking():
    """The caller's own account of the failure.

    They had begun saying the ID when the question came a second time. Nothing
    about a caller starting to speak may release a second answer for the turn
    that is already being answered.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, audio("resp_R", "item_1"))
    spoken = len(bridge.outbound)

    # The caller starts saying their ID.
    bridge.on_realtime_event(
        sid,
        Event("raw_model_event", data=Event("x", data={"type": "input_audio_buffer.speech_started"})),
    )
    # And a second item of the same response arrives mid-word.
    bridge.on_realtime_event(sid, audio("resp_R", "item_2"))

    assert len(bridge.outbound) == spoken, (
        "the bank spoke over a caller who was answering the question it had "
        "just asked"
    )


# === controls: what must keep working =======================================


def test_one_item_streamed_as_many_frames_is_never_suppressed():
    """A sentence is hundreds of frames. All of them are the same answer."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    for _ in range(5):
        bridge.on_realtime_event(sid, audio("resp_R", "item_1"))

    assert len(bridge.outbound) == 5
    assert bridge.conversation.duplicate_responses_suppressed == 0


def test_a_genuinely_second_response_is_still_suppressed():
    """The case the guard was built for, unchanged."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, audio("resp_A", "item_1"))
    bridge.on_realtime_event(sid, audio("resp_B", "item_9"))

    assert len(bridge.outbound) == 1
    assert bridge.conversation.duplicate_responses_suppressed == 1


def test_the_next_turn_may_speak_once_this_one_has_ended():
    """Suppression is scoped to a turn, not to the call.

    Over-suppressing would be the opposite failure and just as bad: a caller
    who asks a second question and is answered with silence.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, audio("resp_A", "item_1"))
    bridge.on_realtime_event(sid, Event("audio_end"))
    bridge.on_realtime_event(sid, audio("resp_B", "item_2"))

    assert len(bridge.outbound) == 2, (
        "a legitimate answer to the next turn was suppressed"
    )


def test_barge_in_still_clears_the_turn():
    """Interruption must leave the next answer able to speak."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, audio("resp_A", "item_1"))
    bridge.on_realtime_event(sid, Event("audio_interrupted"))
    assert len(bridge.outbound) == 0, "barge-in must drop what was queued"

    bridge.on_realtime_event(sid, audio("resp_B", "item_2"))
    assert len(bridge.outbound) == 1, (
        "the answer after a barge-in was suppressed"
    )


def test_audio_with_no_identity_at_all_is_still_played():
    """An unidentifiable stream must not be silently dropped.

    Dropping audio we cannot attribute would silence real answers on any SDK
    shape that omits these fields.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, Event("audio", audio=Payload(PCM, None), item_id=None))

    assert len(bridge.outbound) == 1


# === the live authentication sequence, end to end ===========================


def caller_says(bridge, text):
    """One caller turn: speech onset, then the final transcript."""
    sid = bridge.banking_session_id
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
        ),
    )
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event(
                "input_audio_transcription_completed",
                transcript=text,
                item_id=f"caller-{bridge.conversation.turn_counter}",
            ),
        ),
    )


def agent_says(bridge, response_id, item_id, text, frames=1):
    """One assistant item: its history entry and its audio."""
    sid = bridge.banking_session_id
    bridge.on_realtime_event(sid, Event("history_added", item=Item(item_id, text)))
    for _ in range(frames):
        bridge.on_realtime_event(sid, audio(response_id, item_id))


PIN_PROMPT = "Thank you. Please provide your four-digit demo banking PIN."


def test_the_live_authentication_sequence_asks_for_the_id_once(monkeypatch):
    """Call 56f012b6 as the caller lived it, and then what should follow.

    The balance enquiry, one demo-ID question, a duplicate second item arriving
    while the caller is speaking their ID, and then the rest of authentication
    proceeding normally. The duplicate must be inaudible and everything after it
    must be unaffected - over-suppression here would answer a caller with
    silence, which is the same failure wearing the other hat.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    # 1. The caller asks for a balance. Nobody is verified yet.
    caller_says(bridge, "I want to know my account balance.")
    first_turn = bridge.conversation.turn_counter

    # 2. The bank asks for the demo customer ID - one item, many frames.
    agent_says(bridge, "resp_1", "item_1", ASK_A, frames=4)
    assert len(bridge.outbound) == 4, "the question itself must reach the caller"

    # 3. The caller begins saying their ID, and mid-word a second item of the
    #    same response asks again. This is the live failure.
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
        ),
    )
    agent_says(bridge, "resp_1", "item_2", ASK_B, frames=4)

    assert len(bridge.outbound) == 4, (
        "the caller was asked for their demo customer ID a second time while "
        "they were answering the first"
    )
    assert bridge.conversation.duplicate_responses_suppressed >= 1

    # 4. The caller finishes. This is a new turn and a new answer is owed.
    bridge.on_realtime_event(sid, Event("audio_end"))
    caller_says(bridge, "DEMO001")
    assert bridge.conversation.turn_counter > first_turn, "the caller's turn was lost"

    # 5. The PIN prompt is a legitimate next answer and must not be suppressed.
    agent_says(bridge, "resp_2", "item_3", PIN_PROMPT, frames=3)
    assert len(bridge.outbound) == 7, (
        "the PIN prompt was suppressed; over-suppression answers a caller with "
        "silence"
    )

    # 6. And the PIN turn behaves the same way.
    bridge.on_realtime_event(sid, Event("audio_end"))
    caller_says(bridge, "Four eight two one.")
    agent_says(bridge, "resp_3", "item_4", "Thank you, you are verified.", frames=2)
    assert len(bridge.outbound) == 9

    # Nothing about suppression reached the banking tools.
    assert bridge.duplicate_tool_calls == 0


def test_no_caller_speech_is_lost_to_the_suppression(monkeypatch):
    """The caller's words must survive whatever the bank drops of its own.

    Suppression is about what the *bank* says. A turn the caller took is still
    a turn, and the counter is what the rest of the conversation is scoped by.
    """
    bridge = build_bridge()

    caller_says(bridge, "I want to know my account balance.")
    agent_says(bridge, "resp_1", "item_1", ASK_A)
    agent_says(bridge, "resp_1", "item_2", ASK_B)

    before = bridge.conversation.turn_counter
    caller_says(bridge, "DEMO001")

    # Advanced, not by a particular amount: speech onset and the final
    # transcript are both turn boundaries, so the counter moves more than once
    # per utterance by design. What matters here is that the caller's turn
    # registered at all despite the bank having just suppressed one of its own.
    assert bridge.conversation.turn_counter > before, "the caller's turn was lost"
    assert bridge.frames_after_closing == 0


def test_a_suppressed_item_is_not_written_to_the_replay():
    """The board and the line must agree.

    A sentence the bank refused to speak did not happen to the caller, and a
    replay that shows it is the same disagreement that made this failure hard
    to place: the trace showed two questions, and for a while it was unclear
    whether the caller had heard one or two.
    """
    bridge = build_bridge()

    agent_says(bridge, "resp_1", "item_1", ASK_A)
    agent_says(bridge, "resp_1", "item_2", ASK_B)

    assert "item_2" in bridge._suppressed_items
    assert "item_1" not in bridge._suppressed_items
