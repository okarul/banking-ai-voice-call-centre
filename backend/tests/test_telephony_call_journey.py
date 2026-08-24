"""Phase 6.8 Workstream K: one whole call, start to finish, deterministically.

Every other telephony test proves one property. This one walks a call the way a
customer actually experiences it — greeting, identity, PIN, a banking question,
a long answer, a slow tool, a courtesy, another question, goodbye — and asserts
the things that only go wrong when those steps meet each other.

No DIDWW, no SIP, no live model, no cost. Real bridge, real lifecycle, real
conversation state, real intent classification; a transport that behaves like
the gateway (takes audio instantly, plays it slowly) and a model session that
says what the script says.

The properties under test are business properties, not implementation details:

    the line does not drop while the agent is speaking
    a slow tool is not mistaken for a silent customer
    "thank you" does not end the call
    "goodbye" does, but only after the goodbye has been heard
    the call ends exactly once
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.sessions import session_manager
from app.telephony.bridge import GREETING_CUE, PhoneCallBridge
from app.telephony.lifecycle import CallState, EndReason
from app.telephony.media import BoundedAudioQueue


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


PCM = b"\x00\x10" * 480


class Gateway:
    """A stand-in for the media gateway: instant to take, slow to play."""

    def __init__(self):
        self._inbound = BoundedAudioQueue(max_frames=400, name="journey-in")
        self.ended = False
        self.sent = []
        self.boundaries = []

    async def on_call_started(self):
        return None

    async def wait_until_ready(self, timeout):
        return True

    async def wait_for_protocol(self, timeout):
        return True

    async def receive_audio(self):
        return await self._inbound.get()

    async def send_audio(self, frame):
        self.sent.append(frame)

    async def send_playback_boundary(self, boundary_id):
        self.boundaries.append(boundary_id)
        return True

    async def on_call_ended(self):
        self.ended = True
        self._inbound.close()


class Model:
    def __init__(self):
        self.messages = []

    async def send_audio(self, session_id, audio):
        return None

    async def send_message(self, session_id, text):
        self.messages.append(text)


class Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class Item:
    def __init__(self, item_id, role, text):
        self.item_id = item_id
        self.role = role
        self.type = "message"
        kind = "input_audio" if role == "user" else "audio"
        self.content = [
            type("C", (), {"type": kind, "transcript": text, "text": None})()
        ]


class Call:
    """One telephone call, driven a turn at a time."""

    def __init__(self, bridge, gateway, model, ended):
        self.bridge = bridge
        self.gateway = gateway
        self.model = model
        self.ended = ended
        self._items = 0

    async def caller_speaks(self, text):
        """The caller talks, and their transcript arrives as the SDK sends it."""
        self.bridge.on_realtime_event(
            self.bridge.banking_session_id,
            Event(
                "raw_model_event",
                data=Event("speech", data={"type": "input_audio_buffer.speech_started"}),
            ),
        )
        await asyncio.sleep(0.02)
        self._items += 1
        self.bridge.on_realtime_event(
            self.bridge.banking_session_id,
            Event("history_updated", history=[Item(f"u{self._items}", "user", text)]),
        )
        await asyncio.sleep(0.05)

    async def agent_answers(self, text, *, chunks=3, acknowledge=True):
        """The agent replies, and the gateway plays it out in full."""
        for _ in range(chunks):
            self.bridge.on_realtime_event(
                self.bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )
        self._items += 1
        self.bridge.on_realtime_event(
            self.bridge.banking_session_id,
            Event(
                "history_updated",
                history=[Item(f"a{self._items}", "assistant", text)],
            ),
        )
        await self._drain()
        self.bridge.on_realtime_event(
            self.bridge.banking_session_id, Event("audio_end")
        )
        await asyncio.sleep(0.05)
        if acknowledge:
            await self.gateway_finishes_playing()

    async def gateway_finishes_playing(self):
        assert self.gateway.boundaries, "no playback boundary was ever issued"
        self.bridge.on_playback_acknowledged(self.gateway.boundaries[-1])
        await asyncio.sleep(0.05)

    async def _drain(self):
        for _ in range(400):
            await asyncio.sleep(0.005)
            if not len(self.bridge.outbound):
                return
        raise AssertionError("the outbound queue never drained")

    @property
    def alive(self):
        return self.bridge.lifecycle.end_reason is None and not self.bridge.closed


def build(call_id="journey", *, silence=0.4):
    session = session_manager.create_session()
    gateway, model, ended = Gateway(), Model(), []

    async def on_call_ended(provider_call_id, banking_session_id, reason):
        ended.append(reason)
        await bridge.close()

    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=gateway,
        realtime_manager=model,
        outbound_max_frames=400,
        on_call_ended=on_call_ended,
    )
    bridge.lifecycle._silence_seconds = silence
    return Call(bridge, gateway, model, ended)


# === the whole call =========================================================


def test_a_complete_banking_call_from_greeting_to_goodbye():
    """The business objective, walked end to end.

    Nine turns, a slow tool, a courtesy that must not end the call, and a
    goodbye that must — and only after the caller has heard it.
    """

    async def scenario():
        call = build("full-journey", silence=0.4)
        await call.bridge.start()

        # 1. The bank answers.
        assert await call.bridge.greet() is True
        await call.agent_answers("Thank you for calling ABC Demo Bank. How can I help?")
        greeted_once = call.model.messages == [GREETING_CUE]

        # 2-3. Identity and PIN.
        await call.caller_speaks("my customer id is DEMO001")
        await call.agent_answers("Thank you. May I have your four digit PIN?")
        await call.caller_speaks("one two three four")
        await call.agent_answers("Thank you, you are verified.")
        assert call.alive, "the call dropped during authentication"

        # 4. A banking question with a slow tool behind it.
        await call.caller_speaks("what is my savings balance")
        call.bridge.on_realtime_event(
            call.bridge.banking_session_id,
            Event("tool_start", tool=Event("t", name="get_account_balance"), arguments="{}"),
        )
        # The tool takes longer than the silence timeout. The caller is not
        # silent — the bank is working — and the line must stay up.
        await asyncio.sleep(0.6)
        survived_the_tool = call.alive

        # 5. A long answer, played out in full.
        await call.agent_answers(
            "Your savings balance is available. Anything else?", chunks=12
        )
        assert call.alive, "the call dropped during a long answer"

        # 6. A second question, answered.
        await call.caller_speaks("and my recent transactions")
        await call.agent_answers("Here are your recent transactions.")

        # 7. Courtesy. This must not end the call.
        await call.caller_speaks("thank you very much")
        await call.agent_answers(
            "You're most welcome. Is there anything else I can help you with today?"
        )
        survived_courtesy = call.alive
        armed_by_courtesy = call.bridge.conversation.goodbye_armed

        # 8. Another question afterwards, proving the call really continued.
        await call.caller_speaks("what is my card status")
        await call.agent_answers("Your card is active.")
        assert call.alive

        # 9. An explicit ending.
        await call.caller_speaks("that's all, thank you, goodbye")
        armed = call.bridge.conversation.goodbye_armed
        assert call.alive, "hung up before saying goodbye"

        # The closing line is generated and queued, and still nothing drops.
        for _ in range(6):
            call.bridge.on_realtime_event(
                call.bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )
        call.bridge.on_realtime_event(
            call.bridge.banking_session_id,
            Event("history_updated", history=[Item("a99", "assistant", "Thank you. Goodbye.")]),
        )
        await call._drain()
        call.bridge.on_realtime_event(call.bridge.banking_session_id, Event("audio_end"))
        await asyncio.sleep(0.05)
        alive_before_playout = call.alive

        # Only when the gateway says the goodbye has been played.
        await call.gateway_finishes_playing()
        await asyncio.sleep(0.15)

        return (
            greeted_once,
            survived_the_tool,
            survived_courtesy,
            armed_by_courtesy,
            armed,
            alive_before_playout,
            call.ended,
            call.bridge.lifecycle.end_reason,
            call.gateway.ended,
            call.bridge.conversation.turn_counter,
        )

    (
        greeted_once,
        survived_the_tool,
        survived_courtesy,
        armed_by_courtesy,
        armed,
        alive_before_playout,
        ended,
        reason,
        transport_released,
        turns,
    ) = run(scenario())

    assert greeted_once, "the caller was greeted more than once"
    assert survived_the_tool, "a slow tool was mistaken for a silent caller"
    assert survived_courtesy, "'thank you' ended the call"
    assert armed_by_courtesy is False, "courtesy armed a hang-up"
    assert armed is True, "the explicit goodbye was never recognised"
    assert alive_before_playout, "the line dropped before the goodbye was heard"
    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value], f"ended {len(ended)} times"
    assert transport_released is True, "the media path was never released"
    assert turns >= 6, f"only {turns} caller turns were counted"


def test_a_long_agent_answer_outlasts_the_silence_timeout():
    """A thirty-second answer is not a silent customer."""

    async def scenario():
        call = build("long-answer", silence=0.3)
        await call.bridge.start()

        await call.caller_speaks("tell me about my accounts")
        for _ in range(20):
            call.bridge.on_realtime_event(
                call.bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )
        await call._drain()
        call.bridge.on_realtime_event(call.bridge.banking_session_id, Event("audio_end"))

        # The backend is empty; the gateway is still speaking. Wait out several
        # silence timeouts.
        await asyncio.sleep(1.2)
        alive = call.alive

        await call.gateway_finishes_playing()
        state = call.bridge.lifecycle.state
        await call.bridge.close()
        return alive, state, call.ended

    alive, state, ended = run(scenario())

    assert alive, "the call closed while the agent was still speaking"
    assert ended == []
    assert state is CallState.WAITING_FOR_CALLER


def test_a_genuinely_silent_caller_still_ends_the_call():
    """The other half: silence after real playout must still close."""

    async def scenario():
        call = build("gone-quiet", silence=0.2)
        await call.bridge.start()

        await call.caller_speaks("what is my balance")
        await call.agent_answers("Your balance is available.")

        # The caller says nothing at all.
        await asyncio.sleep(0.5)
        closing = call.bridge.lifecycle.state

        await call.agent_answers("I do not hear anything from you. Thank you.")
        await asyncio.sleep(0.15)
        return closing, call.ended, call.bridge.lifecycle.end_reason

    closing, ended, reason = run(scenario())

    assert closing is CallState.CLOSING
    assert reason is EndReason.CALLER_SILENT
    assert ended == [EndReason.CALLER_SILENT.value]


def test_a_caller_who_hangs_up_mid_answer_ends_immediately():
    """Manual disconnect beats everything, and happens once."""

    async def scenario():
        call = build("hangs-up", silence=30.0)
        await call.bridge.start()

        await call.caller_speaks("what is my balance")
        for _ in range(4):
            call.bridge.on_realtime_event(
                call.bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )

        await call.bridge.lifecycle.on_caller_disconnected()
        await call.bridge.lifecycle.on_caller_disconnected()
        await asyncio.sleep(0.1)
        return call.ended, call.bridge.lifecycle.end_reason

    ended, reason = run(scenario())

    assert reason is EndReason.CALLER_DISCONNECTED
    assert ended == [EndReason.CALLER_DISCONNECTED.value], f"ended {len(ended)} times"


def test_two_calls_run_the_same_journey_without_touching_each_other():
    """Per-call isolation across a whole conversation, not one event."""

    async def scenario():
        first = build("journey-a", silence=30.0)
        second = build("journey-b", silence=30.0)
        await first.bridge.start()
        await second.bridge.start()

        await first.caller_speaks("what is my savings balance")
        await second.caller_speaks("that's all, goodbye")
        await asyncio.sleep(0.1)

        result = (
            first.bridge.conversation.goodbye_armed,
            second.bridge.conversation.goodbye_armed,
            first.alive,
        )

        await second.agent_answers("Thank you. Goodbye.")
        await asyncio.sleep(0.15)

        result += (first.alive, second.bridge.lifecycle.end_reason, first.ended, second.ended)
        await first.bridge.close()
        return result

    (
        first_armed,
        second_armed,
        first_alive_before,
        first_alive_after,
        second_reason,
        first_ended,
        second_ended,
    ) = run(scenario())

    assert first_armed is False, "one caller's goodbye armed another's call"
    assert second_armed is True
    assert first_alive_before and first_alive_after, "an unrelated call was ended"
    assert second_reason is EndReason.CALLER_GOODBYE
    assert first_ended == []
    assert second_ended == [EndReason.CALLER_GOODBYE.value]
