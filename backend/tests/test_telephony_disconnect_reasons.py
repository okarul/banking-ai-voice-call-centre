"""Phase 6.8 correction: the reason a call ended survives the teardown.

Every other test of the disconnect vocabulary sequences the writes by hand.
That proves the database update is idempotent. It does not prove the *order*
is right, and the order was wrong.

Recording the reason used to happen after `tear_down`. Tearing down closes the
media socket, which is exactly what wakes the media route's `finally` — and
that route records `CUSTOMER_ENDED`. Both writers move the row
`WHERE ended_at IS NULL`, so whichever reaches the database first wins for
ever. Whenever the loop scheduled the route first, a goodbye was recorded as a
caller hang-up, a protocol mismatch as a completed call.

So these tests run a **real media socket against the real route**, live and
blocked on `receive()` at the moment the ending happens, and then read the row
back. Each one repeats, because a race proved once is a coincidence.

No DIDWW, no SIP, no live model, no cost.
"""

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.main import create_app
from app.realtime.browser_calls import voice_call_manager
from app.routers.telephony import MEDIA_TOKEN_HEADER
from app.sessions import session_manager
from app.telephony import reasons, service
from app.telephony.bridge import phone_call_registry
from app.telephony.lifecycle import EndReason
from app.telephony.media import PROTOCOL_HELLO, read_protocol_message
from app.telephony.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_payload
from gateway import control as gateway_control

TEST_SECRET = "phase68-reason-ordering-secret-not-a-real-credential"
WEBHOOK = "/api/telephony/incoming"

# Enough repetitions that a scheduling accident cannot pass for a guarantee.
# Each one is a whole call through the real route, so this is not free; it is
# also the only thing that distinguishes "ordered" from "got lucky".
REPEATS = 8


class FakePhoneSession:
    """The paid model session, replaced. Opens no socket, costs nothing."""

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


def until(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def clean_state():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    TOKENS.clear()
    wipe()
    until(lambda: voice_call_manager.used_capacity() == 0)
    yield
    TOKENS.clear()
    asyncio.run(phone_call_registry.close_all())
    asyncio.run(voice_call_manager.close_all())
    asyncio.run(voice_call_manager.release_all())
    until(lambda: voice_call_manager.used_capacity() == 0)
    session_manager.clear()
    wipe()


@pytest.fixture
def server(monkeypatch):
    """The real application, and a handle on the loop it runs its calls on.

    The tests need to start an ending the way the application does — on the
    server's own event loop, while the route is blocked on that same loop.
    Running it from the test thread instead would touch bridges and transports
    bound to a loop that is not the one they live on, which is not the thing
    under test and not a situation production can be in.

    The connector is called on that loop for every accepted call, so it is the
    natural place to capture it.
    """
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "telephony_media_transport", "websocket")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 5)

    captured: dict = {}

    async def connect(_context):
        captured["loop"] = asyncio.get_running_loop()
        return FakePhoneSession()

    monkeypatch.setattr(service, "open_phone_realtime_session", connect)

    with TestClient(create_app()) as running:
        yield running, captured


@pytest.fixture
def adversarial(monkeypatch):
    """Force the scheduling that used to lose, instead of hoping for it.

    Left to itself the loop usually runs the lifecycle's continuation before
    the woken route, so the old ordering passed this test most of the time —
    which is precisely what made the bug survive review and reach a commit. A
    race you cannot reproduce on demand is not covered by a test that happens
    to win it.

    So the teardown is made to wait, after doing its real work, until the
    route's `finally` has actually attempted its write. That is the worst case
    the loop can produce, made certain. With the reason recorded *before* the
    teardown it changes nothing; with it recorded after, the route's
    `CUSTOMER_ENDED` lands first and every assertion below fails.
    """
    from app.observability import recorder

    hangup_attempted = threading.Event()
    attempts: list[str] = []

    real_close = recorder.close_phone_call

    def spy_close(provider_call_id, reason):
        attempts.append(reason)
        if reason == reasons.CALLER_HANGUP:
            hangup_attempted.set()
        return real_close(provider_call_id, reason=reason)

    real_tear_down = service.tear_down
    stalled: set[str] = set()

    async def waiting_tear_down(provider_call_id, banking_session_id):
        # Only the *first* teardown of a call waits, and that is always the
        # lifecycle's: the route cannot wake until this closes its socket. The
        # route's own teardown — which runs second, and which the route must
        # get through before it can write — is left alone. Stalling both would
        # hold up the very write this is waiting for, and the test would pass
        # for the wrong reason.
        first = provider_call_id not in stalled
        stalled.add(provider_call_id)

        result = await real_tear_down(provider_call_id, banking_session_id)

        if first:
            deadline = time.monotonic() + 2.0
            while not hangup_attempted.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
        return result

    monkeypatch.setattr(recorder, "close_phone_call", spy_close)
    monkeypatch.setattr(service, "tear_down", waiting_tear_down)

    def reset():
        hangup_attempted.clear()
        attempts.clear()
        stalled.clear()

    return reset, attempts


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


def attach(client, call_id: str):
    """A raw socket connection, credential attached, nothing answered yet."""
    return client.websocket_connect(
        f"/api/telephony/media/{call_id}",
        headers={MEDIA_TOKEN_HEADER: TOKENS[call_id]},
    )


def answer_hello(socket, *, version: int | None = None) -> None:
    """Complete the handshake the way a gateway does, or with a bad version."""
    hello = read_protocol_message(socket.receive_text())
    assert hello is not None and hello[0] == PROTOCOL_HELLO
    if version is None:
        socket.send_text(gateway_control.protocol_ready_message())
    else:
        socket.send_text(
            json.dumps(
                {
                    "type": "protocol_ready",
                    "version": version,
                    "features": ["playback_ack"],
                }
            )
        )


def row(call_id):
    with session_scope() as db:
        return db.scalars(
            select(AgentSession).where(AgentSession.provider_call_id == call_id)
        ).one()


def on_server(captured, coro):
    """Run a coroutine on the server's loop and wait for it, as the app would."""
    loop = captured["loop"]
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)


def live_call(client, captured, call_id: str):
    """Announce, attach, negotiate, and wait until the call is really running."""
    accepted = announce(client, call_id)
    assert accepted["status"] == "accepted", accepted
    socket = attach(client, call_id)
    entered = socket.__enter__()
    answer_hello(entered)
    assert until(lambda: phone_call_registry.get(call_id) is not None), (
        "the bridge never appeared"
    )
    assert until(lambda: "loop" in captured), "the connector never ran"
    return socket, entered


def settle(call_id: str) -> None:
    """Wait for the route's `finally` to have run and the slot to come back."""
    assert until(lambda: phone_call_registry.get(call_id) is None), (
        "the call never left the registry"
    )
    assert until(lambda: voice_call_manager.used_capacity() == 0), (
        "the capacity slot was never returned"
    )


# === the endings the caller chose ===========================================


@pytest.mark.parametrize(
    "end_reason,expected",
    [
        (EndReason.CALLER_GOODBYE.value, reasons.CALLER_GOODBYE),
        (EndReason.CALLER_SILENT.value, reasons.CALLER_SILENT),
    ],
)
def test_a_lifecycle_ending_outlives_the_socket_that_closes_after_it(
    server, adversarial, end_reason, expected
):
    """A goodbye is recorded as a goodbye, however the loop schedules the route.

    The socket is live and blocked on `receive()` when the ending starts, and
    the caller's line drops *while* the teardown is in progress — which is
    what really happens, because the SIP BYE and the teardown are the same
    event seen from two ends. The route's `finally` therefore wakes mid-
    teardown and tries to write `CUSTOMER_ENDED`; it must find the row closed.

    The close is done from another thread rather than after the ending
    returns. In this harness the server closing its side is not by itself
    enough to wake a `receive()` blocked on an in-process client, so waiting
    until afterwards would test an ordering that never got the chance to go
    wrong — which is exactly how the original bug passed its own tests.
    """
    client, captured = server
    reset, _ = adversarial

    for attempt in range(REPEATS):
        reset()
        call_id = f"ending-{expected}-{attempt}"
        socket, _ = live_call(client, captured, call_id)
        bridge = phone_call_registry.get(call_id)

        def drop_the_line():
            time.sleep(0.05)
            try:
                socket.__exit__(None, None, None)
            except Exception:
                pass

        caller_hangs_up = threading.Thread(target=drop_the_line)
        caller_hangs_up.start()
        try:
            on_server(
                captured,
                service._on_call_ended(
                    call_id, bridge.banking_session_id, end_reason
                ),
            )
        finally:
            caller_hangs_up.join(timeout=10)

        settle(call_id)

        record = row(call_id)
        assert record.disconnect_reason == expected, (
            f"attempt {attempt}: recorded {record.disconnect_reason!r}, "
            f"expected {expected!r} — the socket route won the race"
        )
        assert record.disconnect_reason != reasons.CALLER_HANGUP


# === the endings the application chose ======================================


@pytest.mark.parametrize(
    "cause",
    [reasons.REALTIME_RUNTIME_FAILURE, reasons.MEDIA_FAILURE],
)
def test_a_lost_call_records_which_supplier_failed(server, adversarial, cause):
    """A failure keeps its own name rather than becoming a caller hang-up."""
    client, captured = server
    reset, _ = adversarial

    for attempt in range(REPEATS):
        reset()
        call_id = f"lost-{cause}-{attempt}"
        socket, _ = live_call(client, captured, call_id)
        bridge = phone_call_registry.get(call_id)

        async def lose():
            # The bridge's own path, on the loop it lives on: this is what a
            # broken pump does, and it schedules the teardown as a task.
            bridge._signal_lost(cause)

        try:
            on_server(captured, lose())
        finally:
            try:
                socket.__exit__(None, None, None)
            except Exception:
                pass

        settle(call_id)

        record = row(call_id)
        assert record.disconnect_reason == cause, (
            f"attempt {attempt}: recorded {record.disconnect_reason!r}, "
            f"expected {cause!r}"
        )
        assert record.disconnect_reason != reasons.CALLER_HANGUP


# === the refusal that must not look like a completed call ===================


def test_a_protocol_mismatch_stays_rejected(server, adversarial):
    """The fully organic case: a real gateway answering with a wrong version.

    Nothing is driven by hand here. The socket attaches, answers the hello
    with a version this backend does not speak, and the application refuses
    the call — while that same socket is still open and its route still
    waiting. If the route wins, the row says a caller hung up on a call that
    completed, when in truth the call was refused and never greeted.
    """
    client, captured = server
    reset, _ = adversarial

    for attempt in range(REPEATS):
        reset()
        call_id = f"mismatch-{attempt}"
        accepted = announce(client, call_id)
        assert accepted["status"] == "accepted"

        socket = attach(client, call_id)
        entered = socket.__enter__()
        answer_hello(entered, version=99)

        try:
            # The application gives the call up on its own; the socket closing
            # is a consequence, not the trigger.
            assert until(lambda: phone_call_registry.get(call_id) is None), (
                "the mismatched call was never given up"
            )
        finally:
            try:
                socket.__exit__(None, None, None)
            except Exception:
                pass

        assert until(lambda: voice_call_manager.used_capacity() == 0)

        record = row(call_id)
        assert record.disconnect_reason == reasons.PROTOCOL_MISMATCH, (
            f"attempt {attempt}: recorded {record.disconnect_reason!r}"
        )
        assert record.status == "REJECTED", (
            f"attempt {attempt}: status {record.status!r} — a refused call "
            "was recorded as one that completed"
        )


# === and the hang-up that genuinely is one ==================================


def test_a_real_hang_up_is_still_recorded_as_one(server):
    """The ordering fix must not make `CALLER_HANGUP` unreachable.

    When nothing else has closed the call, the socket closing is the only
    account of why it ended, and it must still be written. A fix that made
    every ending specific by making this one impossible would have replaced
    one wrong answer with another.
    """
    client, captured = server

    call_id = "genuine-hangup"
    socket, _ = live_call(client, captured, call_id)

    socket.__exit__(None, None, None)

    settle(call_id)

    record = row(call_id)
    assert record.disconnect_reason == reasons.CALLER_HANGUP
    assert record.disconnect_reason == "CUSTOMER_ENDED"
