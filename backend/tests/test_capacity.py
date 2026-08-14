"""Admission control, provider-error naming, and what the customer is told.

Phase 12 measured a live ceiling: beyond it, calls did not fail — they connected
and then stalled silently, which is the worst of both worlds. A caller who is
told the lines are busy can try again; a caller sitting in silence cannot tell a
stalled call from a broken bank.

Three things are checked here:

* the limit is **configuration**, defaults to off, and cannot be tripped by two
  callers starting at the same instant
* a refused call **fails closed** — no half-open call, no orphaned session, no
  reuse of anybody else's connection
* the customer sentence names no provider, no number and no error

All calls in this file use an injected connector. Nothing here is billable.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.auth import authentication
from app.main import app
from app.realtime.browser_calls import browser_call_manager
from app.realtime.provider_errors import (
    CONCURRENCY_LIMIT,
    NETWORK,
    PROVIDER_UNAVAILABLE,
    QUOTA_EXHAUSTED,
    RATE_LIMIT,
    RETRYABLE,
    TIMEOUT,
    UNKNOWN,
    classify_provider_error,
    classify_status,
    retry_after_seconds,
)
from app.realtime.realtime_manager import (
    MESSAGES,
    Reason,
    RealtimeConnection,
    RealtimeManager,
    RealtimeSessionError,
)
from app.sessions import SessionManager, session_manager

PINS = {"DEMO001": "4821", "DEMO002": "7315"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    asyncio.run(browser_call_manager.close_all())
    session_manager.clear()


class FakeSession:
    """A provider session with no event stream, like a browser call."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def connector():
    async def connect(_context):
        return FakeSession()

    return connect


def _session(manager, customer_id="DEMO001"):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, customer_id, manager=manager)
    authentication.verify_pin(session.session_id, PINS[customer_id], manager=manager)
    return session


# === the limit is configuration =============================================


def test_no_limit_is_configured_by_default(monkeypatch):
    """The default must never reduce capacity that already works.

    The variable is removed first: this asserts what the code does when nothing
    is configured, and a developer machine with a value in `.env` must not
    change the answer.
    """
    from app.config import Settings

    monkeypatch.delenv("REALTIME_MAX_ACTIVE_SESSIONS", raising=False)

    assert Settings().realtime_max_active_sessions == 0


def test_an_unset_limit_means_calls_are_not_refused(manager):
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=0)

    for _ in range(6):
        session = _session(manager)
        run(realtime.start(session.session_id))

    assert realtime.active_count() == 6
    assert realtime.at_capacity() is False


def test_a_nonsense_limit_falls_back_to_no_limit(monkeypatch):
    """A misspelt setting must not become an accidental cap of zero calls."""
    from app.config import Settings

    for bad in ("three", "", "-1", "  "):
        monkeypatch.setenv("REALTIME_MAX_ACTIVE_SESSIONS", bad)
        assert Settings().realtime_max_active_sessions == 0


def test_a_configured_limit_is_read_from_the_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("REALTIME_MAX_ACTIVE_SESSIONS", "3")
    assert Settings().realtime_max_active_sessions == 3


def test_the_limit_is_read_live_so_an_operator_can_change_it(manager, monkeypatch):
    """No rebuild, no restart of the manager object."""
    from app.config import settings

    realtime = RealtimeManager(connect=connector(), manager=manager)
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 2)

    assert realtime.max_active == 2
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 7)
    assert realtime.max_active == 7


# === refusing at the ceiling ================================================


def test_a_call_beyond_the_limit_is_refused(manager):
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=2)

    first = _session(manager)
    second = _session(manager)
    third = _session(manager)
    run(realtime.start(first.session_id))
    run(realtime.start(second.session_id))

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.start(third.session_id))

    assert error.value.reason == Reason.REALTIME_AT_CAPACITY


def test_two_callers_starting_together_cannot_both_pass_the_check(manager):
    """The check is inside the lock, so the race has one winner."""
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=1)
    first = _session(manager)
    second = _session(manager)

    async def both():
        return await asyncio.gather(
            realtime.start(first.session_id),
            realtime.start(second.session_id),
            return_exceptions=True,
        )

    outcomes = run(both())
    refused = [o for o in outcomes if isinstance(o, RealtimeSessionError)]

    assert len(refused) == 1
    assert refused[0].reason == Reason.REALTIME_AT_CAPACITY
    assert realtime.active_count() == 1


def test_a_refused_call_leaves_nothing_behind(manager):
    """Fail closed: no half-open call, and the existing one is untouched."""
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=1)
    live = _session(manager, "DEMO001")
    turned_away = _session(manager, "DEMO002")
    run(realtime.start(live.session_id))

    with pytest.raises(RealtimeSessionError):
        run(realtime.start(turned_away.session_id))

    assert realtime.active_count() == 1
    assert realtime.is_active(turned_away.session_id) is False
    assert realtime.get(turned_away.session_id) is None
    # The refused caller got no realtime id, and certainly not the other's.
    refused_session = manager.get_session(turned_away.session_id)
    assert refused_session.realtime_session_id is None
    assert refused_session.customer_id == "DEMO002"
    # And the caller already on the line is undisturbed.
    assert manager.get_session(live.session_id).customer_id == "DEMO001"


def slow_connector(delay=0.25):
    """A connector that takes its time, like a real provider handshake.

    The reservation design only matters while a connection is in flight. With
    an instant connector that window is too narrow to test, and a broken
    implementation would pass by luck.
    """

    async def connect(_context):
        await asyncio.sleep(delay)
        return FakeSession()

    return connect


def test_two_simultaneous_callers_at_the_ceiling_admit_exactly_one(manager):
    """The mandated race: active=2, limit=3, two callers press Start together.

    One must be admitted and one refused, and used capacity must finish at 3.
    Never 4.
    """
    realtime = RealtimeManager(
        connect=slow_connector(), manager=manager, max_active=3
    )

    async def scenario():
        # Fill two of the three slots.
        for _ in range(2):
            await realtime.start(_session(manager).session_id)
        assert realtime.used_capacity() == 2

        third = _session(manager)
        fourth = _session(manager)
        return await asyncio.gather(
            realtime.start(third.session_id),
            realtime.start(fourth.session_id),
            return_exceptions=True,
        )

    outcomes = run(scenario())
    admitted = [o for o in outcomes if isinstance(o, RealtimeConnection)]
    refused = [o for o in outcomes if isinstance(o, RealtimeSessionError)]

    assert len(admitted) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert refused[0].reason == Reason.REALTIME_AT_CAPACITY
    assert realtime.used_capacity() == 3
    assert realtime.active_count() == 3


def test_a_reservation_counts_against_capacity_while_it_connects(manager):
    """A slot being filled is not a free slot.

    Counting only established connections would let every caller who arrives
    during a multi-second handshake pass the check.
    """
    realtime = RealtimeManager(
        connect=slow_connector(0.4), manager=manager, max_active=1
    )
    first = _session(manager)
    second = _session(manager)

    async def scenario():
        opening = asyncio.create_task(realtime.start(first.session_id))
        await asyncio.sleep(0.1)  # first is mid-handshake, not yet connected

        assert realtime.active_count() == 0, "not established yet"
        assert realtime.used_capacity() == 1, "but the slot is taken"

        with pytest.raises(RealtimeSessionError) as error:
            await realtime.start(second.session_id)
        assert error.value.reason == Reason.REALTIME_AT_CAPACITY

        await opening
        return realtime.used_capacity()

    assert run(scenario()) == 1


def test_many_simultaneous_callers_never_exceed_the_ceiling(manager):
    """Twelve callers, three slots. Exactly three get in."""
    realtime = RealtimeManager(
        connect=slow_connector(0.2), manager=manager, max_active=3
    )
    sessions = [_session(manager, "DEMO001") for _ in range(12)]

    async def scenario():
        return await asyncio.gather(
            *(realtime.start(s.session_id) for s in sessions),
            return_exceptions=True,
        )

    outcomes = run(scenario())
    admitted = [o for o in outcomes if isinstance(o, RealtimeConnection)]
    at_capacity = [
        o
        for o in outcomes
        if isinstance(o, RealtimeSessionError)
        and o.reason == Reason.REALTIME_AT_CAPACITY
    ]

    assert len(admitted) == 3
    assert len(at_capacity) == 9
    assert realtime.used_capacity() == 3
    assert realtime.active_count() == 3


def test_a_failed_connection_gives_its_slot_straight_back(manager):
    """A reservation must never leak, or capacity drains away over a class."""
    failures = {"count": 2}

    async def flaky(_context):
        if failures["count"]:
            failures["count"] -= 1
            raise RuntimeError("provider said no")
        return FakeSession()

    realtime = RealtimeManager(connect=flaky, manager=manager, max_active=1)

    async def scenario():
        for _ in range(2):
            with pytest.raises(RealtimeSessionError):
                await realtime.start(_session(manager).session_id)
            # The slot is free again immediately, not held by a dead attempt.
            assert realtime.used_capacity() == 0

        # And the next caller can still get in.
        await realtime.start(_session(manager).session_id)
        return realtime.used_capacity()

    assert run(scenario()) == 1


def test_the_same_session_cannot_reserve_twice(manager):
    """Double-clicking Start must not consume two slots."""
    realtime = RealtimeManager(
        connect=slow_connector(0.3), manager=manager, max_active=5
    )
    session = _session(manager)

    async def scenario():
        both = await asyncio.gather(
            realtime.start(session.session_id),
            realtime.start(session.session_id),
            return_exceptions=True,
        )
        return both

    outcomes = run(scenario())
    refused = [o for o in outcomes if isinstance(o, RealtimeSessionError)]

    assert len(refused) == 1
    assert refused[0].reason == Reason.REALTIME_ALREADY_ACTIVE
    assert realtime.used_capacity() == 1


def test_capacity_starts_at_zero_on_a_clean_manager(manager):
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=3)

    assert realtime.active_count() == 0
    assert realtime.used_capacity() == 0
    assert realtime.at_capacity() is False


def test_connecting_is_not_serialised_behind_the_lock(manager):
    """Three callers connecting together must overlap, not queue.

    Holding the lock across a live handshake would make each caller wait for
    the one before. With a 0.4s connector, three serialised calls take 1.2s;
    three overlapping ones take about 0.4s.
    """
    realtime = RealtimeManager(
        connect=slow_connector(0.4), manager=manager, max_active=0
    )
    sessions = [_session(manager) for _ in range(3)]

    async def scenario():
        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(realtime.start(s.session_id) for s in sessions))
        return asyncio.get_running_loop().time() - started

    elapsed = run(scenario())

    assert realtime.active_count() == 3
    assert elapsed < 0.9, f"connections were serialised: {elapsed:.2f}s"


def test_capacity_frees_up_when_a_call_ends(manager):
    realtime = RealtimeManager(connect=connector(), manager=manager, max_active=1)
    first = _session(manager)
    second = _session(manager)
    run(realtime.start(first.session_id))

    run(realtime.close(first.session_id))
    run(realtime.start(second.session_id))

    assert realtime.is_active(second.session_id)


# === what the customer is told ==============================================


def test_the_busy_message_names_no_provider_and_no_numbers():
    message = MESSAGES[Reason.REALTIME_AT_CAPACITY]

    assert message == "Voice banking is temporarily busy. Please try again shortly."
    lowered = message.lower()
    for forbidden in (
        "openai", "rate", "limit", "quota", "concurren", "api", "token",
        "provider", "error", "429", "session", "capacity",
    ):
        assert forbidden not in lowered


def test_the_browser_is_told_it_is_busy_without_any_internal_detail(monkeypatch):
    from app.config import settings

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    import app.routers.call as call_router

    original = call_router.mint_client_secret
    call_router.mint_client_secret = fake_mint
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    client = TestClient(app)

    try:
        first = client.post("/api/call/start")
        assert first.status_code == 201

        second = client.post("/api/call/start")

        assert second.status_code == 503
        detail = second.json()["detail"]
        assert detail == "Voice banking is temporarily busy. Please try again shortly."
        assert "openai" not in detail.lower()

        # Fail closed: the refused attempt left no banking session behind.
        assert client.get("/api/call/active").json() == {
            "active_banking_sessions": 1,
            "active_browser_calls": 1,
        }
    finally:
        call_router.mint_client_secret = original


def test_a_refused_start_does_not_mint_a_credential(monkeypatch):
    """At capacity, no provider request is made at all."""
    from app.config import settings

    minted = []

    async def counting_mint(**_kwargs):
        minted.append(1)
        return {"value": "ek_test_not_real", "expires_at": 1}

    import app.routers.call as call_router

    original = call_router.mint_client_secret
    call_router.mint_client_secret = counting_mint
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    client = TestClient(app)

    try:
        client.post("/api/call/start")
        client.post("/api/call/start")

        assert len(minted) == 1
    finally:
        call_router.mint_client_secret = original


# === naming the provider condition ==========================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Error code: 429 - rate_limit_exceeded", RATE_LIMIT),
        ("insufficient_quota: exceeded your current quota", QUOTA_EXHAUSTED),
        ("credit_balance_exhausted", QUOTA_EXHAUSTED),
        ("too many concurrent sessions for this project", CONCURRENCY_LIMIT),
        ("max_active_sessions reached", CONCURRENCY_LIMIT),
        ("503 service_unavailable", PROVIDER_UNAVAILABLE),
        ("the server is overloaded", PROVIDER_UNAVAILABLE),
        ("ConnectionClosedError: no close frame received", NETWORK),
        ("Request timed out", TIMEOUT),
        ("something nobody has seen before", UNKNOWN),
    ],
)
def test_a_provider_condition_gets_its_own_name(text, expected):
    assert classify_provider_error(RuntimeError(text)) == expected


def test_quota_wins_over_rate_limit():
    """Both arrive as 429. Telling an operator to wait would be wrong."""
    error = RuntimeError("429 you have insufficient_quota for this request")

    assert classify_provider_error(error) == QUOTA_EXHAUSTED


def test_a_status_code_alone_is_enough():
    assert classify_status(429) == RATE_LIMIT
    assert classify_status(503) == PROVIDER_UNAVAILABLE
    assert classify_status(504) == TIMEOUT
    assert classify_status(200) is None
    assert classify_status(None) is None


def test_a_retry_after_hint_is_read_but_never_invented():
    assert retry_after_seconds("retry-after: 20") == 20.0
    assert retry_after_seconds('{"retry_after": 1.5}') == 1.5
    assert retry_after_seconds("no hint here") is None
    assert retry_after_seconds(None) is None


def test_an_exhausted_quota_is_never_retried():
    """Retrying an empty balance is a retry storm that fixes nothing."""
    assert QUOTA_EXHAUSTED not in RETRYABLE
    assert CONCURRENCY_LIMIT not in RETRYABLE
    assert RATE_LIMIT in RETRYABLE
    assert NETWORK in RETRYABLE


def test_no_provider_detail_reaches_the_customer_message():
    """Every reason maps to a sentence with no provider vocabulary in it."""
    for reason, message in MESSAGES.items():
        lowered = message.lower()
        assert "openai" not in lowered
        assert "bearer" not in lowered
        assert "sk-" not in lowered
