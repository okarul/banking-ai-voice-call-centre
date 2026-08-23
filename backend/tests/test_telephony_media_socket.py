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
from app.routers.telephony import MEDIA_TOKEN_HEADER
from app.telephony.bridge import phone_call_registry
from app.telephony.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_payload

TEST_SECRET = "phase3-media-socket-test-secret-not-a-real-credential"
WEBHOOK = "/api/telephony/incoming"


class FakePhoneSession:
    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.messages: list[str] = []
        self.closed = False
        self._events: asyncio.Queue = asyncio.Queue()

    async def send_audio(self, audio: bytes) -> None:
        self.audio_chunks.append(audio)

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

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

    TOKENS.clear()
    wipe()
    yield
    TOKENS.clear()
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


# The credential each announced call was issued, so a test can attach the way
# a gateway does. Cleared per test by the fixture.
TOKENS: dict[str, str] = {}


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
    accepted = client.post(
        WEBHOOK,
        content=body,
        headers={
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: sign_payload(TEST_SECRET, stamp, body),
        },
    ).json()
    if accepted.get("media_token"):
        TOKENS[call_id] = accepted["media_token"]
    return accepted


def media(client, call_id: str, *, token: str | None = ...):
    """Attach a media socket the way the gateway does: with its credential."""
    if token is ...:
        token = TOKENS.get(call_id)
    headers = {MEDIA_TOKEN_HEADER: token} if token else {}
    return client.websocket_connect(
        f"/api/telephony/media/{call_id}", headers=headers
    )


def frame(seed: int = 1) -> bytes:
    pcm = array.array("h", [seed * 700] * codec.ULAW_FRAME_BYTES).tobytes()
    return codec.pcm16_to_ulaw(pcm)


# === attaching ==============================================================


def test_a_socket_attaches_to_an_announced_call(client, sessions):
    assert announce(client, "call-ws")["status"] == "accepted"

    with media(client, "call-ws") as socket:
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
        with media(client, "never-announced") as socket:
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
    with media(client, "call-gone"):
        pass

    # The socket closing ended the call; a second attach finds nothing.
    with pytest.raises(WebSocketDisconnect):
        with media(client, "call-gone") as socket:
            socket.receive_bytes()


def test_assistant_audio_arrives_on_the_socket(client, sessions):
    announce(client, "call-out")

    with media(client, "call-out") as socket:
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

    with media(client, "call-x") as socket_x:
        with media(client, "call-y") as socket_y:
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

    with media(client, "call-text") as socket:
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

    with media(client, "call-bye"):
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

    with media(client, "call-both"):
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

    with media(client, "call-secret") as socket:
        socket.send_bytes(frame())

    try:
        with media(client, "bogus") as socket:
            socket.receive_bytes()
    except WebSocketDisconnect as error:
        assert TEST_SECRET not in str(error)

    assert TEST_SECRET not in client.get("/health").text


# === the greeting, over the real socket =====================================


def test_the_caller_is_greeted_when_the_gateway_attaches(client, sessions):
    """End to end: announce, attach, and the agent opens the conversation."""
    from app.telephony.bridge import GREETING_CUE, phone_call_registry

    announce(client, "call-greet")
    bridge = phone_call_registry.get("call-greet")

    # Nothing said yet — there was nobody holding the line to say it to.
    assert bridge.greeted is False
    assert sessions[0].messages == []

    with media(client, "call-greet"):
        for _ in range(200):
            if bridge.greeted:
                break
            time.sleep(0.01)

    assert bridge.greeted is True
    assert sessions[0].messages == [GREETING_CUE]


def test_two_attached_callers_are_greeted_once_each(client, sessions):
    from app.telephony.bridge import GREETING_CUE, phone_call_registry

    announce(client, "call-g1")
    announce(client, "call-g2")

    with media(client, "call-g1"):
        with media(client, "call-g2"):
            for _ in range(200):
                if all(s.messages for s in sessions[:2]):
                    break
                time.sleep(0.01)
            greeted = [
                phone_call_registry.get("call-g1").greeted,
                phone_call_registry.get("call-g2").greeted,
            ]

    assert greeted == [True, True]
    assert sessions[0].messages == [GREETING_CUE]
    assert sessions[1].messages == [GREETING_CUE]
    # One greeting each, not two for one caller and none for the other.
    assert sum(len(s.messages) for s in sessions[:2]) == 2


def test_a_reattaching_socket_does_not_greet_again(client, sessions):
    """A gateway that reconnects must not make the bank say hello twice."""
    from app.telephony.bridge import GREETING_CUE

    announce(client, "call-reattach")

    with media(client, "call-reattach"):
        for _ in range(200):
            if sessions[0].messages:
                break
            time.sleep(0.01)

    # The first socket closing ended the call, so a second attach is refused
    # outright — and in either case only one greeting was ever spoken.
    assert sessions[0].messages == [GREETING_CUE]


# === per-call media authentication (Phase 4) ================================
#
# Before this, anything that learned or guessed a `provider_call_id` could
# attach to a live banking call and both hear the caller and speak to them. The
# call id is an identifier, not a credential, and "the network is private" is a
# deployment assumption rather than a control.


def test_an_accepted_call_is_issued_a_credential(client, sessions):
    accepted = announce(client, "call-tok")

    assert accepted["status"] == "accepted"
    assert accepted["media_token"]
    assert len(accepted["media_token"]) >= 32
    assert accepted["media_url"] == "/api/telephony/media/call-tok"


def test_a_socket_without_a_credential_is_refused(client, sessions):
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-notok")

    with pytest.raises(WebSocketDisconnect):
        with media(client, "call-notok", token=None) as socket:
            socket.receive_bytes()

    # And the call is untouched: still live, still holding its slot.
    assert phone_call_registry.get("call-notok") is not None
    assert voice_call_manager.used_capacity() == 1
    assert sessions[0].messages == [], "greeted an unauthenticated socket"


def test_a_socket_with_a_wrong_credential_is_refused(client, sessions):
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-badtok")

    with pytest.raises(WebSocketDisconnect):
        with media(client, "call-badtok", token="not-the-right-token") as socket:
            socket.receive_bytes()

    assert phone_call_registry.get("call-badtok") is not None


def test_one_call_credential_does_not_open_another_call(client, sessions):
    """The decisive test: tokens are per call, not per gateway."""
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-mine")
    announce(client, "call-yours")

    with pytest.raises(WebSocketDisconnect):
        with media(client, "call-yours", token=TOKENS["call-mine"]) as socket:
            socket.receive_bytes()

    # Neither call was disturbed, and neither was greeted by the attempt.
    assert phone_call_registry.get("call-mine") is not None
    assert phone_call_registry.get("call-yours") is not None
    assert all(session.messages == [] for session in sessions[:2])


def test_a_credential_cannot_be_used_twice(client, sessions):
    """Single-use: a replayed socket must not take over a live call."""
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-replay")
    token = TOKENS["call-replay"]

    with media(client, "call-replay", token=token):
        pass

    # The first socket closing ended the call; the replay is refused either way,
    # and the important part is that it is refused rather than accepted.
    with pytest.raises(WebSocketDisconnect):
        with media(client, "call-replay", token=token) as socket:
            socket.receive_bytes()


def test_a_second_socket_cannot_hijack_a_live_call(client, sessions):
    """While the first socket is still attached, a replay must not displace it."""
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-live")
    token = TOKENS["call-live"]

    with media(client, "call-live", token=token) as first:
        with pytest.raises(WebSocketDisconnect):
            with media(client, "call-live", token=token) as second:
                second.receive_bytes()

        # The original socket still works: the hijack attempt changed nothing.
        first.send_bytes(frame())
        for _ in range(200):
            if sessions[0].audio_chunks:
                break
            time.sleep(0.01)

    assert len(sessions[0].audio_chunks) == 1


def test_an_expired_credential_is_refused():
    """A token that leaks is useless once its attach window has passed."""
    import asyncio as aio

    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import WebSocketMediaTransport

    bridge = PhoneCallBridge(
        provider_call_id="call-expiry",
        banking_session_id="SESSION-x",
        transport=WebSocketMediaTransport(max_frames=10),
        realtime_manager=None,
        outbound_max_frames=10,
        media_token_ttl=-1.0,
    )

    assert bridge.consume_media_token(bridge.media_token) is False
    aio.run(bridge.close())


def test_a_credential_carries_no_identity_and_no_auth_state():
    """A media credential that carried identity would make attaching a socket
    a way to assert one."""
    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import WebSocketMediaTransport

    bridge = PhoneCallBridge(
        provider_call_id="DEMO001",
        banking_session_id="SESSION-DEMO001-abc",
        transport=WebSocketMediaTransport(max_frames=10),
        realtime_manager=None,
        outbound_max_frames=10,
    )
    token = bridge.media_token

    for leaked in ("DEMO001", "SESSION", "authenticated", "customer", "verified"):
        assert leaked.lower() not in token.lower(), leaked


def test_two_calls_get_different_credentials():
    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import WebSocketMediaTransport

    def build(call_id):
        return PhoneCallBridge(
            provider_call_id=call_id,
            banking_session_id=f"SESSION-{call_id}",
            transport=WebSocketMediaTransport(max_frames=10),
            realtime_manager=None,
            outbound_max_frames=10,
        )

    tokens = {build(f"call-{n}").media_token for n in range(20)}
    assert len(tokens) == 20


def test_a_refusal_is_issued_no_credential(client, sessions, monkeypatch):
    """A call that was not accepted has nothing to attach to."""
    from app.config import settings as live

    monkeypatch.setattr(live, "realtime_max_active_sessions", 1)
    announce(client, "call-first")
    refused = announce(client, "call-overflow")

    assert refused["status"] == "rejected_capacity"
    assert "media_token" not in refused
    assert "media_url" not in refused


def test_a_duplicate_is_issued_no_credential(client, sessions):
    announce(client, "call-dup-tok")
    duplicate = announce(client, "call-dup-tok")

    assert duplicate["status"] == "duplicate"
    assert "media_token" not in duplicate


def test_the_credential_is_never_logged(client, sessions, caplog):
    with caplog.at_level("INFO"):
        announce(client, "call-quiet")
        token = TOKENS["call-quiet"]
        with media(client, "call-quiet"):
            time.sleep(0.05)

    assert token not in caplog.text
    # A refused attempt does not log the credential it was given either.
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-quiet2")
    with caplog.at_level("WARNING"):
        try:
            with media(client, "call-quiet2", token="leaked-secret-value") as socket:
                socket.receive_bytes()
        except WebSocketDisconnect:
            pass
    assert "leaked-secret-value" not in caplog.text


def test_the_credential_is_absent_from_the_operator_view(client, sessions):
    announce(client, "call-describe-tok")
    described = phone_call_registry.get("call-describe-tok").describe()

    assert TOKENS["call-describe-tok"] not in str(described)
    assert "media_token" not in described
    assert described["media_attached"] is False


# === Phase 6.2: the application ends the call, and the socket agrees ========
#
# The live symptom: the bank said "I do not hear anything from you. Thank you."
# and the telephone stayed connected. The route was blocked in
# `websocket.receive()` waiting for a caller who had been disconnected in every
# sense but the socket, so the gateway's iterator never ended, `handle_call`
# never returned, and the outbound SIP BYE never ran.


def test_the_media_socket_closes_when_the_application_ends_the_call(client, sessions):
    """What the gateway needs to see so its relay can finish."""
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-append")
    bridge = phone_call_registry.get("call-append")

    with media(client, "call-append") as socket:
        socket.send_bytes(frame())
        for _ in range(200):
            if sessions[0].audio_chunks:
                break
            time.sleep(0.01)

        # The bank finishes with the call, exactly as the lifecycle does.
        asyncio.run(service.tear_down("call-append", bridge.banking_session_id))

        # The far side — the gateway, in production — observes the close.
        with pytest.raises(WebSocketDisconnect):
            for _ in range(200):
                socket.receive_bytes()

    assert bridge.closed is True


def test_the_gateway_side_iterator_finishes_so_handle_call_can_return(
    client, sessions
):
    """The circular wait, broken.

    A gateway relays with `async for message in socket`. If that never ends,
    `handle_call` never returns and the SIP BYE never fires. This asserts the
    iterator terminates once the application ends the call.
    """
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-iter")
    bridge = phone_call_registry.get("call-iter")

    ended = False
    with media(client, "call-iter") as socket:
        asyncio.run(service.tear_down("call-iter", bridge.banking_session_id))
        try:
            for _ in range(500):
                socket.receive_bytes()
        except WebSocketDisconnect:
            ended = True

    assert ended is True, "the relay would have blocked for ever"


def test_the_routes_teardown_stays_idempotent(client, sessions):
    """The route's `finally` runs after the application already tore down."""
    announce(client, "call-twice")
    bridge = phone_call_registry.get("call-twice")
    banking_session_id = bridge.banking_session_id

    with media(client, "call-twice"):
        asyncio.run(service.tear_down("call-twice", banking_session_id))
        # And again, as the route's finally will.
        asyncio.run(service.tear_down("call-twice", banking_session_id))

    asyncio.run(service.tear_down("call-twice", banking_session_id))

    assert phone_call_registry.get("call-twice") is None
    assert voice_call_manager.used_capacity() == 0


def test_a_caller_hangup_still_works_unchanged(client, sessions):
    """The path that always worked must keep working."""
    announce(client, "call-hangup")
    assert voice_call_manager.used_capacity() == 1

    with media(client, "call-hangup"):
        pass  # the caller goes away

    for _ in range(200):
        if voice_call_manager.used_capacity() == 0:
            break
        time.sleep(0.01)

    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    assert sessions[0].closed is True


def test_an_application_ended_call_leaks_nothing(client, sessions):
    """No bridge, no banking session, no capacity, no model session."""
    from app.sessions import session_manager

    announce(client, "call-noleak")
    bridge = phone_call_registry.get("call-noleak")
    banking_session_id = bridge.banking_session_id
    assert voice_call_manager.used_capacity() == 1

    with media(client, "call-noleak"):
        asyncio.run(service.tear_down("call-noleak", banking_session_id))

    for _ in range(200):
        if voice_call_manager.used_capacity() == 0:
            break
        time.sleep(0.01)

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert session_manager.get_session(banking_session_id) is None
    assert sessions[0].closed is True


def test_ending_one_call_does_not_close_another_callers_socket(client, sessions):
    """Two calls in flight; only the one that ended is disconnected."""
    from starlette.websockets import WebSocketDisconnect

    announce(client, "call-one")
    announce(client, "call-two")
    first = phone_call_registry.get("call-one")

    with media(client, "call-one") as socket_one:
        with media(client, "call-two") as socket_two:
            asyncio.run(service.tear_down("call-one", first.banking_session_id))

            with pytest.raises(WebSocketDisconnect):
                for _ in range(200):
                    socket_one.receive_bytes()

            # The survivor is untouched and still carrying audio.
            socket_two.send_bytes(frame(2))
            for _ in range(200):
                if len(sessions) > 1 and sessions[1].audio_chunks:
                    break
                time.sleep(0.01)

            survivor = phone_call_registry.get("call-two")
            assert survivor is not None
            assert survivor.closed is False

    assert len(sessions[1].audio_chunks) >= 1


def test_the_closing_audio_is_delivered_before_the_socket_closes(client, sessions):
    """The closing sentence must not be cut off.

    Every `send_audio` is awaited before the bank's queue drains, so the close
    frame is queued behind the audio rather than racing it.
    """
    import array

    announce(client, "call-lastword")
    bridge = phone_call_registry.get("call-lastword")

    with media(client, "call-lastword") as socket:
        pcm = array.array("h", [1200] * 480).tobytes()
        sessions[0].emit(FakeEvent("audio", audio=FakeEvent("audio", data=pcm)))
        heard = socket.receive_bytes()

        asyncio.run(service.tear_down("call-lastword", bridge.banking_session_id))

    assert len(heard) == codec.ULAW_FRAME_BYTES
