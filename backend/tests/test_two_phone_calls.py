"""Phase 7.2: two telephone calls at once, through the real event pump.

`test_two_session_concurrency.py` proves two callers cannot reach each other's
money, but it drives the *tool layer* directly. `test_telephony_media.py` runs
several bridges, but not through `RealtimeManager._pump_events`. Nothing until
now put two `PhoneCallBridge` instances side by side and fed them the events a
provider actually sends.

That gap matters for Phase 7, because every isolation claim about a
multi-caller deployment rests on state that is per-call *by construction* -
one `Session` in a keyed dict, one bridge in a keyed registry, one lifecycle
object, one trace anchor - and construction is exactly what an offline test can
check without spending anything.

Overlap here is deterministic. Both calls are driven from one event loop and
interleaved with `asyncio.Event`, never with sleeps: a test that waits a tenth
of a second to "let the other call get ahead" is a test that passes on a fast
machine and fails on a slow one, and it would not be evidence of anything.

Nothing is billable: both calls use injected connectors.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.auth import authentication
from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, CallTraceEvent
from app.observability import recorder, trace
from app.realtime.browser_calls import voice_call_manager
from app.realtime.realtime_manager import RealtimeConnection, RealtimeManager
from app.sessions import SessionManager
from app.telephony.bridge import PhoneCallBridge, phone_call_registry
from app.telephony.media import LoopbackMediaTransport

PINS = {"DEMO001": "4821", "DEMO002": "7315"}
PCM = b"\x00\x10" * 480


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def traced_and_clean():
    """Tracing on, so isolation is observable; every table clean either side."""
    previous = (settings.trace_enabled, settings.telephony_trace_utterances)
    settings.trace_enabled = True
    settings.telephony_trace_utterances = True
    with session_scope() as db:
        db.execute(delete(CallTraceEvent))
    yield
    with session_scope() as db:
        db.execute(delete(CallTraceEvent))
    run(phone_call_registry.close_all())
    run(voice_call_manager.close_all())
    run(voice_call_manager.release_all())
    settings.trace_enabled, settings.telephony_trace_utterances = previous


# --- the shapes the SDK delivers -------------------------------------------


class Simple:
    def __init__(self, type_, **kw):
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class Content:
    def __init__(self, transcript):
        self.type = "audio"
        self.transcript = transcript
        self.text = None


class Item:
    def __init__(self, item_id, role, text, status=None):
        self.item_id = item_id
        self.type = "message"
        self.role = role
        self.content = [Content(text)]
        self.status = status


def caller_started():
    return Simple(
        "raw_model_event",
        data=Simple(
            "raw_server_event", data={"type": "input_audio_buffer.speech_started"}
        ),
    )


def transcript(text, item_id):
    return Simple(
        "raw_model_event",
        data=Simple(
            "input_audio_transcription_completed", transcript=text, item_id=item_id
        ),
    )


def agent_said(text, item_id):
    return Simple("history_added", item=Item(item_id, "assistant", text, "completed"))


def final_agent_text(text, item_id):
    return Simple(
        "raw_model_event",
        data=Simple(
            "raw_server_event",
            data={
                "type": "response.output_audio_transcript.done",
                "item_id": item_id,
                "transcript": text,
                "response_id": f"resp-{item_id}",
                "content_index": 0,
                "output_index": 0,
            },
        ),
    )


class ScriptedSession:
    """A provider session that yields one call's events, on cue.

    The `ready` event is what makes the interleaving deterministic: the test
    releases one call's next event only when it wants it, so both calls are
    genuinely in flight without either being timed.
    """

    def __init__(self, steps, gate):
        self._steps = list(steps)
        self._gate = gate
        self.closed = False

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for step in self._steps:
            await self._gate.wait_turn()
            yield step


class Interleaver:
    """Lets two scripted sessions take strict alternating turns."""

    def __init__(self):
        self._turn = 0
        self._events = [asyncio.Event(), asyncio.Event()]
        self._events[0].set()

    def gate_for(self, index):
        interleaver = self

        class Gate:
            async def wait_turn(self):
                await interleaver._events[index].wait()
                interleaver._events[index].clear()
                interleaver._advance(index)

        return Gate()

    def _advance(self, index):
        self._events[1 - index].set()

    def release_all(self):
        for event in self._events:
            event.set()


class Caller:
    """One telephone call: session, bridge, transport and scripted events."""

    def __init__(self, manager, customer_id, gate):
        self.customer_id = customer_id
        self.manager = manager
        self.banking = manager.create_session()
        self.session_id = self.banking.session_id
        self.call_id = f"pair-{customer_id}-{uuid.uuid4()}"
        recorder.claim_phone_call(
            self.session_id,
            provider_call_id=self.call_id,
            provider_event_id=f"evt-{self.call_id}",
        )
        self.transport = LoopbackMediaTransport()
        self.realtime = _Realtime()
        self.bridge = PhoneCallBridge(
            provider_call_id=self.call_id,
            banking_session_id=self.session_id,
            transport=self.transport,
            realtime_manager=self.realtime,
            outbound_max_frames=200,
        )
        self.bridge.conversation.session_manager = manager
        self.gate = gate

    @property
    def session(self):
        return self.manager.get_session(self.session_id)

    def verify(self):
        authentication.verify_customer(
            self.session_id, self.customer_id, manager=self.manager
        )
        authentication.verify_pin(
            self.session_id, PINS[self.customer_id], manager=self.manager
        )

    def rows(self):
        replay = trace.for_call(self.call_id)
        return replay["events"]


class _Realtime:
    def __init__(self):
        self.audio_frames = 0
        self.messages = []

    async def send_audio(self, *_a, **_k):
        self.audio_frames += 1

    async def send_message(self, _sid, text):
        self.messages.append(text)


def drive(caller, steps, pump):
    """Feed one call's events through the real pump into its own bridge."""
    connection = RealtimeConnection(
        banking_session_id=caller.session_id,
        realtime_session_id=f"rt-{caller.call_id}",
        session=ScriptedSession(steps, caller.gate),
    )
    return pump._pump_events(connection, caller.bridge.on_realtime_event)


# === F: two callers, overlapping, through the real pump =====================


@pytest.mark.trace
def test_two_telephone_calls_overlap_without_touching_each_other():
    """The whole of Section F, as one call each way."""

    async def scenario():
        manager = SessionManager()
        pump = RealtimeManager(manager=manager)
        interleaver = Interleaver()

        a = Caller(manager, "DEMO001", interleaver.gate_for(0))
        b = Caller(manager, "DEMO002", interleaver.gate_for(1))

        await a.bridge.start()
        await b.bridge.start()

        a.verify()
        b.verify()

        a_steps = [
            caller_started(),
            transcript("What is my savings balance?", "a-user-1"),
            agent_said("Your savings balance is 12,450.75 SGD.", "a-agent-1"),
            final_agent_text("Your savings balance is 12,450.75 SGD.", "a-agent-1"),
            caller_started(),
            transcript("Thank you. Goodbye.", "a-user-2"),
        ]
        b_steps = [
            caller_started(),
            transcript("How much is left on my home loan?", "b-user-1"),
            agent_said("Your home loan balance is 284,500.00 SGD.", "b-agent-1"),
            final_agent_text("Your home loan balance is 284,500.00 SGD.", "b-agent-1"),
            caller_started(),
            transcript("That is all, thank you, goodbye.", "b-user-2"),
        ]

        # Strictly alternating, so neither call ever runs to completion first.
        await asyncio.gather(drive(a, a_steps, pump), drive(b, b_steps, pump))
        interleaver.release_all()

        for _ in range(8):
            await asyncio.sleep(0)
        await a.bridge.close()
        await b.bridge.close()
        for _ in range(8):
            await asyncio.sleep(0)
        return a, b

    a, b = run(scenario())

    # --- identity ---------------------------------------------------------
    assert a.call_id != b.call_id
    assert a.session_id != b.session_id
    assert a.session.customer_id == "DEMO001"
    assert b.session.customer_id == "DEMO002"
    assert a.session.authenticated and b.session.authenticated

    # --- no shared conversation state -------------------------------------
    assert a.session.conversation_context is not b.session.conversation_context
    assert a.bridge.lifecycle is not b.bridge.lifecycle
    assert a.bridge.conversation is not b.bridge.conversation
    assert a.realtime is not b.realtime

    # --- traces belong to their own call ----------------------------------
    a_rows, b_rows = a.rows(), b.rows()
    assert a_rows and b_rows

    a_text = " ".join(str(e.get("utterance") or "") for e in a_rows)
    b_text = " ".join(str(e.get("utterance") or "") for e in b_rows)
    assert "284,500" not in a_text, "caller B's loan balance reached caller A"
    assert "12,450" not in b_text, "caller A's savings balance reached caller B"

    a_refs = {e.get("customer_ref") for e in a_rows} - {None}
    b_refs = {e.get("customer_ref") for e in b_rows} - {None}
    assert not (a_refs & b_refs), f"a customer reference is shared: {a_refs & b_refs}"

    # --- each replay is monotonic in its own right ------------------------
    for rows in (a_rows, b_rows):
        sequences = [e["sequence"] for e in rows]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

    # --- and each row is filed against the right agent session ------------
    with session_scope() as db:
        for caller in (a, b):
            agent = db.scalars(
                select(AgentSession).where(
                    AgentSession.provider_call_id == caller.call_id
                )
            ).one()
            rows = db.scalars(
                select(CallTraceEvent).where(
                    CallTraceEvent.provider_call_id == caller.call_id
                )
            ).all()
            assert rows
            assert {r.session_pk for r in rows} == {agent.id}


# === G: both calls end at once ==============================================


@pytest.mark.trace
def test_two_calls_tearing_down_together_release_exactly_one_slot_each():
    """Section G. Concurrent teardown, deterministic, no sleeps."""

    async def scenario():
        manager = SessionManager()

        class FakeSession:
            async def close(self):
                return None

        async def connector(_context):
            return FakeSession()

        first = manager.create_session().session_id
        second = manager.create_session().session_id

        # The authoritative pool, borrowed and given back. Leaving a test's
        # session manager attached to a process-wide object is how one file's
        # teardown becomes another file's mystery.
        original = voice_call_manager._manager
        voice_call_manager._manager = manager
        try:
            await voice_call_manager.start(first, connect=connector)
            await voice_call_manager.start(second, connect=connector)
            assert voice_call_manager.used_capacity() == 2

            # Both endings land in the same scheduling pass.
            results = await asyncio.gather(
                voice_call_manager.close(first),
                voice_call_manager.close(second),
                voice_call_manager.close(first),
                voice_call_manager.close(second),
                return_exceptions=True,
            )
        finally:
            voice_call_manager._manager = original
        return results

    results = run(scenario())

    assert results.count(True) == 2, f"a slot was released twice: {results}"
    assert results.count(False) == 2, "a repeat close was not a no-op"
    assert voice_call_manager.used_capacity() == 0
    assert voice_call_manager._connections == {}
    assert voice_call_manager._reserved == set()
    assert phone_call_registry.active_count() == 0

    from app.observability import readiness

    capacity = readiness.readiness_report()["checks"]["capacity"]
    assert capacity["in_use"] == 0, "readiness still reports a call in flight"
