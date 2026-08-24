"""The UDP media leg: what FreeSWITCH forks a call's audio into.

FreeSWITCH terminates SIP and RTP from the telephone network and forks the
call's audio to a UDP socket. This suite drives that socket directly, speaking
real RTP over real loopback UDP, which is exactly what the media server sends —
so everything from the socket inwards is proven without a media server present.

What that leaves untested is FreeSWITCH itself: the SIP handshake, SDP
negotiation and the RTP it receives from a carrier. What it tests is every
decision this side of the fork, which is the part that has to be right before a
real minute is worth spending.

All customers and PINs referenced are synthetic seed data.
"""

import asyncio
import socket
import struct
import threading
import time

import pytest
import uvicorn
from sqlalchemy import delete, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import service as telephony_service
from app.telephony.bridge import phone_call_registry
from gateway import rtp
from gateway.config import GatewaySettings
from gateway.service import CallOutcome, MediaGateway
from gateway.sources import FRAME_BYTES
from gateway.udp_source import (
    MediaPortAllocator,
    MediaPortsExhausted,
    UdpMediaSource,
)

APPLICATION_TARGET = 5
REALTIME_LIVE_CEILING = 3
TEST_SECRET = "phase4b-udp-media-secret-not-a-real-credential"

# A small range, so exhaustion is reachable in a test.
PORT_LOW = 17600
PORT_HIGH = 17631


# === fakes and fixtures =====================================================


class MockRealtimeSession:
    """Stands in for the paid Realtime session. Opens no socket, costs nothing."""

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


def audio_event(pcm: bytes) -> FakeEvent:
    return FakeEvent("audio", audio=FakeEvent("audio", data=pcm))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def backend():
    """A real banking backend, telephony enabled, on a loopback port."""
    from app.main import create_app

    previous = (
        settings.telephony_enabled,
        settings.telephony_webhook_secret,
        settings.telephony_media_transport,
        settings.realtime_max_active_sessions,
    )
    settings.telephony_enabled = True
    settings.telephony_webhook_secret = TEST_SECRET
    settings.telephony_media_transport = "websocket"
    settings.realtime_max_active_sessions = APPLICATION_TARGET

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "backend did not start"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)
    (
        settings.telephony_enabled,
        settings.telephony_webhook_secret,
        settings.telephony_media_transport,
        settings.realtime_max_active_sessions,
    ) = previous


@pytest.fixture
def sessions(monkeypatch):
    made: list[MockRealtimeSession] = []

    async def connect(_context):
        session = MockRealtimeSession()
        made.append(session)
        return session

    monkeypatch.setattr(telephony_service, "open_phone_realtime_session", connect)
    return made


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
def loop():
    made = asyncio.new_event_loop()
    asyncio.set_event_loop(made)
    yield made
    made.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def run(loop):
    def _run(coro):
        return loop.run_until_complete(coro)

    return _run


@pytest.fixture
def gateway(backend, loop):
    import os

    previous = dict(os.environ)
    os.environ["GATEWAY_BACKEND_URL"] = backend
    os.environ["GATEWAY_WEBHOOK_SECRET"] = TEST_SECRET
    os.environ["GATEWAY_VERIFY_TLS"] = "false"

    async def build():
        return MediaGateway(GatewaySettings())

    built = loop.run_until_complete(build())
    yield built
    loop.run_until_complete(built.aclose())
    os.environ.clear()
    os.environ.update(previous)


@pytest.fixture
def allocator():
    return MediaPortAllocator(port_low=PORT_LOW, port_high=PORT_HIGH)


# === a stand-in for FreeSWITCH's forked media socket ========================


class MediaServerPeer:
    """Sends and receives on one call's media port, as FreeSWITCH would."""

    def __init__(self, port: int, *, use_rtp: bool = True) -> None:
        self.port = port
        self.use_rtp = use_rtp
        self.sender = rtp.RtpSender()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.socket.bind(("127.0.0.1", 0))
        self.heard: list[bytes] = []

    def speak(self, payload: bytes) -> None:
        datagram = self.sender.build(payload) if self.use_rtp else payload
        self.socket.sendto(datagram, ("127.0.0.1", self.port))

    def drain(self) -> list[bytes]:
        """Everything the bank has played back to this caller so far."""
        while True:
            try:
                datagram, _ = self.socket.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            self.heard.append(rtp.payload_of(datagram))
        return self.heard

    def close(self) -> None:
        self.socket.close()


def speech(seed: int) -> bytes:
    """A distinct µ-law frame per caller, so audio can be told apart."""
    return bytes([(seed * 31 + n) % 256 for n in range(FRAME_BYTES)])


async def wait_until(predicate, *, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def phone_rows():
    with session_scope() as db:
        return list(
            db.scalars(
                select(AgentSession)
                .where(AgentSession.channel == "PHONE")
                .order_by(AgentSession.id)
            )
        )


class LoopbackCall:
    """One synthetic SIP call: a media port, a peer, and the gateway relaying."""

    def __init__(self, gateway, allocator, call_id: str, *, use_rtp: bool = True):
        self.call_id = call_id
        self._gateway = gateway
        self._allocator = allocator
        self._use_rtp = use_rtp
        self.source: UdpMediaSource | None = None
        self.peer: MediaServerPeer | None = None
        self.task: asyncio.Task | None = None
        self.outcome: str | None = None

    async def start(self, *, caller: str | None = None) -> "LoopbackCall":
        self.source = UdpMediaSource(self.call_id, caller=caller)
        port = await self._allocator.bind(self.source)
        self.peer = MediaServerPeer(port, use_rtp=self._use_rtp)
        self.task = asyncio.create_task(
            self._gateway.handle_call(self.source), name=f"call-{self.call_id}"
        )
        # The media server sends immediately; that first datagram fixes the peer.
        await asyncio.sleep(0.05)
        self.peer.speak(speech(0))
        return self

    def speak(self, payload: bytes) -> None:
        self.peer.speak(payload)

    def heard(self) -> list[bytes]:
        return self.peer.drain()

    async def hang_up(self) -> str:
        await self.source.close()
        self.outcome = await asyncio.wait_for(self.task, timeout=15)
        self.peer.close()
        return self.outcome


# === 1. framing =============================================================


def test_a_bare_ulaw_frame_is_not_mistaken_for_rtp():
    """A false positive would strip 12 bytes of audio and click."""
    for payload in (b"\xff" * FRAME_BYTES, speech(1), bytes(FRAME_BYTES)):
        assert rtp.looks_like_rtp(payload) is False
        assert rtp.payload_of(payload) == payload


def test_an_rtp_packet_is_recognised_and_unwrapped():
    sender = rtp.RtpSender()
    payload = speech(2)

    datagram = sender.build(payload)

    assert len(datagram) == rtp.RTP_HEADER_BYTES + FRAME_BYTES
    assert rtp.looks_like_rtp(datagram) is True
    assert rtp.payload_of(datagram) == payload


def test_rtp_sequence_and_timestamp_advance_per_frame():
    sender = rtp.RtpSender()

    first = rtp.parse(sender.build(speech(1)))
    second = rtp.parse(sender.build(speech(1)))

    assert second.sequence == (first.sequence + 1) & 0xFFFF
    assert second.timestamp == (first.timestamp + rtp.SAMPLES_PER_FRAME) & 0xFFFFFFFF
    assert first.ssrc == second.ssrc


def test_two_calls_get_different_rtp_streams():
    """Sharing a sender would interleave two calls into one unseparable stream."""
    streams = {rtp.RtpSender().ssrc for _ in range(20)}

    assert len(streams) == 20


def test_a_packet_with_contributors_or_extensions_is_not_treated_as_rtp():
    """Conservative detection: a forked call leg has neither."""
    with_csrc = struct.pack("!BBHII", (2 << 6) | 1, 0, 1, 1, 1) + speech(1)
    with_extension = struct.pack("!BBHII", (2 << 6) | 0x10, 0, 1, 1, 1) + speech(1)

    assert rtp.looks_like_rtp(with_csrc) is False
    assert rtp.looks_like_rtp(with_extension) is False


def test_an_unsupported_payload_type_is_not_treated_as_rtp():
    opus_ish = struct.pack("!BBHII", 2 << 6, 111, 1, 1, 1) + speech(1)

    assert rtp.looks_like_rtp(opus_ish) is False


# === 2. the media port ======================================================


def test_a_source_binds_one_port_from_the_range(run, allocator):
    source = UdpMediaSource("call-bind")

    port = run(allocator.bind(source))

    assert PORT_LOW <= port <= PORT_HIGH
    assert source.port == port
    run(source.close())


def test_two_calls_never_share_a_port(run, allocator):
    async def scenario():
        sources = [UdpMediaSource(f"call-{n}") for n in range(6)]
        ports = [await allocator.bind(source) for source in sources]
        for source in sources:
            await source.close()
        return ports

    ports = run(scenario())

    assert len(set(ports)) == 6


def test_an_exhausted_range_refuses_rather_than_overflowing(run):
    """A bounded range is a firewall rule; running past it is not an option."""
    tiny = MediaPortAllocator(port_low=17700, port_high=17701)

    async def scenario():
        held = [UdpMediaSource(f"call-{n}") for n in range(2)]
        for source in held:
            await tiny.bind(source)
        try:
            with pytest.raises(MediaPortsExhausted):
                await tiny.bind(UdpMediaSource("call-overflow"))
        finally:
            for source in held:
                await source.close()

    run(scenario())


def test_a_closed_port_is_available_again(run):
    small = MediaPortAllocator(port_low=17710, port_high=17710)

    async def scenario():
        first = UdpMediaSource("call-first")
        port = await small.bind(first)
        await first.close()
        await asyncio.sleep(0.05)
        second = UdpMediaSource("call-second")
        again = await small.bind(second)
        await second.close()
        return port, again

    port, again = run(scenario())

    assert port == again == 17710


def test_a_stranger_cannot_redirect_a_live_call(run, allocator):
    """One datagram from anyone else must not repoint the audio."""

    async def scenario():
        source = UdpMediaSource("call-hijack")
        port = await allocator.bind(source)
        real = MediaServerPeer(port)
        real.speak(speech(1))
        await asyncio.sleep(0.1)

        stranger = MediaServerPeer(port)
        stranger.speak(speech(2))
        await asyncio.sleep(0.1)

        peer_after = source.peer
        dropped = source.dropped
        # The bank plays audio back; it must reach the real peer only.
        await source.send_frame(speech(9))
        await asyncio.sleep(0.1)
        heard_real = len(real.drain())
        heard_stranger = len(stranger.drain())

        real.close()
        stranger.close()
        await source.close()
        return peer_after, dropped, heard_real, heard_stranger

    peer, dropped, heard_real, heard_stranger = run(scenario())

    assert peer == ("127.0.0.1", peer[1])
    assert dropped >= 1, "the stranger's datagram was accepted"
    assert heard_real == 1
    assert heard_stranger == 0, "audio was redirected to a stranger"


def test_a_wrongly_sized_frame_never_reaches_the_codec(run, allocator):
    async def scenario():
        source = UdpMediaSource("call-badframe")
        port = await allocator.bind(source)
        peer = MediaServerPeer(port, use_rtp=False)
        peer.speak(b"\xff" * 37)  # not a 20 ms frame
        peer.speak(b"")
        await asyncio.sleep(0.15)
        state = (source.frames_in, source.dropped)
        peer.close()
        await source.close()
        return state

    frames_in, dropped = run(scenario())

    assert frames_in == 0
    assert dropped >= 1


def test_the_inbound_queue_is_bounded(run, allocator):
    """A media server that outruns the relay must not grow this without limit."""

    async def scenario():
        source = UdpMediaSource("call-flood", max_queued_frames=20)
        port = await allocator.bind(source)
        peer = MediaServerPeer(port)
        for _ in range(400):
            peer.speak(speech(1))
        await asyncio.sleep(0.4)
        state = (source._inbound.qsize(), source.dropped)
        peer.close()
        await source.close()
        return state

    queued, dropped = run(scenario())

    assert queued <= 20
    assert dropped > 0


def test_replies_use_the_framing_the_peer_used(run, allocator):
    async def scenario():
        results = {}
        for label, use_rtp in (("rtp", True), ("bare", False)):
            source = UdpMediaSource(f"call-{label}")
            port = await allocator.bind(source)
            peer = MediaServerPeer(port, use_rtp=use_rtp)
            peer.speak(speech(1))
            await asyncio.sleep(0.1)
            await source.send_frame(speech(5))
            await asyncio.sleep(0.1)
            raw = None
            try:
                raw, _ = peer.socket.recvfrom(4096)
            except BlockingIOError:
                pass
            results[label] = raw
            peer.close()
            await source.close()
        return results

    results = run(scenario())

    assert results["rtp"] is not None
    assert rtp.looks_like_rtp(results["rtp"]) is True
    assert results["bare"] == speech(5)


def test_audio_is_dropped_rather_than_queued_before_a_peer_appears(run, allocator):
    """A greeting held until a peer appears arrives over whatever came next."""

    async def scenario():
        source = UdpMediaSource("call-early")
        await allocator.bind(source)
        await source.send_frame(speech(1))
        sent = source.frames_out
        await source.close()
        return sent

    assert run(scenario()) == 0


# === 3. one call, end to end ================================================


def test_one_loopback_call_reaches_the_bank(run, gateway, allocator, sessions):
    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-one").start()
        await wait_until(lambda: sessions and sessions[0].audio_chunks)
        call.speak(speech(4))
        await wait_until(lambda: len(sessions[0].audio_chunks) >= 2)
        return await call.hang_up()

    outcome = run(scenario())

    assert outcome == CallOutcome.COMPLETED
    rows = phone_rows()
    assert len(rows) == 1
    assert rows[0].provider_call_id == "sip-one"
    assert rows[0].channel == "PHONE"
    # Converted on the way: 160 bytes of µ-law becomes 960 bytes of PCM16@24k.
    assert len(sessions[0].audio_chunks[0]) == FRAME_BYTES * 2 * 3


def test_ai_audio_returns_to_the_caller_as_telephone_frames(
    run, gateway, allocator, sessions
):
    import array

    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-back").start()
        await wait_until(lambda: sessions and sessions[0].audio_chunks)
        sessions[0].emit(audio_event(array.array("h", [1400] * 480).tobytes()))
        await wait_until(lambda: call.heard())
        heard = list(call.heard())
        await call.hang_up()
        return heard

    heard = run(scenario())

    assert len(heard) >= 1
    assert all(len(frame) == FRAME_BYTES for frame in heard)


def test_the_greeting_is_delivered_once_on_a_loopback_call(
    run, gateway, allocator, sessions
):
    from app.telephony.bridge import GREETING_CUE

    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-greet").start()
        await wait_until(lambda: sessions and sessions[0].messages)
        await asyncio.sleep(0.2)
        messages = list(sessions[0].messages)
        await call.hang_up()
        return messages

    messages = run(scenario())

    assert messages == [GREETING_CUE]


def test_a_loopback_call_authenticates_nobody(run, gateway, allocator, sessions):
    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-anon").start(
            caller="+6591234567"
        )
        await wait_until(lambda: phone_rows())
        row = phone_rows()[0]
        state = (row.customer_id, row.authenticated, row.auth_status)
        await call.hang_up()
        return state

    customer_id, authenticated, auth_status = run(scenario())

    assert customer_id is None
    assert authenticated is False
    assert auth_status == "PENDING"


@pytest.mark.parametrize(
    "spoofed",
    ["DEMO001", "+6531252836", "127.0.0.1", "sip:DEMO001@bank.example"],
)
def test_no_sip_identifier_authenticates_a_customer(
    run, gateway, allocator, sessions, spoofed
):
    """Caller ID, ANI, From, Contact, DID, IP — none of them are credentials."""

    async def scenario():
        call = LoopbackCall(gateway, allocator, f"sip-{abs(hash(spoofed)) % 9999}")
        await call.start(caller=spoofed)
        await wait_until(lambda: phone_rows())
        row = phone_rows()[0]
        state = (row.customer_id, row.authenticated)
        await call.hang_up()
        return state

    customer_id, authenticated = run(scenario())

    assert customer_id is None
    assert authenticated is False


def test_the_ordinary_pin_flow_still_governs_a_loopback_call(
    run, gateway, allocator, sessions
):
    from app.auth.authentication import submit_customer_id, submit_pin

    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-auth").start()
        await wait_until(lambda: phone_rows())
        banking_session_id = phone_rows()[0].banking_session_id
        submit_customer_id(banking_session_id, "DEMO001")
        result = submit_pin(banking_session_id, "4821")
        await call.hang_up()
        return result

    result = run(scenario())

    assert result["authenticated"] is True
    assert result["customer_id"] == "DEMO001"


# === 4-5. two and three concurrent loopback calls ===========================


def test_two_concurrent_loopback_calls_stay_separate(
    run, gateway, allocator, sessions
):
    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-two-{n}").start()
            for n in range(2)
        ]
        await wait_until(
            lambda: len(sessions) == 2 and all(s.audio_chunks for s in sessions)
        )
        for n, call in enumerate(calls):
            call.speak(speech(n + 20))
        await wait_until(lambda: all(len(s.audio_chunks) >= 2 for s in sessions))
        ports = {call.source.port for call in calls}
        for call in calls:
            await call.hang_up()
        return ports

    ports = run(scenario())

    assert len(ports) == 2
    assert len(phone_rows()) == 2
    assert len(sessions) == 2
    assert len({row.banking_session_id for row in phone_rows()}) == 2
    # The second frame each session received is its own caller's, not a peer's.
    assert sessions[0].audio_chunks[1] != sessions[1].audio_chunks[1]


def test_three_concurrent_loopback_calls_stay_separate(
    run, gateway, allocator, sessions
):
    """Three, matching the current Realtime live ceiling."""
    from app.telephony import audio as codec

    spoken = {n: speech(n + 30) for n in range(REALTIME_LIVE_CEILING)}

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-three-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(
            lambda: len(sessions) == REALTIME_LIVE_CEILING
            and all(s.audio_chunks for s in sessions)
        )
        for n, call in enumerate(calls):
            call.speak(spoken[n])
        await wait_until(lambda: all(len(s.audio_chunks) >= 2 for s in sessions))
        delivered = [list(s.audio_chunks) for s in sessions]
        for call in calls:
            await call.hang_up()
        return delivered

    delivered = run(scenario())

    assert len(phone_rows()) == REALTIME_LIVE_CEILING
    # Each session's second frame is exactly its own caller's, converted.
    for n in range(REALTIME_LIVE_CEILING):
        assert delivered[n][1] == codec.telephony_to_model(spoken[n])
    # And no two callers' audio was identical.
    seconds = {bytes(frames[1]) for frames in delivered}
    assert len(seconds) == REALTIME_LIVE_CEILING


def test_ai_audio_reaches_only_its_own_caller_across_three_calls(
    run, gateway, allocator, sessions
):
    import array

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-out-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(
            lambda: len(sessions) == REALTIME_LIVE_CEILING
            and all(s.audio_chunks for s in sessions)
        )
        # Only the middle caller is answered.
        sessions[1].emit(audio_event(array.array("h", [800] * 480).tobytes()))
        await wait_until(lambda: calls[1].heard())
        await asyncio.sleep(0.2)
        heard = [len(call.heard()) for call in calls]
        for call in calls:
            await call.hang_up()
        return heard

    heard = run(scenario())

    assert heard[1] >= 1, "the answered caller heard nothing"
    assert heard[0] == 0, "caller 0 heard another caller's audio"
    assert heard[2] == 0, "caller 2 heard another caller's audio"


def test_greeting_does_not_cross_between_concurrent_loopback_calls(
    run, gateway, allocator, sessions
):
    from app.telephony.bridge import GREETING_CUE

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-greet-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(
            lambda: len(sessions) == REALTIME_LIVE_CEILING
            and all(s.messages for s in sessions)
        )
        await asyncio.sleep(0.2)
        messages = [list(s.messages) for s in sessions]
        for call in calls:
            await call.hang_up()
        return messages

    messages = run(scenario())

    assert messages == [[GREETING_CUE]] * REALTIME_LIVE_CEILING


def test_authentication_does_not_cross_between_loopback_calls(
    run, gateway, allocator, sessions
):
    from app.auth.authentication import submit_customer_id, submit_pin

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-iso-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(lambda: len(phone_rows()) == REALTIME_LIVE_CEILING)
        rows = {row.provider_call_id: row.banking_session_id for row in phone_rows()}

        submit_customer_id(rows["sip-iso-0"], "DEMO001")
        first = submit_pin(rows["sip-iso-0"], "4821")
        others = [
            session_manager.get_session(rows[f"sip-iso-{n}"])
            for n in (1, 2)
        ]
        state = (first, [(s.customer_id, s.authenticated) for s in others])
        for call in calls:
            await call.hang_up()
        return state

    first, others = run(scenario())

    assert first["authenticated"] is True
    assert others == [(None, False), (None, False)]


def test_banking_answers_do_not_cross_loopback_calls(
    run, gateway, allocator, sessions
):
    from app.auth.authentication import submit_customer_id, submit_pin
    from app.tools import accounts

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-bank-{n}").start()
            for n in range(2)
        ]
        await wait_until(lambda: len(phone_rows()) == 2)
        rows = {row.provider_call_id: row.banking_session_id for row in phone_rows()}

        submit_customer_id(rows["sip-bank-0"], "DEMO001")
        submit_pin(rows["sip-bank-0"], "4821")
        submit_customer_id(rows["sip-bank-1"], "DEMO002")
        submit_pin(rows["sip-bank-1"], "7315")
        answers = (
            accounts.get_account_balance(rows["sip-bank-0"], "Savings"),
            accounts.get_account_balance(rows["sip-bank-1"], "Savings"),
        )
        for call in calls:
            await call.hang_up()
        return answers

    first, second = run(scenario())

    assert first["success"] and second["success"]
    assert first["available_balance"] != second["available_balance"]


def test_no_transcript_is_written_by_the_udp_leg(run, gateway, allocator, sessions):
    import inspect

    from gateway import rtp as rtp_module
    from gateway import udp_source

    for module in (rtp_module, udp_source):
        source = inspect.getsource(module)
        assert "ConversationMessage" not in source, module.__name__
        assert "record_message" not in source, module.__name__

    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-notrans").start()
        await wait_until(lambda: phone_rows())
        await call.hang_up()

    run(scenario())

    with session_scope() as db:
        assert db.scalars(select(ConversationMessage)).all() == []


# === 6. failure and cleanup =================================================


def test_a_caller_hangup_releases_everything(run, gateway, allocator, sessions):
    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-hangup").start()
        await wait_until(lambda: sessions and sessions[0].audio_chunks)
        port = call.source.port
        outcome = await call.hang_up()
        await asyncio.sleep(0.2)
        return outcome, port

    outcome, port = run(scenario())

    assert outcome == CallOutcome.COMPLETED
    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert len(session_manager.list_active_sessions()) == 0
    assert sessions[0].closed is True
    row = phone_rows()[0]
    assert row.status == "COMPLETED"
    assert row.ended_at is not None
    # And the media port is free again.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", port))
    probe.close()


def test_ending_one_loopback_call_leaves_the_others_running(
    run, gateway, allocator, sessions
):
    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-keep-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(lambda: len(phone_rows()) == REALTIME_LIVE_CEILING)
        await calls[0].hang_up()
        await asyncio.sleep(0.2)
        during = voice_call_manager.used_capacity()
        # The survivors still carry audio.
        for call in calls[1:]:
            call.speak(speech(77))
        await wait_until(
            lambda: all(len(s.audio_chunks) >= 2 for s in sessions[1:])
        )
        for call in calls[1:]:
            await call.hang_up()
        return during

    during = run(scenario())

    assert during == REALTIME_LIVE_CEILING - 1
    assert voice_call_manager.used_capacity() == 0


def test_duplicate_cleanup_is_harmless(run, gateway, allocator, sessions):
    async def scenario():
        call = await LoopbackCall(gateway, allocator, "sip-twice").start()
        await wait_until(lambda: phone_rows())
        await call.source.close()
        await call.source.close()
        outcome = await asyncio.wait_for(call.task, timeout=15)
        call.peer.close()
        await asyncio.sleep(0.2)
        return outcome

    outcome = run(scenario())

    assert outcome == CallOutcome.COMPLETED
    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0


def test_a_media_server_that_never_sends_still_cleans_up(
    run, gateway, allocator, sessions
):
    """Announced, port bound, and no audio ever arrives."""

    async def scenario():
        source = UdpMediaSource("sip-silent")
        await allocator.bind(source)
        task = asyncio.create_task(gateway.handle_call(source))
        await wait_until(lambda: phone_rows())
        await source.close()
        outcome = await asyncio.wait_for(task, timeout=15)
        await asyncio.sleep(0.2)
        return outcome

    outcome = run(scenario())

    assert outcome == CallOutcome.COMPLETED
    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0


def test_a_failed_model_session_releases_the_loopback_call(
    run, gateway, allocator, sessions, monkeypatch
):
    async def explode(_context):
        raise RuntimeError("provider refused")

    monkeypatch.setattr(telephony_service, "open_phone_realtime_session", explode)

    async def scenario():
        source = UdpMediaSource("sip-doomed")
        await allocator.bind(source)
        outcome = await gateway.handle_call(source)
        await asyncio.sleep(0.2)
        return outcome

    outcome = run(scenario())

    assert outcome == CallOutcome.REFUSED
    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0


def test_no_relay_or_socket_outlives_its_call(run, gateway, allocator, sessions):
    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-task-{n}").start()
            for n in range(REALTIME_LIVE_CEILING)
        ]
        await wait_until(lambda: len(phone_rows()) == REALTIME_LIVE_CEILING)

        def relays():
            return [
                task
                for task in asyncio.all_tasks()
                if (task.get_name() or "").startswith("gw-") and not task.done()
            ]

        # Two relay tasks per call, one each way — waited for rather than
        # sampled. Creating a task is not the same as the loop having run it,
        # and a fixed pause here made this test fail under load while proving
        # nothing about leaks. The waits are bounded, so a genuine leak still
        # fails; only the guessing is gone.
        await wait_until(lambda: len(relays()) == REALTIME_LIVE_CEILING * 2)
        during = len(relays())

        for call in calls:
            await call.hang_up()

        await wait_until(lambda: not relays())
        after = len(relays())
        transports = [call.source._transport for call in calls]
        return during, after, transports

    during, after, transports = run(scenario())

    assert during == REALTIME_LIVE_CEILING * 2
    assert after == 0
    assert all(transport is None for transport in transports)


# === 7. the sixth call, and the application ceiling =========================


def test_a_sixth_loopback_call_is_refused(run, gateway, allocator, sessions):
    """The application ceiling is 5 and is enforced through the SIP path too."""

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-cap-{n}").start()
            for n in range(APPLICATION_TARGET)
        ]
        await wait_until(lambda: len(phone_rows()) == APPLICATION_TARGET)
        during = voice_call_manager.used_capacity()

        overflow = UdpMediaSource("sip-cap-sixth")
        await allocator.bind(overflow)
        refused = await gateway.handle_call(overflow)

        for call in calls:
            await call.hang_up()
        return refused, during

    refused, during = run(scenario())

    assert refused == CallOutcome.REFUSED
    assert during == APPLICATION_TARGET
    assert voice_call_manager.used_capacity() == 0


def test_five_concurrent_loopback_calls_are_isolated(run, gateway, allocator, sessions):
    from app.telephony import audio as codec

    spoken = {n: speech(n + 50) for n in range(APPLICATION_TARGET)}

    async def scenario():
        calls = [
            await LoopbackCall(gateway, allocator, f"sip-five-{n}").start()
            for n in range(APPLICATION_TARGET)
        ]
        await wait_until(
            lambda: len(sessions) == APPLICATION_TARGET
            and all(s.audio_chunks for s in sessions)
        )
        for n, call in enumerate(calls):
            call.speak(spoken[n])
        await wait_until(lambda: all(len(s.audio_chunks) >= 2 for s in sessions))
        delivered = [list(s.audio_chunks) for s in sessions]
        ports = {call.source.port for call in calls}
        for call in calls:
            await call.hang_up()
        return delivered, ports

    delivered, ports = run(scenario())

    assert len(ports) == APPLICATION_TARGET
    for n in range(APPLICATION_TARGET):
        assert delivered[n][1] == codec.telephony_to_model(spoken[n])
    assert voice_call_manager.used_capacity() == 0
