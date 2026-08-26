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
import contextlib

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


# === 13-16: the hang-up that cancels its own cleanup =========================
#
# Phase 6.11. `service.tear_down` is called from a `finally` in the media
# socket route, and from bridge tasks that a closing call is itself cancelling.
# A cancellation arriving while it was between two of its own `await`s aborted
# it part-way — reliably after the bridge had left the registry, and reliably
# before the model session was closed. The provider session stayed open, its
# capacity slot was never returned, and the idle sweep could reclaim neither,
# because the sweep walks the registry the bridge had already left.
#
# Found as an intermittent failure in `test_telephony_media_socket.py`, where a
# capacity assertion would occasionally see a slot that never came back.


class _CountingRealtime:
    """The capacity bookkeeping `tear_down` has to reach, and nothing else."""

    def __init__(self) -> None:
        self.open_calls: set[str] = set()
        self.reservations: set[str] = set()
        # Long enough that a cancellation lands *inside* the close rather than
        # around it. Deterministic: the test cancels while this is being
        # awaited, which is exactly where the live cancellation landed.
        self.close_delay = 0.05

    def used_capacity(self) -> int:
        return len(self.open_calls) + len(self.reservations)

    async def start(self, session_id: str) -> None:
        self.open_calls.add(session_id)

    async def close(self, session_id: str) -> bool:
        await asyncio.sleep(self.close_delay)
        present = session_id in self.open_calls
        self.open_calls.discard(session_id)
        return present

    async def release(self, session_id: str) -> None:
        self.reservations.discard(session_id)

    async def send_audio(self, session_id, audio):
        pass

    async def send_message(self, session_id, text):
        pass


class _SlowClosingTransport(LoopbackMediaTransport):
    """A transport whose close takes long enough to be interrupted."""

    async def on_call_ended(self) -> None:
        await asyncio.sleep(0.05)
        await super().on_call_ended()


def _wired(monkeypatch, call_id, *, transport=None):
    """One registered call, with the service pointed at a countable manager."""
    from app.telephony import service
    from app.telephony.bridge import phone_call_registry

    realtime = _CountingRealtime()
    monkeypatch.setattr(service, "voice_call_manager", realtime)

    session = session_manager.create_session()
    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=transport or _SlowClosingTransport(),
        realtime_manager=realtime,
        outbound_max_frames=200,
    )
    return service, phone_call_registry, realtime, bridge, session.session_id


async def _settled(predicate, *, timeout: float = 5.0) -> None:
    """Wait on the loop for a condition. Bounded, so a leak still fails."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.01)


def test_a_hang_up_that_cancels_the_cleanup_still_releases_the_call(monkeypatch):
    """The fail-before-fix case, with no timing assumption in it.

    The caller goes away, the route's `finally` starts the teardown, and the
    route's own task is cancelled while that teardown is in flight. That is the
    ordinary shape of a hang-up, not an exotic one, and everything the call
    holds must still be handed back.
    """

    async def scenario():
        service, registry, realtime, bridge, session_id = _wired(
            monkeypatch, "hangup-cancelled"
        )
        await registry.register(bridge)
        await realtime.start(session_id)
        assert realtime.used_capacity() == 1

        route = asyncio.ensure_future(
            service.tear_down("hangup-cancelled", session_id)
        )
        await asyncio.sleep(0)  # let it reach its first await
        route.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await route

        # The waiting stopped. The releasing did not.
        await _settled(lambda: realtime.used_capacity() == 0)
        return realtime.used_capacity(), registry.active_count(), bridge.closed

    capacity, bridges, closed = run(scenario())

    assert capacity == 0, "the capacity slot was stranded by the cancellation"
    assert bridges == 0
    assert closed is True, "the bridge was left half-closed"


def test_a_step_that_fails_does_not_strand_the_steps_after_it(monkeypatch):
    """One release failing must not keep a provider session open.

    The same shape of fault as the cancellation, reached from a different
    direction: the releases were a straight sequence, so the first failure
    stranded every step after it - and what comes after is the model session
    and its capacity slot. A bridge that cannot close is not a reason to keep
    paying for a call nobody is listening to.
    """

    async def scenario():
        service, registry, realtime, bridge, session_id = _wired(
            monkeypatch, "wedged"
        )

        async def wedged():
            raise RuntimeError("bridge is wedged")

        monkeypatch.setattr(bridge, "close", wedged)
        await registry.register(bridge)
        await realtime.start(session_id)

        await service.tear_down("wedged", session_id)
        await _settled(lambda: not service._releasing)
        return realtime.used_capacity(), registry.active_count()

    capacity, bridges = run(scenario())

    assert capacity == 0, "a wedged bridge stranded the capacity slot"
    assert bridges == 0


def test_a_cancelled_cleanup_is_still_only_one_cleanup(monkeypatch):
    """Shielding must not turn one ending into two releases."""

    async def scenario():
        service, registry, realtime, bridge, session_id = _wired(
            monkeypatch, "twice-cancelled"
        )
        await registry.register(bridge)
        await realtime.start(session_id)
        realtime.reservations.add(session_id)

        first = asyncio.ensure_future(
            service.tear_down("twice-cancelled", session_id)
        )
        await asyncio.sleep(0)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

        # The provider's own end event, arriving at the same moment.
        await service.tear_down("twice-cancelled", session_id)
        await _settled(lambda: not service._releasing)

        return realtime.used_capacity(), registry.active_count()

    capacity, bridges = run(scenario())

    assert capacity == 0
    assert bridges == 0


def test_the_release_task_is_held_until_it_finishes(monkeypatch):
    """A task referenced by nothing can be collected mid-release."""
    import gc

    from app.telephony import service

    async def scenario():
        _service, registry, realtime, bridge, session_id = _wired(monkeypatch, "held")
        await registry.register(bridge)
        await realtime.start(session_id)

        route = asyncio.ensure_future(service.tear_down("held", session_id))
        await asyncio.sleep(0)
        route.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await route

        held = len(service._releasing)
        gc.collect()
        still_held = len(service._releasing)

        await _settled(lambda: not service._releasing)
        return held, still_held, len(service._releasing), realtime.used_capacity()

    held, still_held, after, capacity = run(scenario())

    assert held == 1, "the in-flight release was not held anywhere"
    assert still_held == 1, "a collection could have taken the release with it"
    assert after == 0, "the release was never discarded when it finished"
    assert capacity == 0


def test_a_cancelled_hang_up_still_records_the_ending(monkeypatch):
    """Releasing the call and writing down that it ended are one ending.

    Shielding only the release left the other half exposed: the call was freed
    and its row stayed `ACTIVE` with no `ended_at` — a live call on the
    operations board and a finished one everywhere else, for a caller who had
    simply hung up.
    """
    from sqlalchemy import select

    from app.observability import recorder
    from app.telephony import reasons

    async def scenario():
        service, registry, realtime, bridge, session_id = _wired(
            monkeypatch, "record-cancelled"
        )
        recorder.claim_phone_call(
            session_id,
            provider_call_id="record-cancelled",
            provider_event_id="evt-record-cancelled",
        )
        await registry.register(bridge)
        await realtime.start(session_id)

        route = asyncio.ensure_future(
            service.end_media_call("record-cancelled", session_id)
        )
        await asyncio.sleep(0)
        route.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await route

        await _settled(lambda: not service._releasing)
        return realtime.used_capacity()

    capacity = run(scenario())

    assert capacity == 0
    with session_scope() as db:
        row = db.scalars(
            select(AgentSession).where(
                AgentSession.provider_call_id == "record-cancelled"
            )
        ).one()
    assert row.status == "COMPLETED", "a hang-up left the call showing as live"
    assert row.ended_at is not None
    assert row.disconnect_reason == reasons.CALLER_HANGUP
