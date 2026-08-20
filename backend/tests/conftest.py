"""Shared test setup.

Two jobs, both about stopping one test's leftovers becoming another's mystery
failure.

The lockout added in Phase 2 is deliberately durable — that is the whole point
of it, since a control that forgets when the caller hangs up is not a
brute-force control. But durability across *tests* is a different matter. The
suite shares one PostgreSQL database and a handful of synthetic customers, and
many tests deliberately submit wrong PINs. Without this fixture those failures
would accumulate against DEMO001 until some later, unrelated test found it
locked, and the suite would start failing in an order-dependent way that had
nothing to do with the code under test.

Clearing before each test rather than after is intentional: a test that fails
partway through still leaves the next one a clean slate.
"""

import asyncio

import pytest

from app.auth import lockout
from app.realtime.browser_calls import voice_call_manager
from app.telephony.bridge import phone_call_registry


@pytest.fixture(autouse=True)
def clear_persistent_lockout():
    """Start every test with no customer locked out."""
    try:
        lockout.clear_all()
    except Exception:
        # A few tests run with no database configured at all — /health and the
        # settings tests among them. They cannot have written a lock either, so
        # there is nothing to clear and nothing to fail over.
        pass
    yield


@pytest.fixture(autouse=True)
def release_live_calls():
    """Leave no telephone call, bridge or capacity slot behind.

    Any test that reaches the telephony service creates a bridge and takes a
    capacity slot, and both live in process-wide registries. Without this, a
    test that ended a call untidily would show up as a capacity assertion
    failing in a completely unrelated file — and only when the whole suite runs,
    which is the worst way to find it.

    Guarded by a count so the common case (nearly every test) costs one integer
    comparison rather than an event loop.
    """
    yield
    if phone_call_registry.active_count():
        asyncio.run(phone_call_registry.close_all())
    if voice_call_manager.used_capacity():
        asyncio.run(_release_everything())


async def _release_everything() -> None:
    await voice_call_manager.close_all()
    await voice_call_manager.release_all()
