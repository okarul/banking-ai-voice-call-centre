"""Phase 6.2: the clean ending, which used to be the one that did not work.

A caller hangs up and everything unwinds. The *bank* ends the call and nothing
happens — the caller hears "I do not hear anything from you. Thank you." and
then sits connected to dead air until they hang up themselves.

Two defects, and they compounded.

**The teardown cancelled itself.** A clean ending arrives from inside the
outbound pump: the queue drains, the lifecycle sees playback complete, and the
teardown runs on that pump's own stack. `close()` cancelled every pump —
including the one calling it — so it never got as far as releasing the
transport.

**Nothing closed the media socket.** The route sits in `websocket.receive()`
waiting for a caller who has been disconnected in every sense but the socket.
The gateway's iterator therefore never ended, `handle_call` never returned, and
the outbound SIP BYE never ran. A circular wait: each side waiting for the
other to end the call.

These tests are the two fixes, and the paths that must keep working around them.
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge
from app.telephony.lifecycle import CallState, EndReason
from app.telephony.media import LoopbackMediaTransport, WebSocketMediaTransport


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


class Realtime:
    """Just enough of the manager for a bridge to talk to."""

    def __init__(self):
        self.sent = []
        self.messages = []

    async def send_audio(self, session_id, audio):
        self.sent.append(audio)

    async def send_message(self, session_id, text):
        self.messages.append(text)


def build(call_id="teardown", *, transport=None, on_call_ended=None, silence=0.05):
    session = session_manager.create_session()
    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=transport or LoopbackMediaTransport(),
        realtime_manager=Realtime(),
        outbound_max_frames=200,
        on_call_ended=on_call_ended,
    )
    bridge.lifecycle._silence_seconds = silence
    return bridge


PCM = b"\x00\x10" * 480


# === 1-5: closing from inside a pump ========================================


def test_close_called_from_the_outbound_pump_does_not_cancel_itself():
    """The defect, isolated: the teardown ran on the pump's own stack."""

    async def scenario():
        bridge = build("self-cancel")
        finished = asyncio.Event()

        async def end_call(call_id, session_id, reason):
            # Exactly what the service does — and it runs on the pump's stack.
            await bridge.close()
            finished.set()

        bridge._on_call_ended = end_call
        await bridge.start()
        outbound_pump = bridge._tasks[1]

        # One turn: audio, generation ends, queue drains, goodbye already said.
        await bridge.lifecycle.on_assistant_audio()
        await bridge.lifecycle.on_goodbye_spoken()
        bridge.outbound.put(PCM)
        await bridge.lifecycle.on_generation_ended()

        completed = await asyncio.wait_for(finished.wait(), timeout=5)
        return completed, bridge.closed, outbound_pump.cancelled()

    completed, closed, pump_cancelled = run(scenario())

    assert completed is True, "the teardown never finished"
    assert closed is True, "close() did not complete"
    assert pump_cancelled is False, "close() cancelled the task running it"


def test_a_silent_caller_teardown_completes_end_to_end():
    """CALLER_SILENT, which is the live symptom."""

    async def scenario():
        ended = []
        bridge = build("silent")

        async def end_call(call_id, session_id, reason):
            ended.append(reason)
            await bridge.close()

        bridge._on_call_ended = end_call
        await bridge.start()

        # The assistant finishes a turn, so the wait is armed.
        await bridge.lifecycle.on_assistant_audio()
        await bridge.lifecycle.on_generation_ended()
        await bridge.lifecycle.on_playback_drained()

        # Silence fires; the closing line plays out; the call ends.
        await asyncio.sleep(0.2)
        await bridge.lifecycle.on_assistant_audio()
        await bridge.lifecycle.on_generation_ended()
        await bridge.lifecycle.on_playback_drained()
        await asyncio.sleep(0.1)
        return ended, bridge.closed, bridge.transport.ended

    ended, closed, transport_ended = run(scenario())

    assert ended == [EndReason.CALLER_SILENT.value]
    assert closed is True
    assert transport_ended is True, "the transport was never released"


def test_a_goodbye_teardown_completes_end_to_end():
    async def scenario():
        ended = []
        bridge = build("goodbye")

        async def end_call(call_id, session_id, reason):
            ended.append(reason)
            await bridge.close()

        bridge._on_call_ended = end_call
        await bridge.start()

        await bridge.lifecycle.on_assistant_audio()
        await bridge.lifecycle.on_goodbye_spoken()
        await bridge.lifecycle.on_generation_ended()
        await bridge.lifecycle.on_playback_drained()
        await asyncio.sleep(0.1)
        return ended, bridge.closed, bridge.transport.ended

    ended, closed, transport_ended = run(scenario())

    assert ended == [EndReason.CALLER_GOODBYE.value]
    assert closed is True
    assert transport_ended is True


def test_the_other_pump_is_cancelled_and_released():
    """Only the caller of close() survives; the sibling must be stopped."""

    async def scenario():
        bridge = build("siblings")
        done = asyncio.Event()

        async def end_call(call_id, session_id, reason):
            await bridge.close()
            done.set()

        bridge._on_call_ended = end_call
        await bridge.start()
        inbound_pump, outbound_pump = bridge._tasks

        await bridge.lifecycle.on_assistant_audio()
        await bridge.lifecycle.on_goodbye_spoken()
        bridge.outbound.put(PCM)
        await bridge.lifecycle.on_generation_ended()
        await asyncio.wait_for(done.wait(), timeout=5)
        await asyncio.sleep(0.05)
        return inbound_pump, outbound_pump

    inbound_pump, outbound_pump = run(scenario())

    assert inbound_pump.done(), "the inbound pump was left running"
    assert outbound_pump.cancelled() is False


def test_close_remains_idempotent():
    async def scenario():
        bridge = build("idempotent")
        await bridge.start()
        for _ in range(4):
            await bridge.close()
        leftover = [t for t in bridge._tasks if not t.done()]
        return bridge.closed, leftover, bridge.transport.ended

    closed, leftover, transport_ended = run(scenario())

    assert closed is True
    assert leftover == []
    assert transport_ended is True


def test_a_provider_failure_still_tears_down_cleanly():
    """The failure path must keep working around the new guard."""

    async def scenario():
        class Broken(LoopbackMediaTransport):
            async def on_call_ended(self):
                raise RuntimeError("transport already gone")

        bridge = build("broken", transport=Broken())
        await bridge.start()
        await bridge.close()
        return bridge.closed, [t for t in bridge._tasks if not t.done()]

    closed, leftover = run(scenario())

    assert closed is True, "a failing transport blocked the teardown"
    assert leftover == []


# === 6-7: the websocket transport closes what it was given ==================


class FakeSocket:
    def __init__(self, *, fail_close=False, already_gone=False):
        self.sent = []
        self.closed = 0
        self._fail_close = fail_close
        self._already_gone = already_gone

    async def send_bytes(self, data):
        if self._already_gone:
            raise RuntimeError("Cannot call send once a close message has been sent.")
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed += 1
        if self._fail_close:
            raise RuntimeError('Cannot call "close" once a close has been sent.')


def test_the_transport_closes_its_attached_socket_when_the_call_ends():
    """The circular wait: without this the route never wakes."""

    async def scenario():
        transport = WebSocketMediaTransport(max_frames=50)
        socket = FakeSocket()
        transport.attach(socket)
        await transport.on_call_ended()
        return socket.closed, transport.attached

    closed, still_attached = run(scenario())

    assert closed == 1, "the socket was left open"
    assert still_attached is False


def test_closing_an_already_disconnected_socket_is_harmless():
    """A caller who hung up first must not turn teardown into an error."""

    async def scenario():
        transport = WebSocketMediaTransport(max_frames=50)
        transport.attach(FakeSocket(fail_close=True))
        await transport.on_call_ended()          # must not raise
        await transport.on_call_ended()          # nor a second time
        return transport.attached

    assert run(scenario()) is False


def test_a_transport_that_never_attached_closes_nothing():
    async def scenario():
        transport = WebSocketMediaTransport(max_frames=50)
        await transport.on_call_ended()
        return transport.attached

    assert run(scenario()) is False


def test_the_socket_is_closed_only_once_across_repeated_teardowns():
    async def scenario():
        transport = WebSocketMediaTransport(max_frames=50)
        socket = FakeSocket()
        transport.attach(socket)
        for _ in range(3):
            await transport.on_call_ended()
        return socket.closed

    assert run(scenario()) == 1


def test_audio_already_sent_is_not_discarded_by_the_close():
    """Every send is awaited before the queue drains, so the close is last."""

    async def scenario():
        transport = WebSocketMediaTransport(max_frames=50)
        socket = FakeSocket()
        transport.attach(socket)
        for index in range(5):
            await transport.send_audio(bytes([index]) * 160)
        await transport.on_call_ended()
        return socket.sent, socket.closed

    sent, closed = run(scenario())

    assert len(sent) == 5, "audio was dropped before the socket closed"
    assert closed == 1


def test_two_calls_cannot_close_one_anothers_socket():
    async def scenario():
        first = WebSocketMediaTransport(max_frames=50)
        second = WebSocketMediaTransport(max_frames=50)
        first_socket, second_socket = FakeSocket(), FakeSocket()
        first.attach(first_socket)
        second.attach(second_socket)

        await first.on_call_ended()
        return first_socket.closed, second_socket.closed, second.attached

    first_closed, second_closed, second_attached = run(scenario())

    assert first_closed == 1
    assert second_closed == 0, "one call closed another's media socket"
    assert second_attached is True
