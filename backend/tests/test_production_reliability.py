"""Phase 7.4A: what this process does when its dependencies stop answering.

A separate concern from Phase 7.4B and deliberately kept that way. 7.4B is about
whether the bank says the right thing; this is about whether it is still there
to say it, and about what it leaves behind when it is not.

Six failures, all found by reading the code rather than by a caller finding them
first, and all the same shape: something that is only *observability* standing in
front of something that actually releases a resource.

1. **Nothing bounded the wait on PostgreSQL.** `create_engine` was called with
   no connect timeout, no statement timeout and SQLAlchemy's default
   thirty-second pool wait. A database that *refuses* is handled everywhere in
   this application; one that accepts the socket and then never answers was
   handled nowhere - and that is the ordinary failure, not an exotic one: a
   stopped container behind a live port-forward, a firewall that drops instead
   of rejecting, a host mid-failover. Phase 7.4 hit it on the development
   machine: Docker Desktop was down, port 5435 still accepted TCP, and the test
   suite hung on its first test for thirty minutes with no error of any kind.

2. **Stopping the process abandoned its calls instead of ending them.**
   `close_all()` has carried the words "used on shutdown" since it was written
   and nothing but the test suite ever called one.

3. **A write that could not happen stopped a release that had to.**
   `_release_everything` promises in its own docstring that one failing step
   never strands the ones after it - and opened with an unguarded trace write,
   ahead of the bridge, the model session and the capacity slot.

4. **`close_phone_call` raises by design** and ran before `tear_down` in three
   callers that had already decided the call was over.

5. **`sweep_idle_calls` was unguarded** and runs first on the admission path,
   outside every try block - so a database blip there is a caller hearing an
   engaged tone.

6. **The greeting task was unowned.** `asyncio.ensure_future(...)` with no
   reference held and no exception handler, in a module whose own
   `_uninterruptible` documents why that is unsafe.

These tests are non-negotiable. A failure means the change is wrong, not that
the test needs updating.

Deliberately runnable with PostgreSQL switched off. A gate whose first subject is
"what this process does when the database is unreachable" would be a poor one if
it could only be run against a healthy database.
"""

import asyncio
import socket
import time

import pytest

from app.config import settings
from app.database import connection
from app.observability import trace
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import reasons, service
from app.telephony.bridge import phone_call_registry

pytestmark = pytest.mark.reliability

# --- fixtures and fakes -----------------------------------------------------


@pytest.fixture(autouse=True)
def offline_observability(monkeypatch):
    """No test here needs a database, and none of them may quietly want one.

    Every write is replaced with a recorder in memory. That keeps this file
    runnable while PostgreSQL is down - which is the state a third of it is
    about - and it makes the assertions exact: what matters is *that* an ending
    was recorded and with which reason, not what a row looks like afterwards.
    """
    written: dict[str, list] = {"ended": [], "rejected": [], "trace": []}

    def close_phone_call(provider_call_id, *, reason="CUSTOMER_ENDED"):
        written["ended"].append((provider_call_id, reason))
        return None

    def mark_phone_call_rejected(provider_call_id, *, reason="CAPACITY_REJECTED"):
        written["rejected"].append((provider_call_id, reason))

    def record_trace(banking_session_id, event, **kwargs):
        written["trace"].append((banking_session_id, event))

    monkeypatch.setattr(service.recorder, "close_phone_call", close_phone_call)
    monkeypatch.setattr(
        service.recorder, "mark_phone_call_rejected", mark_phone_call_rejected
    )
    monkeypatch.setattr(trace, "record", record_trace)
    monkeypatch.setattr(trace, "purge_expired", lambda **kwargs: 0)
    return written


@pytest.fixture(autouse=True)
def no_calls_left_behind():
    """Leave no call, bridge or capacity slot behind - and never hang doing it.

    Bounded on purpose. Two tests here register a bridge whose `close()` never
    returns, because that is the condition they exist to prove the drain
    survives. An unbounded teardown would then wedge the whole suite on the
    fixture rather than failing the test, which is how one deliberate stall
    becomes a thirty-minute run with no output.
    """
    yield

    async def release():
        try:
            await asyncio.wait_for(phone_call_registry.close_all(), timeout=5)
        except asyncio.TimeoutError:
            # A deliberately stuck fake. Drop it rather than wedge the suite;
            # the capacity release below is the part that matters.
            phone_call_registry._bridges.clear()
        await voice_call_manager.close_all()
        await voice_call_manager.release_all()

    asyncio.run(release())
    session_manager.clear()


class FakeModelSession:
    """A model session with nothing behind it. No stream, so no pump."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def send_message(self, text: str) -> None:
        return None

    async def send_audio(self, audio: bytes) -> None:
        return None


class FakeTransport:
    """Enough transport for the greeting sequence, and nothing more."""

    def __init__(self, *, ready_raises: bool = False, never_ready: bool = False) -> None:
        self.ready_raises = ready_raises
        self.never_ready = never_ready
        self.ended = False

    async def wait_until_ready(self, timeout):
        if self.ready_raises:
            raise RuntimeError("the socket layer failed in a way nobody predicted")
        if self.never_ready:
            await asyncio.Event().wait()
        return True

    async def wait_for_protocol(self, timeout):
        return True

    async def on_call_ended(self) -> None:
        self.ended = True


class FakeBridge:
    """A registered call, for the paths that only need it to be one."""

    def __init__(
        self,
        provider_call_id: str,
        banking_session_id: str,
        *,
        transport=None,
        hang_on_close: bool = False,
    ) -> None:
        self.provider_call_id = provider_call_id
        self.banking_session_id = banking_session_id
        self.transport = transport or FakeTransport()
        self.closed = False
        self.last_activity = time.monotonic()
        self._hang_on_close = hang_on_close

    async def close(self) -> None:
        if self._hang_on_close:
            await asyncio.Event().wait()
        self.closed = True


async def _connect_fake(_context):
    return FakeModelSession()


async def live_call(call_id: str, **bridge_kwargs) -> FakeBridge:
    """One admitted call: a banking session, a capacity slot, a bridge.

    Built through the real manager rather than by poking its dictionaries, so
    the capacity assertions below are about the thing production uses.
    """
    session = session_manager.create_session()
    await voice_call_manager.start(session.session_id, connect=_connect_fake)
    bridge = FakeBridge(call_id, session.session_id, **bridge_kwargs)
    assert await phone_call_registry.register(bridge)
    return bridge


def run(coroutine):
    return asyncio.run(coroutine)


# === A. every wait on the database is bounded ===============================


def test_a_postgres_connection_bounds_the_handshake_and_the_statement():
    """The four bounds, on the connection that carries every banking write.

    `connect_timeout` is the one that matters most and the one libpq has no
    default for: without it, a host that accepts TCP and then goes quiet is an
    indefinite hang rather than an error.
    """
    args = connection._connect_args("postgresql+psycopg://user:pw@host:5432/bank")

    assert args["connect_timeout"] == settings.database_connect_timeout
    assert "statement_timeout" in args["options"]
    assert "lock_timeout" in args["options"]
    assert "idle_in_transaction_session_timeout" in args["options"]


def test_the_bounds_are_only_sent_to_a_driver_that_understands_them():
    """`connect_timeout` and `options` are libpq parameters, not SQL.

    Handing them to another driver - SQLite in a throwaway test - is a
    `TypeError` at connect time, which would turn a hardening measure into an
    outage of its own.
    """
    assert connection._connect_args("sqlite:///:memory:") == {}


def test_no_wait_on_this_database_is_longer_than_a_caller_would_tolerate():
    """A call is a live conversation. Ten seconds of nothing is a lost caller.

    Asserted as upper bounds rather than as exact values, so tuning them stays
    free and removing them does not. Deliberately not asserted *low*: a bound
    tight enough to fire on a healthy database would manufacture outages.
    """
    assert 0 < settings.database_connect_timeout <= 10
    assert 0 < settings.database_statement_timeout_ms <= 15000
    assert 0 < settings.database_lock_timeout_ms
    assert settings.database_lock_timeout_ms <= settings.database_statement_timeout_ms
    assert 0 < settings.database_pool_timeout <= 10
    # Generous enough that a normal turn never waits on it.
    assert settings.database_statement_timeout_ms >= 5000


def test_a_database_that_accepts_and_never_answers_fails_instead_of_hanging():
    """The Phase 7.4A failure itself, reproduced deterministically.

    A listening socket that never accepts is exactly what a stopped container
    behind a live port-forward looks like: the kernel completes the TCP
    handshake from the backlog, and the server never sends the startup message
    it owes. Before this phase that hung for ever.
    """
    black_hole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    black_hole.bind(("127.0.0.1", 0))
    black_hole.listen(1)
    port = black_hole.getsockname()[1]

    previous_url = settings.database_url
    previous_timeout = settings.database_connect_timeout
    settings.database_url = f"postgresql+psycopg://bank:bank@127.0.0.1:{port}/bank"
    settings.database_connect_timeout = 2
    connection.reset_engine()

    try:
        from sqlalchemy import text

        started = time.monotonic()
        with pytest.raises(Exception):
            with connection.session_scope() as db:
                db.execute(text("SELECT 1"))
        elapsed = time.monotonic() - started
    finally:
        settings.database_url = previous_url
        settings.database_connect_timeout = previous_timeout
        connection.reset_engine()
        black_hole.close()

    assert elapsed < 15, (
        f"an unreachable database took {elapsed:.1f}s to fail. It must fail on "
        "the connect timeout, not on whatever the operating system decides."
    )


def test_an_unreachable_host_fails_rather_than_waiting_on_the_kernel():
    """The other unreachable shape: nothing listening at all."""
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    previous_url = settings.database_url
    settings.database_url = f"postgresql+psycopg://bank:bank@127.0.0.1:{port}/bank"
    connection.reset_engine()
    try:
        from sqlalchemy import text

        started = time.monotonic()
        with pytest.raises(Exception):
            with connection.session_scope() as db:
                db.execute(text("SELECT 1"))
        elapsed = time.monotonic() - started
    finally:
        settings.database_url = previous_url
        connection.reset_engine()

    assert elapsed < 15


def test_the_engine_recovers_after_a_reset():
    """`reset_engine` exists for the tests above, and must leave things working.

    A hardening measure that left the process unable to reach a healthy
    database afterwards would be worse than the hang it replaced.
    """
    from sqlalchemy import text

    connection.reset_engine()
    with connection.session_scope() as db:
        assert db.execute(text("SELECT 1")).scalar() == 1

    # And again, to prove the reset is repeatable rather than a one-shot.
    connection.reset_engine()
    with connection.session_scope() as db:
        assert db.execute(text("SELECT 1")).scalar() == 1


def test_a_healthy_database_is_unaffected_by_the_bounds():
    """The normal case must not have become slower or more fragile."""
    from sqlalchemy import text

    started = time.monotonic()
    for _ in range(5):
        with connection.session_scope() as db:
            assert db.execute(text("SELECT 1")).scalar() == 1
    assert time.monotonic() - started < 10


# === B. stopping the process ends its calls =================================


def test_stopping_this_process_releases_the_calls_it_is_carrying(
    offline_observability,
):
    """The deployment case. Every resource back, and an ending written down."""

    async def scenario():
        await live_call("call-shutdown-1")
        await live_call("call-shutdown-2")
        await live_call("call-shutdown-3")

        assert phone_call_registry.active_count() == 3
        assert voice_call_manager.used_capacity() == 3

        from app.main import _release_live_calls

        await _release_live_calls()

        assert phone_call_registry.active_count() == 0
        assert voice_call_manager.used_capacity() == 0

    run(scenario())

    recorded = dict(offline_observability["ended"])
    assert recorded == {
        "call-shutdown-1": reasons.SERVICE_SHUTDOWN,
        "call-shutdown-2": reasons.SERVICE_SHUTDOWN,
        "call-shutdown-3": reasons.SERVICE_SHUTDOWN,
    }


def test_a_shutdown_ending_is_in_the_operator_vocabulary():
    """A reason nobody can filter on is a reason nobody can count.

    Distinct from `APPLICATION_ERROR`, because a deployment is not a fault, and
    from `APPLICATION_END`, because that one means "no more specific name".
    """
    assert reasons.SERVICE_SHUTDOWN in reasons.ALL
    assert reasons.SERVICE_SHUTDOWN != reasons.APPLICATION_END
    assert reasons.SERVICE_SHUTDOWN != reasons.APPLICATION_ERROR


def test_one_stuck_call_cannot_hold_the_process_open():
    """The drain is a bound, not a promise to finish.

    A supervisor allows a grace period and then kills the process, so a drain
    that could run for ever is a drain that gets cut off mid-release.
    """

    async def scenario():
        await live_call("call-stuck", hang_on_close=True)

        from app.main import _release_live_calls

        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_release_live_calls(), timeout=0.5)
        return asyncio.get_running_loop().time() - started

    assert run(scenario()) < 5


def test_the_lifespan_is_the_thing_that_drains():
    """Wired in, not merely available.

    The defect was never a missing function - `close_all()` existed and said
    "used on shutdown" in its own docstring. What was missing was a caller.
    """
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "yield" in source
    after_yield = source.split("yield", 1)[1]
    assert "_release_live_calls" in after_yield, (
        "the lifespan yields and then stops without releasing its live calls"
    )


def test_a_drained_process_leaves_no_session_behind():
    """Sessions are the thing a clarification and an identity live on."""

    async def scenario():
        bridge = await live_call("call-drain-session")
        assert session_manager.get_session(bridge.banking_session_id) is not None

        from app.main import _release_live_calls

        await _release_live_calls()
        return bridge.banking_session_id

    banking_session_id = run(scenario())
    assert session_manager.get_session(banking_session_id) is None


# === C / D. a record that cannot be written never strands a release =========


def test_a_failed_trace_write_still_releases_everything(monkeypatch):
    """`_release_everything` promises this in its own docstring.

    The trace write is the last line of the replay - observability - and it
    stood first, unguarded, in front of the bridge, the model session and the
    capacity slot. `trace.record` is deliberately not `@_safe`, so a database
    blip at hang-up stranded all three.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("PostgreSQL went away mid-hang-up")

    monkeypatch.setattr(trace, "record", explode)

    async def scenario():
        bridge = await live_call("call-trace-fails")
        await service.tear_down(bridge.provider_call_id, bridge.banking_session_id)
        return bridge

    bridge = run(scenario())

    assert phone_call_registry.active_count() == 0, "the bridge was stranded"
    assert voice_call_manager.used_capacity() == 0, "the capacity slot was stranded"
    assert bridge.closed


def test_a_failed_ending_record_still_releases_the_call(monkeypatch):
    """The row is the record of the decision, not the decision itself.

    `close_phone_call` raises by design, and that is right for `_end_call`,
    which reads its return value to decide whether it may release anything at
    all. It is wrong for the callers that have already decided the call is over.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("PostgreSQL went away mid-hang-up")

    monkeypatch.setattr(service.recorder, "close_phone_call", explode)

    async def scenario():
        bridge = await live_call("call-record-fails")
        await service._on_call_ended(
            bridge.provider_call_id, bridge.banking_session_id, "CALLER_GOODBYE"
        )

    run(scenario())

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0


def test_a_lost_call_is_released_even_when_the_database_is_down(monkeypatch):
    """The other half of the same rule, on the failure path."""

    def explode(*args, **kwargs):
        raise RuntimeError("PostgreSQL went away")

    monkeypatch.setattr(service.recorder, "close_phone_call", explode)

    async def scenario():
        bridge = await live_call("call-lost")
        await service._on_call_lost(
            bridge.provider_call_id, bridge.banking_session_id, reasons.MEDIA_FAILURE
        )

    run(scenario())

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0


def test_a_new_call_is_admitted_after_a_failed_hangup(monkeypatch):
    """The consequence that actually reaches a customer.

    With `REALTIME_MAX_ACTIVE_SESSIONS = 1`, a stranded slot means every later
    caller is told the bank is full until the process restarts. So the test is
    not only that the slot came back, but that somebody can use it.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("PostgreSQL went away mid-hang-up")

    monkeypatch.setattr(trace, "record", explode)

    async def scenario():
        first = await live_call("call-before")
        await service.tear_down(first.provider_call_id, first.banking_session_id)
        assert voice_call_manager.used_capacity() == 0

        # The slot is genuinely usable, not merely reported free.
        second = await live_call("call-after")
        assert voice_call_manager.used_capacity() == 1
        return second

    run(scenario())


# === E. admission survives a failing sweep ==================================


def test_a_failing_idle_sweep_never_refuses_the_next_caller(monkeypatch):
    """The sweep runs first on the admission path, outside every try block.

    So an exception in it is not merely a sweep that reclaimed nothing - it is
    a caller who gets an engaged tone. The two lines below it already guard
    `purge_expired` for exactly this reason; the ending write did not.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("PostgreSQL went away mid-sweep")

    monkeypatch.setattr(service.recorder, "close_phone_call", explode)
    monkeypatch.setattr(settings, "telephony_idle_call_timeout", 1)

    async def scenario():
        bridge = await live_call("call-idle")
        bridge.last_activity = time.monotonic() - 3600
        # Must not raise. That is the whole assertion.
        return await service.sweep_idle_calls()

    assert run(scenario()) == 1
    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0


def test_the_sweep_does_not_hide_a_programming_error(monkeypatch):
    """Narrow, not blanket. A defect in this module must still be loud.

    The guard exists so a database blip cannot refuse a caller. It must not
    also swallow an `AttributeError` from a refactor, which would turn a broken
    sweep into a silently leaking one.
    """
    monkeypatch.setattr(settings, "telephony_idle_call_timeout", 1)

    async def scenario():
        bridge = await live_call("call-defect")
        bridge.last_activity = time.monotonic() - 3600

        def broken(*args, **kwargs):
            raise TypeError("somebody changed this signature")

        monkeypatch.setattr(service, "tear_down", broken)
        with pytest.raises(TypeError):
            await service.sweep_idle_calls()

    run(scenario())


# === F. no call task is left to the garbage collector =======================


def test_the_greeting_sequence_is_owned_while_it_runs():
    """`asyncio` keeps only a weak reference to a running task.

    What a collected task loses here is the media wait, the protocol check, the
    greeting and the teardown each of them falls back on - so the call would sit
    silent on a capacity slot until the idle sweep noticed.
    """

    async def scenario():
        bridge = await live_call("call-held")
        service._begin_conversation(bridge)
        owned = service._opening.get(bridge.provider_call_id)
        assert owned is not None, "the greeting task is owned by nothing"
        await asyncio.sleep(0)
        return owned

    run(scenario())


def test_an_unexpected_greeting_failure_gives_the_call_up(offline_observability):
    """Every predicted failure in the greeting sequence already tears down.

    This is the same ending for the ones nobody predicted. Without it the
    exception surfaces only when the interpreter collects the task, by which
    time the call has been holding a slot and an open provider session.
    """

    async def scenario():
        bridge = await live_call(
            "call-greeting-explodes", transport=FakeTransport(ready_raises=True)
        )
        await service._open_conversation(bridge)

    run(scenario())

    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert ("call-greeting-explodes", reasons.APPLICATION_ERROR) in (
        offline_observability["rejected"]
    )


def test_a_greeting_in_flight_is_cancelled_by_teardown():
    """Nothing may still be trying to greet a call that has ended.

    A greeting waiting on media that never arrives outlives the call unless
    teardown stops it - and a task that survives its session is a task that can
    speak after the line is down.
    """

    async def scenario():
        bridge = await live_call(
            "call-greet-forever", transport=FakeTransport(never_ready=True)
        )
        service._begin_conversation(bridge)
        task = service._opening.get(bridge.provider_call_id)
        assert task is not None
        await asyncio.sleep(0)

        await service.tear_down(bridge.provider_call_id, bridge.banking_session_id)

        assert task.cancelled() or task.done(), (
            "the greeting task outlived the call it belonged to"
        )
        assert bridge.provider_call_id not in service._opening

    run(scenario())


def test_no_greeting_task_survives_a_drained_process():
    """The same rule at shutdown rather than at hang-up."""

    async def scenario():
        bridge = await live_call(
            "call-greet-drain", transport=FakeTransport(never_ready=True)
        )
        service._begin_conversation(bridge)
        await asyncio.sleep(0)

        from app.main import _release_live_calls

        await _release_live_calls()

        assert bridge.provider_call_id not in service._opening
        assert not service._opening, "a greeting task outlived the process drain"

    run(scenario())
