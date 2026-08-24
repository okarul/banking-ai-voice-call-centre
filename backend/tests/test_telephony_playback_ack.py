"""Phase 6.7: playback completion means the *telephone* has finished playing.

The backend hands audio to the gateway over a WebSocket as fast as the socket
will take it. The gateway then paces it onto RTP at 160 bytes every 20 ms,
because that is the rate a telephone plays. Those two rates are nothing like
each other: a twenty-second answer leaves the backend's queue in a fraction of
a second and takes twenty seconds to reach the caller.

The backend treated its own queue emptying as the caller having heard
everything. It then armed the ten-second silence timer — while the gateway was
still several seconds into speaking. On a long enough answer the timer expired
mid-sentence and the call was closed on a caller who was still being talked to.

Playback completion has to be something the gateway tells us.
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge
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


class PacingTransport:
    """A gateway that accepts instantly and plays slowly, as the real one does.

    `send_audio` returns as soon as the bytes are taken, exactly like a
    WebSocket. What the caller has actually *heard* is tracked separately.
    """

    def __init__(self):
        self._inbound = BoundedAudioQueue(max_frames=200, name="pace-in")
        self.ended = False
        self.sent = []
        self.boundaries = []
        self.attached = True

    async def on_call_started(self):
        return None

    async def wait_until_ready(self, timeout):
        return True

    async def receive_audio(self):
        return await self._inbound.get()

    async def send_audio(self, frame):
        self.sent.append(frame)          # taken instantly; not yet heard

    async def on_call_ended(self):
        self.ended = True
        self._inbound.close()

    async def send_playback_boundary(self, boundary_id):
        """The gateway is asked when it has finished pacing. It has not yet."""
        self.boundaries.append(boundary_id)
        return True


class Realtime:
    def __init__(self):
        self.messages = []

    async def send_audio(self, session_id, audio):
        return None

    async def send_message(self, session_id, text):
        self.messages.append(text)


PCM = b"\x00\x10" * 480


def build(call_id="playback", *, silence=0.2):
    session = session_manager.create_session()
    transport = PacingTransport()
    ended = []

    async def on_call_ended(provider_call_id, banking_session_id, reason):
        ended.append(reason)
        await bridge.close()

    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=transport,
        realtime_manager=Realtime(),
        outbound_max_frames=200,
        on_call_ended=on_call_ended,
    )
    bridge.lifecycle._silence_seconds = silence
    return bridge, transport, ended


class Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


def audio(bridge, chunks=8):
    for _ in range(chunks):
        bridge.on_realtime_event(
            bridge.banking_session_id, Event("audio", audio=Event("audio", data=PCM))
        )


def generation_ended(bridge):
    bridge.on_realtime_event(bridge.banking_session_id, Event("audio_end"))


# === the regression =========================================================


def test_the_silence_timer_does_not_start_when_only_the_backend_queue_drained():
    """The live failure: a call closed while the agent was still speaking.

    The backend has handed every byte to the gateway, which is still pacing
    them onto RTP. Nothing has confirmed the caller heard anything, so the
    silence timer must not be running — and the call must not close.
    """

    async def scenario():
        bridge, transport, ended = build("still-speaking", silence=0.2)
        await bridge.start()

        audio(bridge, chunks=8)

        # Everything is out of the backend and into the gateway. Live this is
        # near-instant: the socket takes twenty seconds of speech in a fraction
        # of a second, and the gateway then paces it for twenty seconds.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if not len(bridge.outbound) and transport.sent:
                break
        generation_ended(bridge)
        await asyncio.sleep(0.05)

        # The gateway is still speaking to the caller. Wait out the timer.
        await asyncio.sleep(0.6)

        state = bridge.lifecycle.state
        reason = bridge.lifecycle.end_reason
        if not bridge.closed:
            await bridge.close()
        return state, reason, ended

    state, reason, ended = run(scenario())

    assert reason is None, (
        "the call closed while the gateway was still playing the answer"
    )
    assert ended == []
    assert state is not CallState.CLOSING


def ack(bridge, transport, index=-1):
    """The gateway reporting it has finished pacing that boundary onto RTP."""
    bridge.on_playback_acknowledged(transport.boundaries[index])


async def settle(seconds=0.1):
    await asyncio.sleep(seconds)


async def drain_backend_queue(bridge, transport):
    for _ in range(400):
        await asyncio.sleep(0.005)
        if not len(bridge.outbound) and transport.sent:
            return True
    return False


# === what may and may not start the wait ====================================


def test_the_backend_queue_emptying_alone_does_not_arm_the_silence_timer():
    async def scenario():
        bridge, transport, _ = build("queue-only", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=4)
        await drain_backend_queue(bridge, transport)
        await settle()
        state = bridge.lifecycle.state
        armed = bridge.lifecycle._silence_task is not None
        await bridge.close()
        return state, armed, transport.boundaries

    state, armed, boundaries = run(scenario())

    assert state is not CallState.WAITING_FOR_CALLER
    assert armed is False, "started waiting for the caller mid-answer"
    assert boundaries == [], "asked the gateway before generation had ended"


def test_generation_ending_alone_does_not_arm_the_silence_timer():
    """`audio_end` with audio still queued is not the caller having heard it."""

    async def scenario():
        bridge, transport, _ = build("gen-only", silence=30.0)
        await bridge.start()
        # More than the pump can move in one tick.
        for _ in range(60):
            bridge.on_realtime_event(
                bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )
        generation_ended(bridge)
        await settle(0.02)
        armed = bridge.lifecycle._silence_task is not None
        await bridge.close()
        return armed

    assert run(scenario()) is False


def test_only_the_gateway_acknowledgement_starts_waiting_for_the_caller():
    async def scenario():
        bridge, transport, _ = build("ack-starts", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=4)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()

        before = bridge.lifecycle.state
        issued = list(transport.boundaries)

        ack(bridge, transport)
        await settle()
        after = bridge.lifecycle.state
        armed = bridge.lifecycle._silence_task is not None
        await bridge.close()
        return before, issued, after, armed

    before, issued, after, armed = run(scenario())

    assert before is not CallState.WAITING_FOR_CALLER
    assert len(issued) == 1, f"expected one boundary, got {issued}"
    assert after is CallState.WAITING_FOR_CALLER
    assert armed is True


def test_a_long_answer_can_outlast_the_silence_timeout_without_closing():
    """The live failure, stated as the guarantee it needs.

    The backend emptied its queue long ago. The gateway is still speaking. More
    than a full silence timeout passes, and the call stays up.
    """

    async def scenario():
        bridge, transport, ended = build("long-answer", silence=0.2)
        await bridge.start()
        audio(bridge, chunks=6)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)

        await asyncio.sleep(1.0)          # five silence timeouts of gateway speech

        alive = bridge.lifecycle.end_reason is None and not bridge.closed
        ack(bridge, transport)            # the gateway finally finishes
        await settle()
        waiting = bridge.lifecycle.state
        await bridge.close()
        return alive, ended, waiting

    alive, ended, waiting = run(scenario())

    assert alive is True, "the call closed while the gateway was still playing"
    assert ended == []
    assert waiting is CallState.WAITING_FOR_CALLER


def test_the_silence_close_still_happens_after_an_acknowledged_drain():
    """The timer is not removed, only started at the right moment."""

    async def scenario():
        bridge, transport, ended = build("silence-after-ack", silence=0.15)
        await bridge.start()
        audio(bridge, chunks=3)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        ack(bridge, transport)
        await asyncio.sleep(0.4)          # the caller says nothing

        closing = bridge.lifecycle.state
        prompted = bridge.lifecycle.silence_prompts

        # The closing line plays, and its own boundary is acknowledged.
        audio(bridge, chunks=1)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        ack(bridge, transport)
        await settle(0.2)
        return closing, prompted, ended, bridge.lifecycle.end_reason

    closing, prompted, ended, reason = run(scenario())

    assert closing is CallState.CLOSING
    assert prompted == 1
    assert reason is EndReason.CALLER_SILENT
    assert ended == [EndReason.CALLER_SILENT.value]


def test_a_short_reply_behaves_normally():
    async def scenario():
        bridge, transport, _ = build("short", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=1)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        ack(bridge, transport)
        await settle()
        state = bridge.lifecycle.state
        turns = bridge.lifecycle.turns_completed
        await bridge.close()
        return state, turns

    state, turns = run(scenario())

    assert state is CallState.WAITING_FOR_CALLER
    assert turns == 1


# === the goodbye still waits for real playout ===============================


def test_an_explicit_goodbye_waits_for_acknowledged_rtp_playout():
    """Phase 6.6 intact, and now honest about when playback finished."""

    async def scenario():
        bridge, transport, ended = build("goodbye-ack", silence=30.0)
        await bridge.start()

        bridge.on_realtime_event(
            bridge.banking_session_id,
            Event(
                "raw_model_event",
                data=Event(
                    "input_audio_transcription_completed",
                    transcript="that is all, thank you, goodbye",
                ),
            ),
        )
        await settle()
        assert bridge.conversation.goodbye_armed is True

        audio(bridge, chunks=5)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await asyncio.sleep(0.4)

        early = bridge.lifecycle.end_reason
        ack(bridge, transport)
        await settle(0.2)
        return early, ended, bridge.lifecycle.end_reason

    early, ended, reason = run(scenario())

    assert early is None, "the SIP leg was released before the goodbye had played"
    assert reason is EndReason.CALLER_GOODBYE
    assert ended == [EndReason.CALLER_GOODBYE.value]


# === acknowledgement safety =================================================


def test_a_duplicate_acknowledgement_is_ignored():
    async def scenario():
        bridge, transport, _ = build("dup-ack", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()

        for _ in range(4):
            ack(bridge, transport)
        await settle()
        turns = bridge.lifecycle.turns_completed
        await bridge.close()
        return turns

    assert run(scenario()) == 1, "a repeated acknowledgement completed the turn twice"


def test_a_stale_boundary_id_is_ignored():
    async def scenario():
        bridge, transport, _ = build("stale-id", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()

        bridge.on_playback_acknowledged("999")
        bridge.on_playback_acknowledged("")
        await settle()
        state = bridge.lifecycle.state
        await bridge.close()
        return state

    assert run(scenario()) is not CallState.WAITING_FOR_CALLER


def test_two_calls_cannot_acknowledge_each_others_playback():
    async def scenario():
        first, first_transport, _ = build("call-one", silence=30.0)
        second, second_transport, _ = build("call-two", silence=30.0)
        await first.start()
        await second.start()

        for bridge, transport in ((first, first_transport), (second, second_transport)):
            audio(bridge, chunks=2)
            await drain_backend_queue(bridge, transport)
            generation_ended(bridge)
        await settle()

        # An id that is not the one this call is waiting for.
        first.on_playback_acknowledged(second_transport.boundaries[-1] + "-other")
        await settle()
        leaked = first.lifecycle.state is CallState.WAITING_FOR_CALLER

        ack(first, first_transport)
        await settle()
        result = (leaked, first.lifecycle.state, second.lifecycle.state)
        await first.close()
        await second.close()
        return result

    leaked, first_state, second_state = run(scenario())

    assert leaked is False, "one call completed another's turn"
    assert first_state is CallState.WAITING_FOR_CALLER
    assert second_state is not CallState.WAITING_FOR_CALLER


def test_barge_in_makes_an_outstanding_boundary_stale():
    """The caller talked over the answer. Its acknowledgement means nothing."""

    async def scenario():
        bridge, transport, _ = build("barge-in", silence=30.0)
        await bridge.start()
        audio(bridge, chunks=3)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        stale = transport.boundaries[-1]

        bridge.on_realtime_event(bridge.banking_session_id, Event("audio_interrupted"))
        await settle()

        bridge.on_playback_acknowledged(stale)
        await settle()
        state = bridge.lifecycle.state
        armed = bridge.lifecycle._silence_task is not None
        await bridge.close()
        return state, armed

    state, armed = run(scenario())

    assert state is not CallState.WAITING_FOR_CALLER
    assert armed is False, "a stale acknowledgement armed the silence timer"


def test_generation_ending_with_no_audio_at_all_does_not_hang():
    """A turn that produced nothing must still complete."""

    async def scenario():
        bridge, transport, _ = build("no-audio", silence=30.0)
        await bridge.start()
        generation_ended(bridge)
        await settle()
        issued = list(transport.boundaries)
        if issued:
            ack(bridge, transport)
            await settle()
        state = bridge.lifecycle.state
        await bridge.close()
        return issued, state

    issued, state = run(scenario())

    assert len(issued) == 1, "no boundary was issued for an empty turn"
    assert state is CallState.WAITING_FOR_CALLER


def test_a_transport_that_cannot_be_asked_completes_on_its_own_queue():
    """A loopback transport, or a socket already gone. No hang, no wait."""

    async def scenario():
        bridge, transport, _ = build("cannot-ask", silence=30.0)

        async def refuse(boundary_id):
            return False

        transport.send_playback_boundary = refuse
        await bridge.start()
        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        state = bridge.lifecycle.state
        await bridge.close()
        return state

    assert run(scenario()) is CallState.WAITING_FOR_CALLER


# === the wire protocol ======================================================


def test_a_control_message_round_trips_between_the_two_implementations():
    """The backend and the gateway agree by contract, not by import."""
    from app.telephony import media as app_side
    from gateway import control as gateway_side

    asked = app_side.playback_boundary_message("7")
    assert gateway_side.read_control_message(asked) == (
        gateway_side.PLAYBACK_BOUNDARY,
        "7",
    )

    answered = gateway_side.playback_drained_message("7")
    assert app_side.read_control_message(answered) == (app_side.PLAYBACK_DRAINED, "7")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        '{"type": "playback_drained"}',
        '{"type": "playback_drained", "id": 7}',
        '{"type": "playback_drained", "id": ""}',
        '{"type": "something_else", "id": "7"}',
        '{"id": "7"}',
    ],
)
def test_malformed_control_frames_are_rejected_by_both_sides(text):
    from app.telephony import media as app_side
    from gateway import control as gateway_side

    assert app_side.read_control_message(text) is None
    assert gateway_side.read_control_message(text) is None


def test_an_over_long_boundary_id_is_rejected():
    from app.telephony import media as app_side

    payload = '{"type": "playback_drained", "id": "%s"}' % ("x" * 65)
    assert app_side.read_control_message(payload) is None


def test_control_messages_carry_nothing_but_a_type_and_an_id():
    """No transcript, identity, banking data or credential may ride here."""
    import json

    from app.telephony import media as app_side
    from gateway import control as gateway_side

    for payload in (
        app_side.playback_boundary_message("3"),
        gateway_side.playback_drained_message("3"),
    ):
        assert set(json.loads(payload)) == {"type", "id"}


def test_later_audio_makes_an_issued_boundary_stale_and_a_second_one_is_issued():
    """A boundary is a question about a turn, and the turn can grow.

    The model produces more audio after `audio_end` — a retried response, a
    late chunk. The question already asked was about less audio than the caller
    will now hear, so its answer must not complete the turn; a fresh question
    is asked once the new audio has drained, and only that answer counts.
    """

    async def scenario():
        bridge, transport, _ = build("re-boundary", silence=30.0)
        await bridge.start()

        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        first = transport.boundaries[-1]

        # More audio arrives for the same turn.
        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        await settle()

        # The answer to the first question is now about the wrong thing.
        bridge.on_playback_acknowledged(first)
        await settle()
        completed_early = bridge.lifecycle.state is CallState.WAITING_FOR_CALLER

        generation_ended(bridge)
        await settle()
        issued = list(transport.boundaries)

        bridge.on_playback_acknowledged(issued[-1])
        await settle()
        state = bridge.lifecycle.state
        turns = bridge.lifecycle.turns_completed
        await bridge.close()
        return completed_early, issued, state, turns

    completed_early, issued, state, turns = run(scenario())

    assert completed_early is False, "a stale acknowledgement completed the turn"
    assert len(issued) == 2, f"no second boundary was issued: {issued}"
    assert issued[0] != issued[1], "the second question reused the first id"
    assert state is CallState.WAITING_FOR_CALLER
    assert turns == 1, "the turn completed more than once"


def test_the_first_acknowledgement_cannot_arrive_late_and_complete_a_later_turn():
    """Two turns, and the first turn's answer wandering in during the second."""

    async def scenario():
        bridge, transport, _ = build("late-first", silence=30.0)
        await bridge.start()

        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()
        first = transport.boundaries[-1]
        ack(bridge, transport)
        await settle()

        # A second turn begins and reaches its own boundary.
        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        generation_ended(bridge)
        await settle()

        # The first turn's answer, arriving twice over.
        bridge.on_playback_acknowledged(first)
        await settle()
        turns_before = bridge.lifecycle.turns_completed

        ack(bridge, transport)
        await settle()
        turns_after = bridge.lifecycle.turns_completed
        await bridge.close()
        return turns_before, turns_after

    turns_before, turns_after = run(scenario())

    assert turns_before == 1, "an old acknowledgement completed the current turn"
    assert turns_after == 2


# === the boundary must not depend on task scheduling order =================
#
# `_request_playback_boundary` reads `lifecycle.generation_ended`, which
# `on_generation_ended` writes. Scheduled as two independent tasks, whichever
# the event loop ran first decided whether the boundary was issued at all — and
# losing that race is unrecoverable for a turn whose queue is already empty,
# which is every short or silent one. There is no later pump event to retry
# from, so the call waits for an acknowledgement that can never come.


def test_audio_end_schedules_one_ordered_unit_not_two_racing_tasks():
    """The structural guarantee, asserted rather than hoped for.

    The handler's scheduled work is captured instead of being run, then awaited
    in the worst order the loop could have chosen. One coroutine means there is
    no order left to get wrong; two would fail here, and would also fail the
    behavioural test below.
    """

    async def scenario():
        bridge, transport, _ = build("ordering-unit", silence=30.0)
        await bridge.start()

        scheduled = []
        bridge._schedule = scheduled.append          # capture, do not run

        generation_ended(bridge)                     # queue already empty

        count = len(scheduled)
        for coroutine in reversed(scheduled):        # adversarial order
            await coroutine
        boundaries = list(transport.boundaries)
        await bridge.close()
        return count, boundaries

    count, boundaries = run(scenario())

    assert count == 1, f"audio_end scheduled {count} tasks whose order matters"
    assert boundaries == ["1"], f"no boundary was issued: {boundaries}"


def test_a_boundary_is_issued_when_the_queue_is_already_empty():
    """Behavioural counterpart: the case the race actually lost.

    Nothing is queued when `audio_end` arrives, so the outbound pump will not
    run again. If the boundary is not issued by this handler it is never issued.
    """

    async def scenario():
        bridge, transport, _ = build("already-empty", silence=30.0)
        await bridge.start()

        audio(bridge, chunks=2)
        await drain_backend_queue(bridge, transport)
        assert not len(bridge.outbound), "the queue was not empty for this test"

        generation_ended(bridge)
        await settle()

        boundaries = list(transport.boundaries)
        # And it completes on the acknowledgement, as any other turn does.
        ack(bridge, transport)
        await settle()
        state = bridge.lifecycle.state
        await bridge.close()
        return boundaries, state

    boundaries, state = run(scenario())

    assert boundaries == ["1"], f"expected exactly one boundary, got {boundaries}"
    assert state is CallState.WAITING_FOR_CALLER


def test_a_zero_audio_turn_with_an_empty_queue_issues_exactly_one_boundary():
    """The most exposed case: no audio at all, so no pump activity whatsoever."""

    async def scenario():
        bridge, transport, _ = build("zero-audio-order", silence=30.0)
        await bridge.start()

        generation_ended(bridge)
        await settle()
        first = list(transport.boundaries)

        # A second `audio_end` for the same finished turn changes nothing.
        generation_ended(bridge)
        await settle()
        second = list(transport.boundaries)
        await bridge.close()
        return first, second

    first, second = run(scenario())

    assert first == ["1"], f"expected one boundary, got {first}"
    assert second == ["1"], f"a repeated audio_end issued another: {second}"


def test_the_pump_still_issues_the_boundary_when_it_drains_last():
    """The other ordering: `audio_end` first, queue drains afterwards.

    Both paths must work, and this one is what the handler cannot do alone.
    """

    async def scenario():
        bridge, transport, _ = build("pump-drains-last", silence=30.0)
        await bridge.start()

        # A transport slow enough that the queue is still full when the model
        # reports it has finished generating — which is the ordinary case on a
        # long answer.
        original = transport.send_audio

        async def slowly(frame):
            await asyncio.sleep(0.004)
            await original(frame)

        transport.send_audio = slowly

        for _ in range(30):
            bridge.on_realtime_event(
                bridge.banking_session_id,
                Event("audio", audio=Event("audio", data=PCM)),
            )
        generation_ended(bridge)
        await settle(0.02)

        still_queued = len(bridge.outbound)
        early = list(transport.boundaries)

        await drain_backend_queue(bridge, transport)
        await settle()
        late = list(transport.boundaries)
        await bridge.close()
        return still_queued, early, late

    still_queued, early, late = run(scenario())

    assert still_queued > 0, "the premise failed: the queue had already drained"
    assert early == [], "asked before the caller could have heard it"
    assert late == ["1"], f"the pump never asked: {late}"
