"""Phase 6.8 Workstream D: the two ends say what they are, before it matters.

The playback boundary only works if the far end answers it. A backend that
expects an acknowledgement from a gateway too old to send one waits for ever:
the turn never completes, the silence timer never arms, and the call sits open
with nobody able to say why. Deploying the two together is the intent, but
intent is not a mechanism — and a hung call is a bad way to find out that a
release went out in halves.

So they negotiate before the caller is greeted, and a mismatch becomes a
refusal an operator can read.
"""

import asyncio

import pytest
from sqlalchemy import delete, select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import media as app_protocol
from app.telephony.bridge import phone_call_registry
from app.telephony.media import (
    FEATURE_PLAYBACK_ACK,
    PROTOCOL_HELLO,
    PROTOCOL_READY,
    PROTOCOL_VERSION,
    WebSocketMediaTransport,
    protocol_hello_message,
    read_protocol_message,
)
from gateway import control as gateway_protocol


@pytest.fixture(autouse=True)
def clean():
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


def run(coro):
    return asyncio.run(coro)


class FakeSocket:
    def __init__(self):
        self.text = []
        self.closed = 0

    async def send_text(self, payload):
        self.text.append(payload)

    async def send_bytes(self, payload):
        return None

    async def close(self, code=1000):
        self.closed += 1


def attached():
    transport = WebSocketMediaTransport(max_frames=50)
    socket = FakeSocket()
    transport.attach(socket)
    return transport, socket


def ready(version=PROTOCOL_VERSION, features=(FEATURE_PLAYBACK_ACK,)):
    return version, tuple(features)


# === the contract itself ====================================================


def test_the_two_ends_speak_the_same_version():
    """The one assertion that would catch a half-deployed release at build time."""
    assert app_protocol.PROTOCOL_VERSION == gateway_protocol.PROTOCOL_VERSION
    assert app_protocol.FEATURE_PLAYBACK_ACK == gateway_protocol.FEATURE_PLAYBACK_ACK


def test_the_hello_and_the_answer_round_trip():
    asked = protocol_hello_message()
    assert gateway_protocol.read_protocol_message(asked) == (
        gateway_protocol.PROTOCOL_HELLO,
        PROTOCOL_VERSION,
        (FEATURE_PLAYBACK_ACK,),
    )

    answered = gateway_protocol.protocol_ready_message()
    assert read_protocol_message(answered) == (
        PROTOCOL_READY,
        PROTOCOL_VERSION,
        (FEATURE_PLAYBACK_ACK,),
    )


def test_the_negotiation_carries_nothing_but_version_and_features():
    """No customer, no banking data, no credential may ride here."""
    import json

    for payload in (protocol_hello_message(), gateway_protocol.protocol_ready_message()):
        assert set(json.loads(payload)) == {"type", "version", "features"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        '{"type": "protocol_ready"}',
        '{"type": "protocol_ready", "version": "1", "features": []}',
        '{"type": "protocol_ready", "version": 1, "features": "playback_ack"}',
        '{"type": "protocol_ready", "version": true, "features": []}',
        '{"type": "protocol_ready", "version": -1, "features": []}',
        '{"type": "something", "version": 1, "features": []}',
        '{"version": 1, "features": []}',
    ],
)
def test_a_malformed_negotiation_frame_is_rejected_by_both_ends(text):
    assert read_protocol_message(text) is None
    assert gateway_protocol.read_protocol_message(text) is None


def test_an_oversized_feature_list_is_rejected():
    import json

    payload = json.dumps(
        {"type": PROTOCOL_READY, "version": 1, "features": ["f"] * 17}
    )
    assert read_protocol_message(payload) is None


def test_an_overlong_feature_name_is_rejected():
    import json

    payload = json.dumps(
        {"type": PROTOCOL_READY, "version": 1, "features": ["x" * 33]}
    )
    assert read_protocol_message(payload) is None


# === the transport's half ===================================================


def test_a_matching_answer_makes_the_transport_compatible():
    transport, socket = attached()

    async def scenario():
        assert await transport.send_protocol_hello() is True
        transport.on_protocol_ready(*ready())
        return await transport.wait_for_protocol(timeout=1.0)

    assert run(scenario()) is True
    assert transport.compatible is True
    assert socket.text == [protocol_hello_message()]


def test_a_missing_playback_ack_capability_fails_fast():
    """The exact hang this exists to prevent: right version, wrong gateway."""
    transport, _ = attached()

    async def scenario():
        transport.on_protocol_ready(*ready(features=("audio_only",)))
        return await transport.wait_for_protocol(timeout=1.0)

    assert run(scenario()) is False
    assert transport.compatible is False


def test_an_unsupported_version_fails_fast():
    transport, _ = attached()

    async def scenario():
        transport.on_protocol_ready(*ready(version=PROTOCOL_VERSION + 1))
        return await transport.wait_for_protocol(timeout=1.0)

    assert run(scenario()) is False
    assert transport.peer_version == PROTOCOL_VERSION + 1


def test_an_older_version_also_fails_fast():
    transport, _ = attached()

    async def scenario():
        transport.on_protocol_ready(*ready(version=0))
        return await transport.wait_for_protocol(timeout=1.0)

    assert run(scenario()) is False


def test_a_silent_far_end_times_out_rather_than_waiting_for_ever():
    """A gateway too old to answer must not hold a call open indefinitely."""
    transport, _ = attached()

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await transport.wait_for_protocol(timeout=0.2)
        return result, loop.time() - started

    result, elapsed = run(scenario())

    assert result is False
    assert elapsed < 2.0, "the wait was not bounded"


def test_repeating_the_answer_is_idempotent():
    transport, _ = attached()

    async def scenario():
        for _ in range(3):
            transport.on_protocol_ready(*ready())
        return await transport.wait_for_protocol(timeout=1.0)

    assert run(scenario()) is True
    assert transport.compatible is True


def test_a_late_bad_answer_cannot_undo_an_agreed_protocol():
    transport, _ = attached()

    async def scenario():
        transport.on_protocol_ready(*ready())
        agreed = await transport.wait_for_protocol(timeout=1.0)
        transport.on_protocol_ready(*ready(version=99))
        return agreed, transport.compatible

    agreed, still = run(scenario())

    assert agreed is True
    assert still is True, "a stray frame downgraded a working call"


def test_a_call_that_ends_while_negotiating_releases_the_waiter():
    transport, _ = attached()

    async def scenario():
        waiting = asyncio.create_task(transport.wait_for_protocol(timeout=5.0))
        await asyncio.sleep(0.05)
        await transport.on_call_ended()
        return await asyncio.wait_for(waiting, timeout=2.0)

    assert run(scenario()) is False


def test_two_calls_cannot_negotiate_for_one_another():
    first, _ = attached()
    second, _ = attached()

    async def scenario():
        first.on_protocol_ready(*ready())
        return (
            await first.wait_for_protocol(timeout=1.0),
            await second.wait_for_protocol(timeout=0.2),
        )

    first_ok, second_ok = run(scenario())

    assert first_ok is True
    assert second_ok is False, "one call's answer satisfied another"


def test_a_transport_with_no_far_end_is_compatible_by_definition():
    from app.telephony.media import LoopbackMediaTransport

    async def scenario():
        return await LoopbackMediaTransport().wait_for_protocol(timeout=0.1)

    assert run(scenario()) is True


def test_a_hello_cannot_be_sent_once_the_call_has_ended():
    transport, _ = attached()

    async def scenario():
        await transport.on_call_ended()
        return await transport.send_protocol_hello()

    assert run(scenario()) is False


# === the gateway's half =====================================================


class _GatewaySocket:
    def __init__(self, messages):
        self._messages = list(messages)
        self.replies = []

    async def send(self, payload):
        self.replies.append(payload)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self._messages:
            yield message


def _gateway():
    import os

    from gateway.client import BackendClient
    from gateway.config import GatewaySettings
    from gateway.service import MediaGateway

    os.environ.setdefault("GATEWAY_WEBHOOK_SECRET", "protocol-test-secret-value")
    return MediaGateway(GatewaySettings(), client=BackendClient.__new__(BackendClient))


class _Sink:
    call_id = "protocol"

    def __init__(self):
        self.frames = []

    async def send_frame(self, frame):
        self.frames.append(frame)

    async def receive_frame(self):
        return None

    async def close(self):
        return None


def test_the_gateway_answers_a_hello():
    gateway = _gateway()
    socket = _GatewaySocket([protocol_hello_message()])

    asyncio.run(gateway._bank_to_caller(_Sink(), socket))

    assert socket.replies == [gateway_protocol.protocol_ready_message()]


def test_the_gateway_ignores_a_malformed_negotiation_frame():
    gateway = _gateway()
    socket = _GatewaySocket(['{"type": "protocol_hello", "version": "x"}'])

    asyncio.run(gateway._bank_to_caller(_Sink(), socket))

    assert socket.replies == []


def test_answering_a_hello_does_not_disturb_the_media():
    """Negotiation and audio share a socket; only audio may reach the caller."""
    from gateway.sources import FRAME_BYTES

    gateway = _gateway()
    sink = _Sink()
    socket = _GatewaySocket(
        [protocol_hello_message(), b"\xaa" * FRAME_BYTES, protocol_hello_message()]
    )

    asyncio.run(gateway._bank_to_caller(sink, socket))

    assert len(sink.frames) == 1
    assert sink.frames[0] == b"\xaa" * FRAME_BYTES
    assert len(socket.replies) == 2


def test_a_playback_boundary_still_works_alongside_negotiation():
    """Phase 6.7 unaffected: the boundary is answered as before."""
    import json

    from gateway.sources import FRAME_BYTES

    gateway = _gateway()
    sink = _Sink()
    boundary = json.dumps({"type": "playback_boundary", "id": "1"})
    socket = _GatewaySocket([protocol_hello_message(), b"\xbb" * FRAME_BYTES, boundary])

    asyncio.run(gateway._bank_to_caller(sink, socket))

    assert len(sink.frames) == 1
    assert socket.replies == [
        gateway_protocol.protocol_ready_message(),
        gateway_protocol.playback_drained_message("1"),
    ]


# === a refused call leaves nothing behind ===================================


def test_a_gateway_that_never_answers_releases_every_resource(monkeypatch):
    """The whole point: a mismatch costs a call, never the service.

    A gateway too old to negotiate attaches and then says nothing. The call
    must be given up — not left holding the only capacity slot while waiting
    for a turn that can never complete.
    """
    from app.config import settings
    from app.telephony import service
    import tests.test_telephony_webhook as webhook

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_media_transport", "websocket")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    monkeypatch.setattr(settings, "telephony_media_connect_timeout", 2)
    monkeypatch.setattr(settings, "telephony_protocol_timeout", 1)
    monkeypatch.setattr(
        service, "open_phone_realtime_session", webhook._stub_connector
    )

    async def scenario():
        from datetime import datetime, timezone

        from app.telephony.schemas import InboundCallEvent

        result = await service.handle_event(
            InboundCallEvent(
                provider="TEST",
                provider_event_id="evt-silent-gw",
                provider_call_id="silent-gw",
                event_type="incoming",
                event_timestamp=datetime.now(timezone.utc),
            )
        )
        bridge = phone_call_registry.get("silent-gw")
        assert bridge is not None, "the call was never registered"

        # A gateway attaches, and then never says what it is.
        bridge.transport.attach(FakeSocket())

        for _ in range(80):
            await asyncio.sleep(0.05)
            if phone_call_registry.get("silent-gw") is None:
                break

        return result, bridge

    result, bridge = run(scenario())

    assert result.outcome.value == "accepted"
    assert phone_call_registry.get("silent-gw") is None, "the bridge outlived the call"
    assert voice_call_manager.used_capacity() == 0, "a capacity slot was held"
    assert bridge.closed is True
    assert session_manager.get_session(bridge.banking_session_id) is None

    with session_scope() as db:
        row = db.scalars(
            select(AgentSession).where(AgentSession.provider_call_id == "silent-gw")
        ).one()
    assert row.disconnect_reason == "PROTOCOL_MISMATCH", row.disconnect_reason


def test_an_incompatible_gateway_is_refused_with_a_precise_reason(monkeypatch):
    """Right version, no playback_ack. Refused before the caller is greeted."""
    from app.config import settings
    from app.telephony import service
    import tests.test_telephony_webhook as webhook

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_media_transport", "websocket")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    monkeypatch.setattr(settings, "telephony_media_connect_timeout", 2)
    monkeypatch.setattr(settings, "telephony_protocol_timeout", 2)
    monkeypatch.setattr(
        service, "open_phone_realtime_session", webhook._stub_connector
    )

    async def scenario():
        from datetime import datetime, timezone

        from app.telephony.schemas import InboundCallEvent

        await service.handle_event(
            InboundCallEvent(
                provider="TEST",
                provider_event_id="evt-old-gw",
                provider_call_id="old-gw",
                event_type="incoming",
                event_timestamp=datetime.now(timezone.utc),
            )
        )
        bridge = phone_call_registry.get("old-gw")
        bridge.transport.attach(FakeSocket())
        bridge.transport.on_protocol_ready(PROTOCOL_VERSION, ("audio_only",))

        for _ in range(80):
            await asyncio.sleep(0.05)
            if phone_call_registry.get("old-gw") is None:
                break
        return bridge

    bridge = run(scenario())

    assert phone_call_registry.get("old-gw") is None
    assert voice_call_manager.used_capacity() == 0
    assert bridge.greeted is False, "a caller was greeted into a call that cannot end"
