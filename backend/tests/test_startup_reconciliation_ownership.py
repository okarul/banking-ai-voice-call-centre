"""Phase 7.4A.1: whose calls a starting process is allowed to close.

`recorder.reconcile_active_sessions()` runs in lifespan startup and repairs rows
left `ACTIVE` by a process that died without ending its calls. That repair is
necessary - after a hard crash nothing else will ever close those rows - and its
query is:

    select(AgentSession).where(
        AgentSession.ended_at.is_(None), AgentSession.status != REJECTED
    )

which names no owner. "Anything still open at that moment belongs to a process
that no longer exists", says the docstring. That is true of the deployment this
application documents - one process - and false of the one a rolling restart
creates, where a new process starts while the old one is still carrying calls.

Two consequences, and the second is the expensive one:

1. **An operator is told the wrong thing.** A live call is stamped
   `FORCED_CLEANUP`, which means "left behind by a crash", while the caller is
   still talking.

2. **The call leaks.** `_end_call` reads the return of `close_phone_call` to
   decide whether it may release anything - and that update moves the row
   `WHERE ended_at IS NULL`. Once a foreign startup has closed the row, the
   owning process gets `None` back, reports `ALREADY_ENDED`, and **never calls
   `tear_down`**. The bridge, the capacity slot and the provider session stay
   held. On a deployment with `REALTIME_MAX_ACTIVE_SESSIONS = 1` that is one
   rolling restart away from every later caller being told the bank is full.

The second is not really a reconciliation bug at all. It is that release of
*in-memory* resources is gated on a *database* state transition, and the
database is shared while the resources are not. Both halves are pinned here.

These tests describe two process lifetimes without needing two processes: a
"startup" is `reconcile_active_sessions()` being called, which is exactly what
the lifespan does and all it does.
"""

import asyncio
import time

import pytest
from sqlalchemy import delete, select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.observability import recorder
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import reasons, service
from app.telephony.bridge import phone_call_registry
from app.telephony.channels import Channel
from app.telephony.schemas import InboundCallEvent
from datetime import datetime, timezone


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield

    async def release():
        try:
            await asyncio.wait_for(phone_call_registry.close_all(), timeout=5)
        except asyncio.TimeoutError:
            phone_call_registry._bridges.clear()
        await voice_call_manager.close_all()
        await voice_call_manager.release_all()

    run(release())
    session_manager.clear()
    # A claim is process-local and deliberately outlives a call, so it also
    # outlives a test unless something drops it.
    from app import process_ownership

    process_ownership.forget_all_terminal_pending()
    wipe()


class FakeModelSession:
    async def close(self):
        return None

    async def send_message(self, text):
        return None

    async def send_audio(self, audio):
        return None


class FakeTransport:
    def __init__(self):
        self.ended = False

    async def wait_until_ready(self, timeout):
        return True

    async def wait_for_protocol(self, timeout):
        return True

    async def on_call_ended(self):
        self.ended = True


class FakeBridge:
    def __init__(self, provider_call_id, banking_session_id):
        self.provider_call_id = provider_call_id
        self.banking_session_id = banking_session_id
        self.transport = FakeTransport()
        self.closed = False
        self.last_activity = time.monotonic()

    async def close(self):
        self.closed = True


async def _connect_fake(_context):
    return FakeModelSession()


async def process_a_answers(call_id: str) -> FakeBridge:
    """One live call, owned by this process: row, bridge, capacity slot.

    Claimed through the real recorder so the row is shaped exactly as a live
    call's row is - `ACTIVE`, `ended_at IS NULL`, channel PHONE.
    """
    session = session_manager.create_session()
    recorder.claim_phone_call(
        session.session_id,
        provider_call_id=call_id,
        provider_event_id=f"evt-{call_id}",
    )
    await voice_call_manager.start(session.session_id, connect=_connect_fake)
    bridge = FakeBridge(call_id, session.session_id)
    assert await phone_call_registry.register(bridge)
    return bridge


def process_b_starts() -> int:
    """Another application context's startup: what the lifespan does, and all it does.

    Drives `process_ownership.reconcile_on_startup` rather than the recorder
    directly, because a test that reached past it would be asserting against a
    repair nobody actually performs.
    """
    from app import process_ownership

    return process_ownership.reconcile_on_startup()


def row_for(call_id: str):
    with session_scope() as db:
        return db.scalars(
            select(AgentSession).where(AgentSession.provider_call_id == call_id)
        ).first()


def ended_event(call_id: str) -> InboundCallEvent:
    return InboundCallEvent(
        provider="TEST",
        provider_event_id=f"evt-{call_id}-ended",
        provider_call_id=call_id,
        event_type="ended",
        event_timestamp=datetime.now(timezone.utc),
    )


def orphan_row(call_id: str) -> None:
    """A row from a process generation that is genuinely gone.

    Left `ACTIVE` with no `ended_at` and - the part that makes it an orphan -
    nothing in this process holding a bridge or a slot for it. That is what a
    hard crash leaves behind, and repairing it is what reconciliation is for.
    """
    with session_scope() as db:
        db.add(
            AgentSession(
                # `agent_session_id` is String(20); a long call id truncates
                # rather than raising a DataError in the middle of a test that
                # is about something else entirely.
                agent_session_id=f"AGENT-{call_id}"[:20],
                banking_session_id=f"SESSION-{call_id}"[:36],
                channel=Channel.PHONE.value,
                provider_call_id=call_id,
                provider_event_id=f"evt-{call_id}",
                status="ACTIVE",
                started_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )


# === 1. a live call must survive another process starting ===================


def test_a_starting_process_does_not_close_a_live_call():
    """The rolling-restart invariant, stated at its narrowest.

    Process A is carrying a call. Process B starts. B has no way to know
    anything about A's call except that its row is open - and an open row is
    not evidence of a dead owner.
    """

    async def scenario():
        bridge = await process_a_answers("call-live-a")
        assert row_for("call-live-a").ended_at is None

        process_b_starts()

        row = row_for("call-live-a")
        assert row.ended_at is None, (
            "a starting process closed a call that another process is still "
            f"carrying; it was stamped {row.disconnect_reason}"
        )
        assert row.status == "ACTIVE"
        return bridge

    run(scenario())


def test_the_owner_can_still_end_its_own_call_after_another_process_starts():
    """The leak, and the reason this is HIGH rather than cosmetic.

    `_end_call` reads `close_phone_call`'s return to decide whether it may
    release anything. A foreign startup having closed the row makes that return
    `None`, so the owning process reports `ALREADY_ENDED` and tears nothing
    down - stranding a bridge, a capacity slot and a provider session that it,
    not the database, is holding.
    """

    async def scenario():
        bridge = await process_a_answers("call-owner-ends")
        assert voice_call_manager.used_capacity() == 1

        process_b_starts()

        # A's own provider `ended` webhook arrives, as it always would.
        result = await service.handle_event(ended_event("call-owner-ends"))

        assert phone_call_registry.active_count() == 0, (
            "the bridge was stranded: the owning process declined to release "
            f"its own call because the row was already closed ({result.outcome})"
        )
        assert voice_call_manager.used_capacity() == 0, (
            "the capacity slot was stranded"
        )
        assert bridge.closed is True

    run(scenario())


def test_a_second_caller_can_be_admitted_after_a_rolling_restart():
    """What the leak costs a customer, at capacity 1."""

    async def scenario():
        await process_a_answers("call-first")
        process_b_starts()
        await service.handle_event(ended_event("call-first"))

        assert voice_call_manager.used_capacity() == 0
        # And the slot is genuinely usable, not merely reported free.
        await process_a_answers("call-second")
        assert voice_call_manager.used_capacity() == 1

    run(scenario())


# === 2. a genuine orphan must still be repaired =============================


def test_a_genuine_orphan_is_still_reconciled():
    """The case reconciliation exists for, and it must not be lost.

    A row left open by a process that is gone, with nothing in this process
    holding anything for it. Nobody else will ever close it.
    """
    orphan_row("call-orphan")
    assert row_for("call-orphan").ended_at is None

    closed = process_b_starts()

    row = row_for("call-orphan")
    assert closed >= 1
    assert row.ended_at is not None, "a genuine orphan was left open for ever"
    assert row.disconnect_reason == "FORCED_CLEANUP"
    assert row.status == "DISCONNECTED"


def test_an_orphan_and_a_live_call_are_told_apart():
    """Both at once, which is what a rolling restart actually looks like.

    The previous generation crashed leaving a row behind, *and* a live process
    is carrying a call. One must be repaired and the other left alone.
    """

    async def scenario():
        await process_a_answers("call-live-b")
        orphan_row("call-orphan-b")

        process_b_starts()

        live = row_for("call-live-b")
        orphan = row_for("call-orphan-b")

        assert live.ended_at is None, (
            f"the live call was force-closed as {live.disconnect_reason}"
        )
        assert orphan.ended_at is not None, "the orphan was not repaired"
        assert orphan.disconnect_reason == "FORCED_CLEANUP"

    run(scenario())


# === 3. reconciliation must not be the thing that releases resources ========


def test_reconciliation_never_releases_another_process_resources():
    """A row is not a resource. Closing one must free nothing in memory.

    Stated separately because it is the invariant that makes the two halves of
    this file independent: even a reconciliation that wrongly closes a row must
    not be able to take a bridge or a slot away from the process that owns it.
    """

    async def scenario():
        await process_a_answers("call-resources")
        before_bridges = phone_call_registry.active_count()
        before_capacity = voice_call_manager.used_capacity()

        process_b_starts()

        assert phone_call_registry.active_count() == before_bridges
        assert voice_call_manager.used_capacity() == before_capacity

    run(scenario())


# === 4. the terminal write that failed, and the orphan it invented ==========
#
# Phase 7.4A.1 found the 8-thread duplicate race non-deterministic: the same
# live call was sometimes stamped FORCED_CLEANUP even with ownership scoping in
# place. Instrumentation showed the closing reconcile running with an *empty*
# owned set while the row was still open, and adding a single database read to
# the probe made the race disappear - so it was timing, and timing is not a
# diagnosis.
#
# This is the sequence, driven deterministically instead of raced.


def test_a_failed_terminal_write_leaves_a_row_that_looks_like_an_orphan(monkeypatch):
    """The whole defect, in one sequence and with no timing in it.

    `_record_ending` swallows a failed `close_phone_call` on purpose: a row that
    cannot be written must never stop a call being released, which is the Phase
    7.4A fix and must stay. But the swallow loses the fact that the row is still
    *ours and unfinished* - and `tear_down` then removes the session, the bridge
    and the slot, which is all the evidence of ownership there was.

    What is left is a row nobody claims: open, with no live resources behind it,
    which is exactly the shape of a crash orphan. The next startup in the same
    process repairs it as one, and an operator is told a call that ran perfectly
    well was abandoned by a dead process.
    """
    async def scenario():
        bridge = await process_a_answers("call-terminal-fails")
        session_id = bridge.banking_session_id

        # 2/3. The terminal write fails for a bounded, transient reason - a lock
        #      timeout under contention is the one Phase 7.4A introduced.
        def lock_timeout(*args, **kwargs):
            raise RuntimeError("canceling statement due to lock timeout")

        monkeypatch.setattr(recorder, "close_phone_call", lock_timeout)

        # 4. The drain runs. Local teardown must proceed regardless.
        await service.release_live_calls()

        return session_id

    session_id = run(scenario())

    # 4. Local resources are gone, which is correct and must not regress.
    assert phone_call_registry.active_count() == 0
    assert voice_call_manager.used_capacity() == 0
    assert session_manager.get_session(session_id) is None

    # 5. The row was never closed, because the write failed.
    row = row_for("call-terminal-fails")
    assert row.ended_at is None, "the harness did not reproduce a failed write"

    # 6. But the row is still recognisably ours.
    #
    #    This assertion is the one Phase 7.4A.1 inverted. It originally read
    #    `session_id not in locally_owned_banking_sessions()` - asserting the
    #    defect's mechanism, which was the right thing to pin while proving the
    #    hypothesis and the wrong thing to keep afterwards. The teardown above
    #    has destroyed the session, the bridge and the slot, exactly as it must;
    #    what survives is a claim on the unfinished row and nothing else.
    from app import process_ownership

    assert session_id in process_ownership.pending_terminal_sessions(), (
        "the unfinished terminal row was not claimed, so nothing remembers it "
        "is ours to finish"
    )
    assert session_id in process_ownership.locally_owned_banking_sessions(), (
        "the claim exists but startup reconciliation cannot see it"
    )
    assert (
        process_ownership.pending_terminal_reason(session_id)
        == reasons.SERVICE_SHUTDOWN
    ), "the claim lost the reason it intends to record"

    # 7. So the next startup in this same process leaves it alone.
    monkeypatch.undo()
    process_b_starts()

    row = row_for("call-terminal-fails")
    assert row.disconnect_reason != "FORCED_CLEANUP", (
        "a call this process ran and released was recorded as abandoned by a "
        "dead process, because the terminal write failed and nothing remembered "
        "that the row was still ours"
    )
    assert row.ended_at is None, (
        "the row was closed by a reconciliation that should have skipped it"
    )


# === 5. the bounded retry, and what each of its outcomes means =============


def test_the_retry_records_the_intended_reason_and_drops_the_claim(monkeypatch):
    """F3. The ordinary transient: lost once, written on the second attempt.

    A lock lost under contention, a connection recycled underneath a write.
    One retry is what tells that apart from a database that is actually down,
    and it happens *after* the release so no caller waits for it.
    """
    attempts = {"n": 0}
    real_close = recorder.close_phone_call

    def fail_once(provider_call_id, *, reason="CUSTOMER_ENDED"):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("canceling statement due to lock timeout")
        return real_close(provider_call_id, reason=reason)

    async def scenario():
        bridge = await process_a_answers("call-retry-wins")
        monkeypatch.setattr(recorder, "close_phone_call", fail_once)
        await service.release_live_calls()
        return bridge.banking_session_id

    session_id = run(scenario())

    from app import process_ownership

    assert attempts["n"] == 2, "expected exactly one retry, not %s" % attempts["n"]
    assert session_id not in process_ownership.pending_terminal_sessions(), (
        "the claim outlived a terminal write that succeeded"
    )

    row = row_for("call-retry-wins")
    assert row.ended_at is not None
    assert row.disconnect_reason == reasons.SERVICE_SHUTDOWN
    assert voice_call_manager.used_capacity() == 0


def test_a_retry_that_finds_the_row_already_terminal_keeps_the_first_reason(
    monkeypatch,
):
    """F4. Somebody else finished it. Drop the claim, change nothing.

    `close_phone_call` moves the row `WHERE ended_at IS NULL`, so it cannot
    overwrite a reason another path already wrote - the retry finding the row
    closed is therefore both safe and final, and the claim must go.
    """
    real_close = recorder.close_phone_call
    state = {"n": 0}

    def fail_then_let_somebody_else_win(provider_call_id, *, reason="CUSTOMER_ENDED"):
        state["n"] += 1
        if state["n"] == 1:
            # The first attempt fails, and meanwhile the media route records
            # the caller's hang-up - which is a legitimate, different reason.
            real_close(provider_call_id, reason=reasons.CALLER_HANGUP)
            raise RuntimeError("canceling statement due to lock timeout")
        return real_close(provider_call_id, reason=reason)

    async def scenario():
        bridge = await process_a_answers("call-retry-already")
        monkeypatch.setattr(
            recorder, "close_phone_call", fail_then_let_somebody_else_win
        )
        await service.release_live_calls()
        return bridge.banking_session_id

    session_id = run(scenario())

    from app import process_ownership

    assert session_id not in process_ownership.pending_terminal_sessions(), (
        "the claim outlived a row that was already terminal"
    )

    row = row_for("call-retry-already")
    assert row.disconnect_reason == reasons.CALLER_HANGUP, (
        "the retry overwrote a terminal reason another path had legitimately "
        "recorded"
    )
    assert voice_call_manager.used_capacity() == 0


def test_a_duplicate_ended_event_after_a_failed_write_changes_nothing(monkeypatch):
    """F7. Idempotent, including on the path that had to give up.

    The row is open and claimed, the resources are gone. A second provider
    `ended` event must not release anything twice, and must not drive the
    capacity counter below zero.
    """

    def always_fails(*args, **kwargs):
        raise RuntimeError("canceling statement due to lock timeout")

    async def scenario():
        await process_a_answers("call-dup-after-fail")
        monkeypatch.setattr(recorder, "close_phone_call", always_fails)
        await service.release_live_calls()
        monkeypatch.undo()

        # The provider tells us twice, after the fact.
        first = await service.handle_event(ended_event("call-dup-after-fail"))
        second = await service.handle_event(ended_event("call-dup-after-fail"))
        return first, second

    first, second = run(scenario())

    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    # Whatever each execution reported, neither released anything twice.
    assert first.outcome in (service.Outcome.ENDED, service.Outcome.ALREADY_ENDED)
    assert second.outcome is service.Outcome.ALREADY_ENDED


def test_a_new_caller_is_admitted_after_a_failed_terminal_write(monkeypatch):
    """F8. What the whole fix is for: the next caller gets through."""

    def always_fails(*args, **kwargs):
        raise RuntimeError("canceling statement due to lock timeout")

    async def scenario():
        await process_a_answers("call-before-fail")
        monkeypatch.setattr(recorder, "close_phone_call", always_fails)
        await service.release_live_calls()
        monkeypatch.undo()

        assert voice_call_manager.used_capacity() == 0

        # And the slot is genuinely usable, not merely reported free.
        await process_a_answers("call-after-fail")
        assert voice_call_manager.used_capacity() == 1

    run(scenario())


def test_a_claimed_row_does_not_block_repair_of_a_real_orphan(monkeypatch):
    """The claim is narrow: one session, not an amnesty for every open row."""

    def always_fails(*args, **kwargs):
        raise RuntimeError("canceling statement due to lock timeout")

    async def scenario():
        bridge = await process_a_answers("call-claimed")
        orphan_row("call-real-orphan")
        monkeypatch.setattr(recorder, "close_phone_call", always_fails)
        await service.release_live_calls()
        monkeypatch.undo()
        return bridge.banking_session_id

    run(scenario())
    process_b_starts()

    claimed = row_for("call-claimed")
    orphan = row_for("call-real-orphan")

    assert claimed.ended_at is None, "the claimed row was reconciled anyway"
    assert claimed.disconnect_reason != "FORCED_CLEANUP"
    assert orphan.ended_at is not None, "a real orphan was protected by the claim"
    assert orphan.disconnect_reason == "FORCED_CLEANUP"
