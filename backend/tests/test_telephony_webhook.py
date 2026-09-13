"""Phase 2: the inbound provider event boundary.

The endpoint under test is the only route in this application that answers a
request from outside the machine, so most of what follows is about refusing.
The tests are grouped by the question they answer:

    1. is this really the provider?          signature, timestamp, tampering
    2. is this event well-formed?            content type, schema, event type
    3. have we seen it before?               idempotency, sequential and racing
    4. what did it cost?                     capacity, taken once and returned
    5. who is calling?                       nobody, and the event cannot say
    6. did anything leak?                    responses, logs, public settings

Time is injected, never slept. Staleness is checked by handing the verifier a
chosen instant, which makes the boundary conditions exact instead of
approximate and keeps the suite fast.

All customers and PINs here are synthetic seed data.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.main import create_app
from app.realtime.browser_calls import browser_call_manager
from app.sessions import session_manager
from app.telephony import reasons
from app.telephony.bridge import phone_call_registry
from app.telephony import service as telephony_service
from app.telephony.schemas import InboundCallEvent
from app.telephony.signature import (
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ProviderVerificationError,
    sign_payload,
    verify_provider_request,
)

# A deterministic test credential. Not a real secret and never used anywhere
# but here — the point is that the test signs exactly as the verifier verifies.
TEST_SECRET = "phase2-test-signing-secret-not-a-real-credential"

PINS = {"DEMO001": "4821", "DEMO002": "7315"}

ENDPOINT = "/api/telephony/incoming"


@pytest.fixture(autouse=True)
def clean_operational_tables():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    asyncio.run(browser_call_manager.close_all())
    # Reservations are not connections, so `close_all` does not clear them. A
    # test that exercised a failure path between reserving and registering
    # would otherwise leak a slot into the next test.
    asyncio.run(browser_call_manager.release_all())
    session_manager.clear()
    wipe()


class _StubRealtimeSession:
    """Stands in for the paid model session. Opens no socket, costs nothing."""

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


async def _stub_connector(_context):
    return _StubRealtimeSession()


@pytest.fixture
def telephony_on(monkeypatch):
    """An application built with the telephone channel switched on.

    The model connector is stubbed. Every test in this file is about the
    *bank's* behaviour — signature checking, admission, capacity, the PIN flow,
    per-call isolation — and none of it is about the model provider. Left
    unstubbed it opened a real paid realtime session for every accepted call,
    which made the whole file fail whenever that provider was unreachable,
    out of credit, or merely slow. A deterministic suite must not depend on an
    external service being up to tell us whether our own routing works.
    """
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(
        telephony_service, "open_phone_realtime_session", _stub_connector
    )
    return create_app()


@pytest.fixture
def client(telephony_on):
    return TestClient(telephony_on)


def event_body(
    *,
    event_type: str = "incoming",
    call_id: str = "call-0001",
    event_id: str = "evt-0001",
    **overrides,
) -> bytes:
    payload = {
        "provider": "DIDWW",
        "provider_event_id": event_id,
        "provider_call_id": call_id,
        "event_type": event_type,
        "event_timestamp": "2026-08-20T10:00:00Z",
        "source": "+6591234567",
        "destination": "+6531252836",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def signed_headers(body: bytes, *, timestamp: int | None = None) -> dict:
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: sign_payload(TEST_SECRET, stamp, body),
    }


def post(client, body: bytes, headers: dict | None = None):
    return client.post(ENDPOINT, content=body, headers=headers or signed_headers(body))


def phone_rows() -> list[AgentSession]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(AgentSession)
                .where(AgentSession.channel == "PHONE")
                .order_by(AgentSession.id)
            )
        )


# === 1. is this really the provider? ========================================


def test_a_correctly_signed_event_is_accepted(client):
    response = post(client, event_body())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["duplicate"] is False
    assert len(phone_rows()) == 1


def test_an_unsigned_event_is_refused(client):
    body = event_body()
    response = client.post(
        ENDPOINT, content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    assert phone_rows() == []


def test_a_wrongly_signed_event_is_refused(client):
    body = event_body()
    headers = signed_headers(body)
    headers[SIGNATURE_HEADER] = "v1=" + "0" * 64

    assert post(client, body, headers).status_code == 401
    assert phone_rows() == []


def test_a_signature_from_a_different_secret_is_refused(client):
    body = event_body()
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: sign_payload("some-other-secret", stamp, body),
    }

    assert post(client, body, headers).status_code == 401
    assert phone_rows() == []


def test_a_payload_edited_after_signing_is_refused(client):
    """The decisive tamper test: valid signature, different body."""
    original = event_body(call_id="call-original")
    headers = signed_headers(original)
    tampered = event_body(call_id="call-substituted")

    assert post(client, tampered, headers).status_code == 401
    assert phone_rows() == []


def test_a_missing_timestamp_is_refused(client):
    body = event_body()
    headers = signed_headers(body)
    del headers[TIMESTAMP_HEADER]

    assert post(client, body, headers).status_code == 401


def test_every_verification_failure_says_the_same_thing(client):
    """A refusal that explains itself is a hint for the next attempt."""
    body = event_body()

    unsigned = client.post(
        ENDPOINT, content=body, headers={"Content-Type": "application/json"}
    )
    wrong = signed_headers(body)
    wrong[SIGNATURE_HEADER] = "v1=" + "0" * 64
    invalid = post(client, body, wrong)
    stale_headers = signed_headers(body, timestamp=int(time.time()) - 86_400)
    stale = post(client, body, stale_headers)

    bodies = {unsigned.text, invalid.text, stale.text}
    assert len(bodies) == 1, bodies
    for response in (unsigned, invalid, stale):
        assert response.status_code == 401
        for leaked in ("SIGNATURE", "TIMESTAMP", "secret", "hmac", "expected"):
            assert leaked.lower() not in response.text.lower()


# --- timestamp handling, at chosen instants rather than by sleeping ---------


def _verify_at(moment: datetime, *, offset_seconds: int = 0):
    body = event_body()
    stamp = str(int((moment + timedelta(seconds=offset_seconds)).timestamp()))
    return verify_provider_request(
        body=body,
        timestamp_header=stamp,
        signature_header=sign_payload(TEST_SECRET, stamp, body),
        secret=TEST_SECRET,
        tolerance_seconds=300,
        now=moment,
    )


def test_a_fresh_timestamp_verifies():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    assert _verify_at(now, offset_seconds=-10) is not None


def test_a_timestamp_inside_the_tolerance_verifies():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    assert _verify_at(now, offset_seconds=-299) is not None


def test_a_stale_timestamp_is_rejected():
    """A captured request stops being replayable once it ages out."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(ProviderVerificationError) as error:
        _verify_at(now, offset_seconds=-301)
    assert error.value.reason == "TIMESTAMP_STALE"


def test_a_far_future_timestamp_is_rejected():
    """Otherwise a chosen clock mints a request valid for as long as you like."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(ProviderVerificationError) as error:
        _verify_at(now, offset_seconds=+301)
    assert error.value.reason == "TIMESTAMP_IN_FUTURE"


def test_a_replayed_request_stops_working_once_it_is_stale():
    """The same bytes and signature: valid now, refused later."""
    captured_at = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    body = event_body()
    stamp = str(int(captured_at.timestamp()))
    signature = sign_payload(TEST_SECRET, stamp, body)

    def attempt(now):
        return verify_provider_request(
            body=body,
            timestamp_header=stamp,
            signature_header=signature,
            secret=TEST_SECRET,
            tolerance_seconds=300,
            now=now,
        )

    assert attempt(captured_at + timedelta(seconds=60)) is not None
    with pytest.raises(ProviderVerificationError):
        attempt(captured_at + timedelta(hours=1))


def test_a_malformed_timestamp_is_rejected():
    for rubbish in ("not-a-number", "", "  ", "12.5", "1e9999"):
        with pytest.raises(ProviderVerificationError):
            verify_provider_request(
                body=b"{}",
                timestamp_header=rubbish,
                signature_header="v1=" + "0" * 64,
                secret=TEST_SECRET,
                tolerance_seconds=300,
            )


def test_verification_refuses_everything_when_no_secret_is_configured():
    """An unconfigured secret must never mean 'skip the check'."""
    with pytest.raises(ProviderVerificationError) as error:
        verify_provider_request(
            body=b"{}",
            timestamp_header=str(int(time.time())),
            signature_header="v1=abc",
            secret=None,
            tolerance_seconds=300,
        )
    assert error.value.reason == "NOT_CONFIGURED"


def test_the_route_does_not_exist_without_a_secret(monkeypatch):
    """Telephony on, secret missing: no endpoint at all."""
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", None)

    paths = set(create_app().openapi()["paths"])
    assert ENDPOINT not in paths


def test_the_route_exists_when_configured(telephony_on):
    assert ENDPOINT in set(telephony_on.openapi()["paths"])


# === 2. is this event well-formed? ==========================================


def test_a_non_json_content_type_is_refused(client):
    body = event_body()
    headers = signed_headers(body)
    headers["Content-Type"] = "text/plain"

    assert post(client, body, headers).status_code == 415
    assert phone_rows() == []


def test_a_malformed_body_is_refused(client):
    body = b"{not json at all"

    assert post(client, body).status_code == 422
    assert phone_rows() == []


def test_a_payload_missing_a_required_field_is_refused(client):
    payload = json.loads(event_body())
    del payload["provider_call_id"]
    body = json.dumps(payload).encode()

    assert post(client, body).status_code == 422
    assert phone_rows() == []


def test_an_unsupported_event_type_is_refused(client):
    for unsupported in ("ringing", "answered", "completed", "initiated", "transfer"):
        body = event_body(event_type=unsupported)
        assert post(client, body).status_code == 422, unsupported
    assert phone_rows() == []


def test_an_unknown_field_is_rejected_not_ignored(client):
    """Strictness is the control: an ignored extra is an accepted extra."""
    body = event_body(unexpected_field="anything")

    assert post(client, body).status_code == 422
    assert phone_rows() == []


def test_an_identifier_with_odd_characters_is_refused(client):
    for hostile in ("call'; DROP TABLE agent_sessions;--", "call\nid", "call id"):
        body = event_body(call_id=hostile)
        assert post(client, body).status_code == 422
    assert phone_rows() == []


def test_an_oversized_body_is_refused_before_it_is_hashed(client):
    body = b'{"padding":"' + b"x" * (80 * 1024) + b'"}'

    response = post(client, body, signed_headers(body))
    assert response.status_code == 413
    assert phone_rows() == []


def test_the_endpoint_accepts_only_post(client):
    for method in ("get", "put", "delete", "patch"):
        response = getattr(client, method)(ENDPOINT)
        assert response.status_code == 405, method


# === 3. have we seen it before? =============================================


def test_a_duplicate_event_sent_twice_creates_one_call(client):
    body = event_body(call_id="call-dup", event_id="evt-dup")

    first = post(client, body)
    second = post(client, body)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert second.json()["duplicate"] is True
    # A retry must not be answered with an error, or the provider retries again.
    assert second.status_code == 200
    assert len(phone_rows()) == 1


def test_a_second_event_reusing_a_call_id_creates_nothing(client):
    """Same call, different notification: still one call."""
    post(client, event_body(call_id="call-same", event_id="evt-a"))
    response = post(client, event_body(call_id="call-same", event_id="evt-b"))

    assert response.json()["status"] == "duplicate"
    assert len(phone_rows()) == 1


def test_a_second_event_reusing_an_event_id_creates_nothing(client):
    """Same notification id against a different call is also a repeat."""
    post(client, event_body(call_id="call-x", event_id="evt-same"))
    response = post(client, event_body(call_id="call-y", event_id="evt-same"))

    assert response.json()["status"] == "duplicate"
    assert len(phone_rows()) == 1


def test_two_different_calls_both_register(client):
    post(client, event_body(call_id="call-1", event_id="evt-1"))
    post(client, event_body(call_id="call-2", event_id="evt-2"))

    assert len(phone_rows()) == 2


def test_the_duplicate_response_is_deterministic(client):
    body = event_body(call_id="call-det", event_id="evt-det")
    post(client, body)

    responses = [post(client, body).json() for _ in range(4)]
    assert all(response == responses[0] for response in responses)
    assert responses[0]["status"] == "duplicate"


def test_the_database_refuses_a_duplicate_provider_call_id():
    """The guarantee is the index, not the code path that usually reaches it."""
    from sqlalchemy.exc import IntegrityError

    from app.observability import recorder

    session = session_manager.create_session()
    other = session_manager.create_session()
    recorder.claim_phone_call(
        session.session_id, provider_call_id="call-unique", provider_event_id="evt-1"
    )

    with pytest.raises(recorder.DuplicateProviderCall):
        recorder.claim_phone_call(
            other.session_id,
            provider_call_id="call-unique",
            provider_event_id="evt-2",
        )

    # And directly, with no application code in the way.
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(
                AgentSession(
                    agent_session_id="AGT-999999",
                    banking_session_id="anything",
                    channel="PHONE",
                    provider_call_id="call-unique",
                    status="ACTIVE",
                    auth_status="PENDING",
                    started_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )


def test_many_browser_calls_may_all_have_no_provider_id(client, monkeypatch):
    """The unique index must not make NULL a single-use value."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    # This test is about the index, not the ceiling: lift the limit so five
    # browser calls can coexist and all sit at a NULL provider id.
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 0)

    for _ in range(5):
        assert client.post("/api/call/start").status_code == 201

    with session_scope() as db:
        null_rows = db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.provider_call_id.is_(None))
        )
    assert null_rows == 5


# --- the race ---------------------------------------------------------------


def _concurrent_duplicates(application, body, headers, *, callers):
    """Send the same event from `callers` threads through ONE live application.

    Returns the statuses and a snapshot of what the bank was holding *while it
    was still running* - which is the only moment a capacity slot or a bridge is
    supposed to exist, and the thing the previous shape could not observe.

    The threads are started before any is joined, so they contend for the unique
    index genuinely; nothing here serialises them.
    """
    import threading

    statuses = []
    lock = threading.Lock()

    with TestClient(application) as client:
        def fire():
            answer = client.post(ENDPOINT, content=body, headers=headers).json()
            with lock:
                statuses.append(answer["status"])

        threads = [threading.Thread(target=fire) for _ in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        live = {
            "rows": len(phone_rows()),
            "capacity": browser_call_manager.used_capacity(),
            "bridges": phone_call_registry.active_count(),
        }

    return statuses, live



def test_concurrent_duplicate_events_produce_exactly_one_call(telephony_on):
    """Two workers, the same event, at the same moment.

    The check-then-insert this replaces would let both find nothing and both
    proceed. Only the database can settle it, and this is the test that says
    so: one accepted, one duplicate, one row, one capacity slot.
    """
    body = event_body(call_id="call-race", event_id="evt-race")
    headers = signed_headers(body)

    # **One application, several callers.** Each thread used to open its own
    # `TestClient` context, which starts and stops a whole application lifespan
    # per thread. Phase 7.4A.1 measured what that manufactures: a *startup*
    # running while another context is mid-admission, so a reconciliation
    # samples ownership before the row exists, queries after it commits, and
    # repairs a live call as a crash orphan. FORCED_CLEANUP, about half the time.
    #
    # Production cannot do that. `app.main.lifespan` reconciles before its
    # `yield`, and uvicorn serves nothing until startup returns, so under the
    # supported topology - one process, one worker, one lifespan - no admission
    # is ever concurrent with a reconciliation. The overlapping-lifespan case is
    # the documented, unsolved limitation, not what these tests are for.
    #
    # So one lifespan, and the concurrency moves to where it belongs: several
    # threads posting the same event through the same running application. That
    # is the race this test exists to describe - two workers reaching the unique
    # index at once - and it is still genuinely concurrent.
    statuses, live = _concurrent_duplicates(telephony_on, body, headers, callers=2)

    assert sorted(statuses) == ["accepted", "duplicate"], statuses

    # Observed while the application was still running, which is the only
    # moment a slot is meant to be held at all. The old shape could not assert
    # this: a sibling context's shutdown could release the call first.
    assert live["rows"] == 1, live
    assert live["capacity"] == 1, live
    assert live["bridges"] == 1, live

    # And after that one lifespan has stopped: the drain released the call, and
    # the row says the drain is what did it. `FORCED_CLEANUP` here would mean a
    # live call had been repaired as a crash orphan.
    assert browser_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    row = phone_rows()[0]
    assert row.ended_at is not None
    assert row.disconnect_reason == reasons.SERVICE_SHUTDOWN


def test_many_concurrent_duplicates_still_produce_one_call(telephony_on):
    body = event_body(call_id="call-race-8", event_id="evt-race-8")
    headers = signed_headers(body)

    # **One application, several callers.** Each thread used to open its own
    # `TestClient` context, which starts and stops a whole application lifespan
    # per thread. Phase 7.4A.1 measured what that manufactures: a *startup*
    # running while another context is mid-admission, so a reconciliation
    # samples ownership before the row exists, queries after it commits, and
    # repairs a live call as a crash orphan. FORCED_CLEANUP, about half the time.
    #
    # Production cannot do that. `app.main.lifespan` reconciles before its
    # `yield`, and uvicorn serves nothing until startup returns, so under the
    # supported topology - one process, one worker, one lifespan - no admission
    # is ever concurrent with a reconciliation. The overlapping-lifespan case is
    # the documented, unsolved limitation, not what these tests are for.
    #
    # So one lifespan, and the concurrency moves to where it belongs: several
    # threads posting the same event through the same running application. That
    # is the race this test exists to describe - two workers reaching the unique
    # index at once - and it is still genuinely concurrent.
    statuses, live = _concurrent_duplicates(telephony_on, body, headers, callers=8)

    assert statuses.count("accepted") == 1, statuses
    assert statuses.count("duplicate") == 7, statuses

    assert live["rows"] == 1, live
    assert live["capacity"] == 1, live
    assert live["bridges"] == 1, live

    # And after that one lifespan has stopped: the drain released the call, and
    # the row says the drain is what did it. `FORCED_CLEANUP` here would mean a
    # live call had been repaired as a crash orphan.
    assert browser_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    row = phone_rows()[0]
    assert row.ended_at is not None
    assert row.disconnect_reason == reasons.SERVICE_SHUTDOWN


# === 4. what did it cost? ===================================================


def test_an_accepted_call_consumes_exactly_one_slot(client):
    assert browser_call_manager.used_capacity() == 0

    post(client, event_body(call_id="call-cap", event_id="evt-cap"))

    assert browser_call_manager.used_capacity() == 1


def test_a_duplicate_consumes_no_further_capacity(client):
    body = event_body(call_id="call-cap2", event_id="evt-cap2")
    post(client, body)
    before = browser_call_manager.used_capacity()

    for _ in range(3):
        post(client, body)

    assert browser_call_manager.used_capacity() == before == 1


def test_phone_and_browser_share_one_ceiling(client, monkeypatch):
    """One bank, one limit. Not a pool each."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 2)

    assert client.post("/api/call/start").status_code == 201
    assert browser_call_manager.used_capacity() == 1

    accepted = post(client, event_body(call_id="call-share", event_id="evt-share"))
    assert accepted.json()["status"] == "accepted"
    assert browser_call_manager.used_capacity() == 2

    # The ceiling is now reached, and it is reached for both channels.
    assert client.post("/api/call/start").status_code == 503
    refused = post(client, event_body(call_id="call-share2", event_id="evt-share2"))
    assert refused.json()["status"] == "rejected_capacity"


def test_a_capacity_refusal_leaves_no_partial_call(client, monkeypatch):
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    post(client, event_body(call_id="call-a", event_id="evt-a"))

    response = post(client, event_body(call_id="call-b", event_id="evt-b"))

    assert response.status_code == 200
    assert response.json()["status"] == "rejected_capacity"
    # One slot held by the accepted call, none by the refused one.
    assert browser_call_manager.used_capacity() == 1

    rows = {row.provider_call_id: row for row in phone_rows()}
    refused = rows["call-b"]
    assert refused.status == "REJECTED"
    assert refused.ended_at is not None
    assert refused.banking_session_id is None
    assert refused.customer_id is None
    # And no orphan banking session left behind in memory.
    assert len(session_manager.list_active_sessions()) == 1


def test_a_refused_call_does_not_block_an_active_one(client, monkeypatch):
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    post(client, event_body(call_id="call-keep", event_id="evt-keep"))
    post(client, event_body(call_id="call-turned-away", event_id="evt-turned-away"))

    rows = {row.provider_call_id: row.status for row in phone_rows()}
    assert rows["call-keep"] == "ACTIVE"
    assert rows["call-turned-away"] == "REJECTED"


# === lifecycle ==============================================================


def test_an_end_event_closes_the_call_and_returns_its_slot(client):
    post(client, event_body(call_id="call-end", event_id="evt-start"))
    assert browser_call_manager.used_capacity() == 1

    response = post(
        client, event_body(event_type="ended", call_id="call-end", event_id="evt-end")
    )

    assert response.json()["status"] == "ended"
    assert browser_call_manager.used_capacity() == 0
    row = phone_rows()[0]
    assert row.status == "COMPLETED"
    assert row.ended_at is not None


def test_a_duplicate_end_event_releases_capacity_only_once(client):
    """Two hang-ups for one call must not free two slots."""
    post(client, event_body(call_id="call-e2", event_id="evt-s2"))
    post(client, event_body(call_id="call-e2b", event_id="evt-s2b"))
    assert browser_call_manager.used_capacity() == 2

    end = event_body(event_type="ended", call_id="call-e2", event_id="evt-e2")
    first = post(client, end)
    second = post(client, end)
    third = post(
        client, event_body(event_type="ended", call_id="call-e2", event_id="evt-e2c")
    )

    assert first.json()["status"] == "ended"
    assert second.json()["status"] == "already_ended"
    assert third.json()["status"] == "already_ended"
    # The other call still holds its slot, and only one was returned.
    assert browser_call_manager.used_capacity() == 1


def test_an_end_event_for_an_unknown_call_changes_nothing(client):
    post(client, event_body(call_id="call-live", event_id="evt-live"))
    before = browser_call_manager.used_capacity()

    response = post(
        client,
        event_body(event_type="ended", call_id="call-never-existed", event_id="evt-x"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "already_ended"
    assert browser_call_manager.used_capacity() == before
    assert phone_rows()[0].status == "ACTIVE"


def test_ending_one_call_cannot_end_another(client):
    post(client, event_body(call_id="call-alice", event_id="evt-alice"))
    post(client, event_body(call_id="call-bob", event_id="evt-bob"))

    post(
        client,
        event_body(event_type="ended", call_id="call-alice", event_id="evt-alice-end"),
    )

    rows = {row.provider_call_id: row.status for row in phone_rows()}
    assert rows["call-alice"] == "COMPLETED"
    assert rows["call-bob"] == "ACTIVE"
    assert browser_call_manager.used_capacity() == 1


def test_an_end_event_cannot_close_a_browser_call(client, monkeypatch):
    """Channel scoping: a provider id belongs to the telephone channel only."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    client.post("/api/call/start")

    post(client, event_body(event_type="ended", call_id="call-none", event_id="evt-n"))

    with session_scope() as db:
        browser = db.scalars(
            select(AgentSession).where(AgentSession.channel == "WEBRTC")
        ).one()
    assert browser.status == "ACTIVE"
    assert browser.ended_at is None


# === 5. who is calling? =====================================================


def test_an_incoming_event_authenticates_nobody(client):
    post(client, event_body(call_id="call-anon", event_id="evt-anon"))

    row = phone_rows()[0]
    assert row.customer_id is None
    assert row.authenticated is False
    assert row.auth_status == "PENDING"


def test_the_schema_has_no_way_to_claim_an_identity():
    """Structural: the fields do not exist, so they cannot be trusted."""
    fields = set(InboundCallEvent.model_fields)

    for forbidden in (
        "customer_id", "authenticated", "verified", "account_id", "pin", "role",
    ):
        assert forbidden not in fields, forbidden


@pytest.mark.parametrize(
    "claim",
    [
        {"customer_id": "DEMO001"},
        {"authenticated": True},
        {"verified": True},
        {"account_id": "XXXX1001"},
    ],
)
def test_an_event_claiming_an_identity_is_rejected_outright(client, claim):
    body = event_body(**claim)

    assert post(client, body).status_code == 422
    assert phone_rows() == []


def test_the_caller_number_is_never_stored(client):
    post(client, event_body(call_id="call-cli", event_id="evt-cli"))

    row = phone_rows()[0]
    stored = " ".join(
        str(getattr(row, column.name)) for column in AgentSession.__table__.columns
    )
    assert "6591234567" not in stored
    assert row.customer_id is None


def test_a_caller_number_shaped_like_a_customer_id_confers_nothing(client):
    post(client, event_body(call_id="call-cli2", event_id="evt-cli2", source="DEMO001"))

    row = phone_rows()[0]
    assert row.customer_id is None
    assert row.authenticated is False


def test_a_provider_call_id_shaped_like_a_customer_id_confers_nothing(client):
    post(client, event_body(call_id="DEMO001", event_id="evt-shaped"))

    row = phone_rows()[0]
    assert row.provider_call_id == "DEMO001"
    assert row.customer_id is None
    assert row.authenticated is False


def test_a_phone_session_cannot_read_banking_data_before_the_pin(client):
    """The registered session is a session like any other: unauthenticated."""
    from app.authorization.errors import AuthorizationError
    from app.authorization.guards import require_authenticated_customer

    post(client, event_body(call_id="call-guard", event_id="evt-guard"))
    banking_session_id = phone_rows()[0].banking_session_id

    with pytest.raises(AuthorizationError):
        require_authenticated_customer(banking_session_id)


def test_the_ordinary_pin_flow_works_on_a_phone_session(client):
    """Channel 2 reuses the authentication of Channel 1, unchanged."""
    from app.auth.authentication import submit_customer_id, submit_pin

    post(client, event_body(call_id="call-auth", event_id="evt-auth"))
    banking_session_id = phone_rows()[0].banking_session_id

    assert submit_customer_id(banking_session_id, "DEMO001")["success"] is True
    result = submit_pin(banking_session_id, PINS["DEMO001"])

    assert result["authenticated"] is True
    assert result["customer_id"] == "DEMO001"
    assert session_manager.get_session(banking_session_id).customer_id == "DEMO001"


def test_a_phone_session_and_a_browser_session_stay_separate(client, monkeypatch):
    from app.auth.authentication import submit_customer_id, submit_pin

    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)

    browser_session_id = client.post("/api/call/start").json()["session_id"]
    post(client, event_body(call_id="call-iso", event_id="evt-iso"))
    phone_session_id = phone_rows()[0].banking_session_id

    submit_customer_id(phone_session_id, "DEMO001")
    submit_pin(phone_session_id, PINS["DEMO001"])

    # The phone caller verified. The browser caller did not become them.
    assert session_manager.get_session(phone_session_id).customer_id == "DEMO001"
    browser = session_manager.get_session(browser_session_id)
    assert browser.customer_id is None
    assert browser.authenticated is False


def test_a_phone_call_cannot_take_over_an_existing_session(client, monkeypatch):
    """Registration always mints a new session; it never adopts one."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    browser_session_id = client.post("/api/call/start").json()["session_id"]

    post(client, event_body(call_id="call-takeover", event_id="evt-takeover"))

    assert phone_rows()[0].banking_session_id != browser_session_id


# === 6. did anything leak? ==================================================


def test_no_response_carries_the_signing_secret(client):
    bodies = [
        post(client, event_body()).text,
        post(client, event_body()).text,
        client.post(ENDPOINT, content=b"{}", headers={}).text,
    ]

    for body in bodies:
        assert TEST_SECRET not in body
        assert "secret" not in body.lower()


def test_public_settings_carry_no_webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)
    public = settings.public_settings()

    assert "telephony_webhook_secret" not in public
    assert TEST_SECRET not in str(public)


def test_the_secret_is_not_reachable_through_any_route(client):
    for path in ("/", "/health", "/api/admin/dashboard/summary"):
        assert TEST_SECRET not in client.get(path).text


def test_a_refusal_never_names_an_internal_detail(client):
    body = b'{"broken'
    response = post(client, body)

    for internal in (
        "Traceback", "sqlalchemy", "psycopg", "agent_sessions", "pydantic",
        ".py", "app/", "IntegrityError",
    ):
        assert internal.lower() not in response.text.lower(), internal


def test_an_unexpected_failure_returns_a_generic_response(client, monkeypatch):
    async def explode(_payload):
        raise RuntimeError("postgresql://user:hunter2@127.0.0.1:5435/banking_ai_demo")

    monkeypatch.setattr("app.telephony.service.handle_event", explode)

    response = post(client, event_body())

    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert "postgresql" not in response.text
    assert response.json()["status"] == "error"


def test_the_logged_event_carries_no_caller_number(client, caplog):
    with caplog.at_level("INFO", logger="app.telephony"):
        post(client, event_body(call_id="call-log", event_id="evt-log"))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "6591234567" not in logged
    assert TEST_SECRET not in logged
    # But it does say enough to investigate with.
    assert "call-log" in logged


def test_the_dashboard_shows_a_phone_call_without_provider_internals(client):
    post(client, event_body(call_id="call-dash", event_id="evt-dash"))

    response = client.get("/api/admin/agents")
    assert response.status_code == 200
    rows = response.json()["sessions"]
    assert any(row["channel"] == "PHONE" for row in rows)
    for leaked in (TEST_SECRET, "6591234567", "sip:", "signature"):
        assert leaked not in response.text


# === regressions found during the Phase 2 security review ===================


def test_an_id_collision_is_not_reported_as_a_duplicate(monkeypatch):
    """Two different calls arriving together must both be registered.

    `agent_session_id` is derived from `max(id)`, so simultaneous inserts can
    collide on the operator-facing name. That is an integrity error with
    nothing to do with duplication, and the first version of this code treated
    every integrity error alike — which would have acknowledged a real second
    caller as a repeat and never registered their call at all.
    """
    from app.observability import recorder

    real_next_id = recorder._next_agent_session_id

    first = session_manager.create_session()
    taken_name = recorder.claim_phone_call(
        first.session_id,
        provider_call_id="call-collide-1",
        provider_event_id="evt-collide-1",
    )

    # The next claim asks for a name that is already in use, exactly once —
    # which is what losing the race for `max(id)` looks like.
    collided = {"yet": False}

    def hand_out_a_taken_name(db):
        if not collided["yet"]:
            collided["yet"] = True
            return taken_name
        return real_next_id(db)

    monkeypatch.setattr(recorder, "_next_agent_session_id", hand_out_a_taken_name)

    second = session_manager.create_session()
    second_name = recorder.claim_phone_call(
        second.session_id,
        provider_call_id="call-collide-2",
        provider_event_id="evt-collide-2",
    )

    assert collided["yet"] is True
    assert second_name != taken_name
    assert len(phone_rows()) == 2


def test_a_real_duplicate_is_still_reported_as_one(monkeypatch):
    """The discrimination must not have made every conflict a retry."""
    from app.observability import recorder

    session = session_manager.create_session()
    recorder.claim_phone_call(
        session.session_id,
        provider_call_id="call-still-dup",
        provider_event_id="evt-still-dup",
    )

    other = session_manager.create_session()
    with pytest.raises(recorder.DuplicateProviderCall):
        recorder.claim_phone_call(
            other.session_id,
            provider_call_id="call-still-dup",
            provider_event_id="evt-different",
        )


def test_an_oversized_body_is_refused_on_its_declared_length(client):
    """Refused before the body is read, not after it is already in memory."""
    body = event_body()
    headers = signed_headers(body)
    headers["Content-Length"] = str(MAX_BODY_BYTES + 1)

    # Starlette will not send a body longer than the content we give it, so the
    # declared length is what the check must act on.
    response = client.post(ENDPOINT, content=body, headers=headers)
    assert response.status_code == 413


def test_the_webhook_secret_is_scrubbed_from_logs(monkeypatch):
    """It has no recognisable shape, so only the literal rule can catch it."""
    from app.redaction import redact

    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)

    scrubbed = redact(f"boom while verifying with {TEST_SECRET} in scope")

    assert TEST_SECRET not in scrubbed
    assert "<redacted>" in scrubbed


def test_a_traceback_mentioning_the_secret_is_scrubbed(caplog, monkeypatch):
    import logging

    from app.redaction import install_redaction

    monkeypatch.setattr(settings, "telephony_webhook_secret", TEST_SECRET)

    logger = logging.getLogger("test.telephony.leak")
    install_redaction(logger)

    with caplog.at_level(logging.ERROR, logger="test.telephony.leak"):
        logger.error("verification failed: secret=%s", TEST_SECRET)

    assert TEST_SECRET not in caplog.text
