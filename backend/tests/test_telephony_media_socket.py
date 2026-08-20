"""The media socket: one call, one socket, and no way to invent a call.

The webhook is protected by an HMAC. This socket is protected by the fact that
it can only ever *attach to* something the webhook already created — it has no
path that brings a call into existence. That property is the one worth testing
hardest, because a media socket that could open a banking session would be a
way around the signature check entirely.

These run through the real application over a real (in-process) WebSocket, with
a fake model session standing in for OpenAI.

All customers and PINs here are synthetic seed data.
"""

import array
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.main import create_app
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import audio as codec
from app.telephony import service
from app.telephony.bridge import phone_call_registry
from app.telephony.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_payload

TEST_SECRET = "phase3-media-socket-test-secret-not-a-real-credential"
WEBHOOK = "/api/telephony/incoming"


class FakePhoneSession:
    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.closed = False
        self._events: asyncio.Queue = asyncio.Queue()

    async def send_audio(self, audio: bytes) -> None:
        self.audio_chunks.append(audio)

    async def close(self) -> None:
        self.closed = True

    async def __aiter__(self):
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def emit(self, event) -> None:
        self._events.put_nowait(event)


class FakeEvent:
    def __init__(self, type_: str, **fields) -> None:
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


@pytest.fixture(autouse=True)
def clean_state():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    asyncio.run(phone_call_registry.close_all())
    asyncio.run(voice_call_manager.close_all())
    asyncio.run(voice_call_manager.release_all())
    session_manager.clear()
    wipe()


@pytest.fixture
def sessions(monkeypatch):
    """Telephony on, websocket media, fake model sessions."""
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "telephony_media_transport", "websocket")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 5)

    made: list[FakePhoneSession] = []

    async def connect(_context):
        session = FakePhoneSession()
        made.append(session)
        return session

    monkeypatch.setattr(service, "open_phone_realtime_session", connect)
    return made


@pytest.fixture
def client(sessions):
    # As a context manager, so the app keeps one event loop across requests and
    # the bridges' pump tasks survive between them.
    with TestClient(create_app()) as running:
        yield running


def announce(client, call_id: str) -> dict:
    body = json.dumps(
        {
            "provider": "TEST",
            "provider_event_id": f"evt-{call_id}",
            "provider_call_id": call_id,
            "event_type": "incoming",
            "event_timestamp": "2026-08-20T10:00:00Z",
        }
    ).encode()
    stamp = str(int(time.time()))
    return client.post(
        WEBHOOK,
        content=body,
        headers={
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: sign_payload(TEST_SECRET, stamp, body),
        },
    ).json()


def frame(seed: int = 1) -> bytes:
    pcm = array.array("h", [seed * 700] * codec.ULAW_FRAME_BYTES).tobytes()
    return codec.pcm16_to_ulaw(pcm)


# === attaching ==============================================================


def test_a_socket_attaches_to_an_announced_call(client, sessions):
    assert announce(client, "call-ws")["status"] == "accepted"

    with client.websocket_connect("/api/telephony/media/call-ws") as socket:
        socket.send_bytes(frame())
        # The frame reaches this call's model session, converted on the way.
        for _ in range(200):
            if sessions[0].audio_chunks:
                break
            time.sleep(0.01)

    assert len(sessions[0].audio_chunks) == 1
    assert len(sessions[0].audio_chunks[0]) == codec.ULAW_FRAME_BYTES * 2 * 3


def test_a_socket_for_an_unannounced_call_is_refused(client, sessions):
    """The property that matters: a socket cannot create a call."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/telephony/media/never-announced") as socket:
            socket.send_bytes(frame())
            socket.receive_bytes()

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert sessions == []
    with session_scope() as db:
        assert db.scalars(select(AgentSession)).all() == []


def test_a_socket_for_an_ended_call_is_refused(client, sessions):
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-gone")
    with client.websocket_connect("/api/telephony/media/call-gone"):
        pass

    # The socket closing ended the call; a second attach finds nothing.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/telephony/media/call-gone") as socket:
            socket.receive_bytes()


def test_assistant_audio_arrives_on_the_socket(client, sessions):
    announce(client, "call-out")

    with client.websocket_connect("/api/telephony/media/call-out") as socket:
        pcm = array.array("h", [1500] * 480).tobytes()
        sessions[0].emit(FakeEvent("audio", audio=FakeEvent("audio", data=pcm)))
        received = socket.receive_bytes()

    # Converted to what a telephone carries: one 20 ms µ-law frame.
    assert len(received) == codec.ULAW_FRAME_BYTES
    assert received == codec.model_to_telephony(pcm)


def test_two_calls_each_get_their_own_socket_and_audio(client, sessions):
    """Two sockets, two bridges, two model sessions, nothing shared."""
    announce(client, "call-x")
    announce(client, "call-y")

    with client.websocket_connect("/api/telephony/media/call-x") as socket_x:
        with client.websocket_connect("/api/telephony/media/call-y") as socket_y:
            socket_x.send_bytes(frame(1))
            socket_y.send_bytes(frame(2))
            for _ in range(200):
                if all(session.audio_chunks for session in sessions[:2]):
                    break
                time.sleep(0.01)

            # Checked while both are live: closing either socket ends its call,
            # so after the `with` blocks the registry is legitimately empty.
            bridges = {
                phone_call_registry.get("call-x"),
                phone_call_registry.get("call-y"),
            }
            assert len(bridges) == 2
            assert None not in bridges

    assert len(sessions[0].audio_chunks) == 1
    assert len(sessions[1].audio_chunks) == 1
    # Different callers sent different audio, and neither received the other's.
    assert sessions[0].audio_chunks != sessions[1].audio_chunks
    assert sessions[0].audio_chunks[0] == codec.telephony_to_model(frame(1))
    assert sessions[1].audio_chunks[0] == codec.telephony_to_model(frame(2))

    # Both sockets closed, so both calls released everything they held.
    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0


def test_a_text_message_on_the_media_socket_is_ignored(client, sessions):
    """This socket carries audio. A control channel here would be a second
    way to affect a call."""
    announce(client, "call-text")

    with client.websocket_connect("/api/telephony/media/call-text") as socket:
        socket.send_text(json.dumps({"event": "hangup", "customer_id": "DEMO001"}))
        socket.send_bytes(frame())
        for _ in range(200):
            if sessions[0].audio_chunks:
                break
            time.sleep(0.01)

    assert len(sessions[0].audio_chunks) == 1
    with session_scope() as db:
        row = db.scalars(select(AgentSession)).one()
    assert row.customer_id is None


# === cleanup ================================================================


def test_the_caller_going_away_releases_the_call(client, sessions):
    announce(client, "call-bye")
    assert voice_call_manager.used_capacity() == 1

    with client.websocket_connect("/api/telephony/media/call-bye"):
        pass

    for _ in range(200):
        if voice_call_manager.used_capacity() == 0:
            break
        time.sleep(0.01)

    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    assert sessions[0].closed is True

    with session_scope() as db:
        row = db.scalars(select(AgentSession)).one()
    assert row.status == "COMPLETED"
    assert row.ended_at is not None


def test_a_disconnect_and_a_provider_end_event_release_one_slot_between_them(
    client, sessions
):
    """Both arrive; the call ends once and one slot comes back."""
    announce(client, "call-both")
    announce(client, "call-other")
    assert voice_call_manager.used_capacity() == 2

    with client.websocket_connect("/api/telephony/media/call-both"):
        pass

    body = json.dumps(
        {
            "provider": "TEST",
            "provider_event_id": "evt-both-end",
            "provider_call_id": "call-both",
            "event_type": "ended",
            "event_timestamp": "2026-08-20T10:00:00Z",
        }
    ).encode()
    stamp = str(int(time.time()))
    client.post(
        WEBHOOK,
        content=body,
        headers={
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: sign_payload(TEST_SECRET, stamp, body),
        },
    )

    # The other call still holds exactly its own slot.
    assert voice_call_manager.used_capacity() == 1
    assert phone_call_registry.get("call-other") is not None


def test_the_media_socket_never_leaks_a_secret(client, sessions):
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-secret")

    with client.websocket_connect("/api/telephony/media/call-secret") as socket:
        socket.send_bytes(frame())

    try:
        with client.websocket_connect("/api/telephony/media/bogus") as socket:
            socket.receive_bytes()
    except WebSocketDisconnect as error:
        assert TEST_SECRET not in str(error)

    assert TEST_SECRET not in client.get("/health").text
