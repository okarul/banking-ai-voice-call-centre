"""The reference media gateway, driven end to end against a real backend.

Not a mock of the boundary — a real uvicorn server on a loopback port, real
HTTP with real HMAC signatures, real WebSockets carrying real µ-law frames.
The only thing standing in for something is the OpenAI Realtime session, which
is mocked because opening one costs money and this suite must be free to run.

What is proven here that the backend's own tests cannot prove: that a second,
independent implementation of the signing scheme agrees with the bank's; that a
gateway which holds a per-call credential can attach and one which does not
cannot; and that five simultaneous calls through the whole stack keep their
audio to themselves.

All customers and PINs referenced are synthetic seed data.
"""

import asyncio
import socket
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
from gateway.client import BackendClient, CallRefused
from gateway.config import GatewaySettings
from gateway.service import CallOutcome, MediaGateway
from gateway.sources import FRAME_BYTES, SyntheticSource

APPLICATION_TARGET = 5
TEST_SECRET = "gateway-suite-shared-secret-not-a-real-credential"


# === a real backend on a real port ==========================================


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
        uvicorn.Config(
            create_app(), host="127.0.0.1", port=port, log_level="error"
        )
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
    """Mocked model sessions, one per call, in creation order."""
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
    """One event loop per test.

    The gateway holds an HTTP connection pool for its lifetime, exactly as a
    long-running gateway process does. `asyncio.run` per call would give it a
    new loop each time and leave the pool holding connections belonging to a
    loop that had closed — an artefact of the test, not of the gateway.
    """
    made = asyncio.new_event_loop()
    asyncio.set_event_loop(made)
    yield made
    made.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def run(loop):
    """Run a coroutine on this test's loop."""

    def _run(coro):
        return loop.run_until_complete(coro)

    return _run


@pytest.fixture
def gateway(backend, loop):
    """A gateway pointed at the running backend, built on this test's loop."""
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


# === helpers ================================================================


def speech(seed: int) -> bytes:
    """A distinct frame per caller, so audio can be told apart."""
    return bytes([(seed * 7 + n) % 256 for n in range(FRAME_BYTES)])


async def carry(gateway, source, *, frames=(), settle=0.4):
    """Run one call: attach, speak the given frames, hang up."""
    handling = asyncio.create_task(gateway.handle_call(source))
    await asyncio.sleep(settle)
    for frame in frames:
        source.speak(frame)
    if frames:
        await asyncio.sleep(0.3)
    source.hang_up()
    return await asyncio.wait_for(handling, timeout=15)


def phone_rows():
    with session_scope() as db:
        return list(
            db.scalars(
                select(AgentSession)
                .where(AgentSession.channel == "PHONE")
                .order_by(AgentSession.id)
            )
        )


# === A. the boundary and the signing contract ===============================


def test_the_gateway_never_imports_the_bank():
    """The boundary is structural, not a convention.

    In a real deployment this process runs on the host exposed to the telephone
    network. An import of `app.*` would mean the bank's database URL and its
    OpenAI key live in that address space, and would couple two things that are
    meant to be deployable apart.
    """
    import inspect
    import pkgutil

    import gateway

    for module in pkgutil.iter_modules(gateway.__path__):
        source = inspect.getsource(
            __import__(f"gateway.{module.name}", fromlist=["_"])
        )
        assert "import app" not in source, module.name
        assert "from app" not in source, module.name


def test_the_two_signing_implementations_agree():
    """Two implementations that must agree are a check on each other.

    A test that signed with the same function the backend verifies with could
    pass while both were wrong together.
    """
    from app.telephony.signature import sign_payload
    from gateway.signing import SIGNATURE_HEADER, sign

    for body in (b"{}", b'{"a":1}', b"x" * 5000, bytes(range(256))):
        headers = sign(TEST_SECRET, body, timestamp="1750000000")
        assert headers[SIGNATURE_HEADER] == sign_payload(
            TEST_SECRET, "1750000000", body
        )


def test_the_gateway_signs_the_bytes_it_sends():
    """Serialised once, then both signed and sent."""
    import inspect

    from gateway import client

    source = inspect.getsource(client.BackendClient._event_body)
    assert "json.dumps" in source
    announce = inspect.getsource(client.BackendClient.announce)
    # The same `body` object is signed and transmitted.
    assert "headers = sign(self._settings.webhook_secret, body)" in announce
    assert "content=body" in announce


def test_an_unconfigured_gateway_refuses_to_run():
    """Better a gateway that will not start than one sending unsigned events."""
    import os

    from gateway.client import BackendUnavailable

    previous = os.environ.pop("GATEWAY_WEBHOOK_SECRET", None)
    try:
        with pytest.raises(BackendUnavailable):
            BackendClient(GatewaySettings())
    finally:
        if previous is not None:
            os.environ["GATEWAY_WEBHOOK_SECRET"] = previous


def test_the_gateway_summary_carries_no_secret():
    import os

    os.environ["GATEWAY_WEBHOOK_SECRET"] = TEST_SECRET
    summary = GatewaySettings().safe_summary()

    assert TEST_SECRET not in str(summary)
    assert "webhook_secret" not in summary


def test_an_accepted_call_does_not_print_its_credential():
    """Logging the object is the natural thing to do; it must be safe."""
    from gateway.client import AcceptedCall

    accepted = AcceptedCall(
        provider_event_id="evt-1",
        media_url="/api/telephony/media/call-1",
        media_token="super-secret-token-value",
    )

    assert "super-secret-token-value" not in repr(accepted)
    assert "super-secret-token-value" not in str(accepted)


def test_a_wrong_secret_cannot_open_a_call(gateway, sessions, backend, run):
    """The gateway's credentials are checked, not assumed."""
    import os

    os.environ["GATEWAY_WEBHOOK_SECRET"] = "the-wrong-secret-entirely"
    impostor = MediaGateway(GatewaySettings())

    async def scenario():
        try:
            return await impostor.handle_call(SyntheticSource("call-impostor"))
        finally:
            await impostor.aclose()

    outcome = run(scenario())

    assert outcome == CallOutcome.UNAVAILABLE
    assert phone_rows() == []
    assert voice_call_manager.used_capacity() == 0
    os.environ["GATEWAY_WEBHOOK_SECRET"] = TEST_SECRET


def test_the_media_socket_refuses_a_gateway_without_a_credential(
    gateway, sessions, backend, run
):
    """The Phase 4 control, from the outside: an id is not authorisation."""
    import websockets

    from gateway.client import AcceptedCall

    async def scenario():
        accepted = await gateway._client.announce("call-nocred")
        url = gateway._settings.websocket_base + accepted.media_url
        # Exactly what an attacker who learned the call id could try.
        try:
            async with websockets.connect(url, open_timeout=5):
                return "attached"
        except Exception as error:
            return type(error).__name__
        finally:
            await gateway._client.report_ended("call-nocred")

    result = run(scenario())

    assert result != "attached", "a socket attached with no credential"


# === B. one synthetic call, end to end ======================================


def test_one_call_is_carried_end_to_end(gateway, sessions, run):
    outcome = run(carry(gateway, SyntheticSource("call-one"), frames=[speech(1)]))

    assert outcome == CallOutcome.COMPLETED
    assert gateway.completed == 1

    rows = phone_rows()
    assert len(rows) == 1
    assert rows[0].provider_call_id == "call-one"
    assert rows[0].channel == "PHONE"
    # The caller's frame reached the model, converted on the way.
    assert len(sessions) == 1
    assert len(sessions[0].audio_chunks) == 1
    assert len(sessions[0].audio_chunks[0]) == FRAME_BYTES * 2 * 3


def test_the_caller_is_greeted_through_the_gateway(gateway, sessions, run):
    run(carry(gateway, SyntheticSource("call-greeted")))

    from app.telephony.bridge import GREETING_CUE

    assert sessions[0].messages == [GREETING_CUE]


def test_assistant_audio_reaches_the_caller_as_telephone_frames(gateway, sessions, run):
    import array

    source = SyntheticSource("call-audio-back")

    async def scenario():
        handling = asyncio.create_task(gateway.handle_call(source))
        await asyncio.sleep(0.5)
        pcm = array.array("h", [1400] * 480).tobytes()
        sessions[0].emit(audio_event(pcm))
        await asyncio.sleep(0.4)
        source.hang_up()
        return await asyncio.wait_for(handling, timeout=15)

    run(scenario())

    assert len(source.played) >= 1
    assert all(len(frame) == FRAME_BYTES for frame in source.played)


def test_a_call_is_cleaned_up_after_the_caller_hangs_up(gateway, sessions, run):
    run(carry(gateway, SyntheticSource("call-bye")))

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert sessions[0].closed is True
    row = phone_rows()[0]
    assert row.status == "COMPLETED"
    assert row.ended_at is not None


def test_no_call_authenticates_a_customer(gateway, sessions, run):
    run(carry(gateway, SyntheticSource("call-anon", caller="DEMO001")))

    row = phone_rows()[0]
    assert row.customer_id is None
    assert row.authenticated is False
    assert row.auth_status == "PENDING"


def test_the_caller_number_is_not_stored(gateway, sessions, run):
    run(carry(gateway, SyntheticSource("call-cli", caller="+6591234567")))

    row = phone_rows()[0]
    stored = " ".join(
        str(getattr(row, column.name)) for column in AgentSession.__table__.columns
    )
    assert "6591234567" not in stored


# === C. two concurrent calls ================================================


def test_two_concurrent_calls_stay_separate(gateway, sessions, run):
    first = SyntheticSource("call-a")
    second = SyntheticSource("call-b")

    async def scenario():
        return await asyncio.gather(
            carry(gateway, first, frames=[speech(1)]),
            carry(gateway, second, frames=[speech(2)]),
        )

    outcomes = run(scenario())

    assert outcomes == [CallOutcome.COMPLETED, CallOutcome.COMPLETED]
    assert len(phone_rows()) == 2
    assert len(sessions) == 2
    # Two sessions, one frame each, and not the same frame.
    assert [len(s.audio_chunks) for s in sessions] == [1, 1]
    assert sessions[0].audio_chunks != sessions[1].audio_chunks


# === D. five concurrent calls ===============================================


def test_five_concurrent_calls_are_all_carried(gateway, sessions, run):
    sources = [SyntheticSource(f"call-{n}") for n in range(APPLICATION_TARGET)]

    async def scenario():
        return await asyncio.gather(
            *(carry(gateway, source, frames=[speech(n)]) for n, source in enumerate(sources))
        )

    outcomes = run(scenario())

    assert outcomes == [CallOutcome.COMPLETED] * APPLICATION_TARGET
    assert len(phone_rows()) == APPLICATION_TARGET
    assert len(sessions) == APPLICATION_TARGET
    assert len({row.banking_session_id for row in phone_rows()}) == APPLICATION_TARGET


def test_five_concurrent_calls_keep_their_audio_to_themselves(gateway, sessions, run):
    """F: no crossover, through the whole stack."""
    from app.telephony import audio as codec

    sources = [SyntheticSource(f"iso-{n}") for n in range(APPLICATION_TARGET)]
    frames = {n: speech(n + 3) for n in range(APPLICATION_TARGET)}

    async def scenario():
        return await asyncio.gather(
            *(
                carry(gateway, source, frames=[frames[n]])
                for n, source in enumerate(sources)
            )
        )

    run(scenario())

    # Each model session received exactly one frame, and it was its own caller's.
    for n in range(APPLICATION_TARGET):
        session = sessions[n]
        assert len(session.audio_chunks) == 1, f"call {n} got {len(session.audio_chunks)}"
    delivered = {bytes(s.audio_chunks[0]) for s in sessions}
    assert len(delivered) == APPLICATION_TARGET, "two callers' audio was identical"
    for n in range(APPLICATION_TARGET):
        assert sessions[n].audio_chunks[0] == codec.telephony_to_model(frames[n])


def test_assistant_audio_returns_only_to_its_own_caller(gateway, sessions, run):
    """F: outbound isolation, through the whole stack."""
    import array

    sources = [SyntheticSource(f"out-{n}") for n in range(APPLICATION_TARGET)]

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)
        # Only one caller is answered.
        sessions[2].emit(audio_event(array.array("h", [900] * 480).tobytes()))
        await asyncio.sleep(0.5)
        for source in sources:
            source.hang_up()
        return await asyncio.gather(*handling)

    run(scenario())

    assert len(sources[2].played) >= 1, "the answered caller heard nothing"
    for n in (0, 1, 3, 4):
        assert sources[n].played == [], f"caller {n} heard another caller's audio"


def test_authentication_on_one_call_does_not_reach_another(gateway, sessions, run):
    """F: no authentication crossover."""
    from app.auth.authentication import submit_customer_id, submit_pin

    sources = [SyntheticSource(f"auth-{n}") for n in range(APPLICATION_TARGET)]

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)

        rows = {row.provider_call_id: row.banking_session_id for row in phone_rows()}
        verified = rows["auth-0"]
        submit_customer_id(verified, "DEMO001")
        result = submit_pin(verified, "4821")

        others = [
            session_manager.get_session(rows[f"auth-{n}"]) for n in range(1, 5)
        ]
        snapshot = (result, [(s.customer_id, s.authenticated) for s in others])

        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)
        return snapshot

    result, others = run(scenario())

    assert result["authenticated"] is True
    assert result["customer_id"] == "DEMO001"
    assert others == [(None, False)] * 4


def test_banking_answers_do_not_cross_calls(gateway, sessions, run):
    """F: no banking-tool crossover."""
    from app.auth.authentication import submit_customer_id, submit_pin
    from app.tools import accounts

    sources = [SyntheticSource(f"bank-{n}") for n in range(2)]

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.6)
        rows = {row.provider_call_id: row.banking_session_id for row in phone_rows()}

        submit_customer_id(rows["bank-0"], "DEMO001")
        submit_pin(rows["bank-0"], "4821")
        submit_customer_id(rows["bank-1"], "DEMO002")
        submit_pin(rows["bank-1"], "7315")

        answers = (
            accounts.get_account_balance(rows["bank-0"], "Savings"),
            accounts.get_account_balance(rows["bank-1"], "Savings"),
        )
        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)
        return answers

    first, second = run(scenario())

    assert first["success"] and second["success"]
    assert first["available_balance"] != second["available_balance"]


def test_no_transcript_crosses_calls(gateway, sessions):
    """F: there is one transcript path, and the gateway adds none."""
    import inspect

    import gateway as gateway_package

    for module in ("client", "service", "sources", "signing", "config"):
        source = inspect.getsource(
            __import__(f"gateway.{module}", fromlist=["_"])
        )
        assert "ConversationMessage" not in source, module
        assert "record_message" not in source, module

    with session_scope() as db:
        assert db.scalars(select(ConversationMessage)).all() == []
    assert gateway_package is not None


# === E. the sixth call ======================================================


def test_a_sixth_concurrent_call_is_refused(gateway, sessions, run):
    sources = [SyntheticSource(f"cap-{n}") for n in range(APPLICATION_TARGET)]
    sixth = SyntheticSource("cap-sixth")

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)
        refused = await gateway.handle_call(sixth)
        during = voice_call_manager.used_capacity()
        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)
        return refused, during

    refused, during = run(scenario())

    assert refused == CallOutcome.REFUSED
    assert during == APPLICATION_TARGET
    assert gateway.refused == 1
    # The refused caller got no media path and no model session.
    assert len(sessions) == APPLICATION_TARGET
    assert sixth.played == []


def test_the_first_five_are_unharmed_by_the_sixth(gateway, sessions, run):
    sources = [SyntheticSource(f"keep-{n}") for n in range(APPLICATION_TARGET)]

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)
        await gateway.handle_call(SyntheticSource("keep-sixth"))
        # The five can still carry audio afterwards.
        for n, source in enumerate(sources):
            source.speak(speech(n))
        await asyncio.sleep(0.4)
        delivered = [len(s.audio_chunks) for s in sessions[:APPLICATION_TARGET]]
        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)
        return delivered

    delivered = run(scenario())

    assert delivered == [1] * APPLICATION_TARGET


def test_a_freed_slot_admits_the_next_caller(gateway, sessions, run):
    sources = [SyntheticSource(f"slot-{n}") for n in range(APPLICATION_TARGET)]
    replacement = SyntheticSource("slot-new")

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)
        refused = await gateway.handle_call(SyntheticSource("slot-overflow"))

        sources[0].hang_up()
        await asyncio.wait_for(handling[0], timeout=15)
        await asyncio.sleep(0.3)

        admitted = asyncio.create_task(gateway.handle_call(replacement))
        await asyncio.sleep(0.5)
        accepted = phone_call_registry.get("slot-new") is not None

        replacement.hang_up()
        await admitted
        for source in sources[1:]:
            source.hang_up()
        await asyncio.gather(*handling[1:])
        return refused, accepted

    refused, accepted = run(scenario())

    assert refused == CallOutcome.REFUSED
    assert accepted is True


# === G. cleanup =============================================================


def test_five_calls_release_everything(gateway, sessions, run):
    sources = [SyntheticSource(f"clean-{n}") for n in range(APPLICATION_TARGET)]

    async def scenario():
        return await asyncio.gather(*(carry(gateway, s) for s in sources))

    run(scenario())

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert voice_call_manager.active_count() == 0
    assert len(session_manager.list_active_sessions()) == 0
    assert all(session.closed for session in sessions)
    assert all(source.closed for source in sources)
    rows = phone_rows()
    assert len(rows) == APPLICATION_TARGET
    assert all(row.ended_at is not None for row in rows)


def test_no_relay_tasks_outlive_their_calls(gateway, sessions, run):
    sources = [SyntheticSource(f"task-{n}") for n in range(3)]

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.6)
        during = len(
            [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("gw-")]
        )
        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)
        await asyncio.sleep(0.1)
        after = len(
            [
                t
                for t in asyncio.all_tasks()
                if (t.get_name() or "").startswith("gw-") and not t.done()
            ]
        )
        return during, after

    during, after = run(scenario())

    assert during == 6, "two relay tasks per call"
    assert after == 0, "relay tasks outlived their call"


def test_a_call_the_bank_refuses_leaves_nothing_behind(gateway, sessions, run):
    """A refusal must not leave a source open or a slot held."""
    sources = [SyntheticSource(f"ref-{n}") for n in range(APPLICATION_TARGET)]
    refused_source = SyntheticSource("ref-overflow")

    async def scenario():
        handling = [
            asyncio.create_task(gateway.handle_call(source)) for source in sources
        ]
        await asyncio.sleep(0.8)
        await gateway.handle_call(refused_source)
        for source in sources:
            source.hang_up()
        await asyncio.gather(*handling)

    run(scenario())

    assert refused_source.closed is True
    assert refused_source.played == []
    assert voice_call_manager.used_capacity() == 0


def test_a_duplicate_announcement_is_refused_not_doubled(gateway, sessions, run):
    """AC: provider retries stay idempotent through the gateway."""

    async def scenario():
        source = SyntheticSource("call-dupe")
        handling = asyncio.create_task(gateway.handle_call(source))
        await asyncio.sleep(0.5)
        try:
            await gateway._client.announce("call-dupe")
            duplicated = True
        except CallRefused as refusal:
            duplicated = refusal.status != "duplicate"
        source.hang_up()
        await handling
        return duplicated

    duplicated = run(scenario())

    assert duplicated is False
    assert len(phone_rows()) == 1
    assert len(sessions) == 1


def test_the_gateway_reports_the_ending_exactly_once(gateway, sessions, run):
    run(carry(gateway, SyntheticSource("call-endonce")))

    rows = phone_rows()
    assert len(rows) == 1
    assert rows[0].status == "COMPLETED"
    assert rows[0].duration_seconds is not None
