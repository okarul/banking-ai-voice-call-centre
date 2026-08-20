"""Shared test setup.

One job: keep the persistent PIN lockout from leaking between tests.

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

import pytest

from app.auth import lockout


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
