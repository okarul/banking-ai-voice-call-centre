"""Shared test setup.

Three jobs. Two are about stopping one test's leftovers becoming another's
mystery failure. The third is about stopping an offline test reaching OpenAI at
all - see `block_live_provider`.

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

# The markers this repository already uses for tests that are *allowed* to
# reach a real external service. `pytest.ini` deselects `integration` by
# default, which is what keeps `pytest tests -q` free and offline; these are
# the tests that opt back in deliberately.
LIVE_PROVIDER_MARKERS = frozenset({"integration", "realtime"})


@pytest.fixture(autouse=True)
def block_live_provider(request):
    """An offline test may not open a real provider session. Fail closed.

    Phase 7.2 found this the expensive way. A capacity test drove the
    development realtime route over HTTP; that route called `start()` with no
    connector, so it fell through to `open_openai_session` and opened a genuine
    OpenAI Realtime session. Nothing in the suite stopped it, because every
    *other* provider test injects its own connector and none had ever needed a
    backstop.

    Cleaning up afterwards is not the fix: by then the connection has been
    made and the usage spent. So the live connector is replaced with one that
    raises, and an offline test that reaches it fails loudly and says what to
    do instead.

    Two seams, because a connector is resolved in two ways:

    * the module attribute, read late by `telephony.service` and by
      `RealtimeManager.__init__`, so patching it covers every manager built
      from here on;
    * `_connect` on managers that already exist, which captured the function at
      import time and would not see the patch.

    `test_capacity_gate.py` proves there is exactly one such manager, which is
    what makes those two seams complete rather than merely thorough.

    Tests marked `integration` or `realtime` are left alone. They are
    deselected by default and are selected only on purpose.
    """
    if LIVE_PROVIDER_MARKERS & set(request.node.keywords):
        yield
        return

    async def refuse_live_provider(_context):
        raise AssertionError(
            "an offline test tried to open a real OpenAI Realtime session. "
            "Inject a connector with start(connect=...), or mark the test "
            "`integration` if it is meant to reach the provider."
        )

    realtime_module = _realtime_module()
    live_connector = realtime_module.open_openai_session

    # Saved and restored by hand rather than through `monkeypatch`. Depending
    # on that fixture from an autouse one changes when it is created for every
    # test in the suite, and therefore when its undo runs relative to other
    # fixtures - which is enough to break a test that patches something a later
    # teardown still calls. A guard must not reorder the suite it guards.
    patched = [(realtime_module, "open_openai_session", live_connector)]
    realtime_module.open_openai_session = refuse_live_provider
    for manager in _live_managers():
        # Only where the live connector is actually installed. A manager whose
        # default is an offline stand-in - `browser_call_manager` holds
        # `open_browser_call`, which never leaves this process - is left alone,
        # because blocking it would break the browser path without preventing
        # anything.
        if manager._connect is live_connector:
            patched.append((manager, "_connect", manager._connect))
            manager._connect = refuse_live_provider
    try:
        yield
    finally:
        for target, name, original in patched:
            setattr(target, name, original)


def _realtime_module():
    """The realtime_manager *module*, not the instance of the same name.

    `app/realtime/__init__.py` re-exports the module-level `realtime_manager`
    object, which rebinds that attribute on the package - so both
    `from app.realtime import realtime_manager` and
    `import app.realtime.realtime_manager as m` hand back the **instance**.
    Only a direct `sys.modules` lookup reaches the module. This shadowing is
    part of why a second capacity pool sat unnoticed behind a name everybody
    read as the module.
    """
    import importlib

    return importlib.import_module("app.realtime.realtime_manager")


def _live_managers():
    """Every RealtimeManager instance an offline test could reach."""
    realtime_module = _realtime_module()

    managers = [voice_call_manager]
    orphan = getattr(realtime_module, "realtime_manager", None)
    if orphan is not None and orphan is not voice_call_manager:
        managers.append(orphan)
    return managers


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


@pytest.fixture(scope="session", autouse=True)
def unpinned_capacity():
    """Do not let the developer's .env decide what the suite can test.

    `REALTIME_MAX_ACTIVE_SESSIONS` is a deployment control, and Phase 6 sets it
    to 1 for single-call live testing. Tests that open two or three calls to
    prove they stay isolated were then failing on the ceiling rather than on
    anything they assert — a suite whose results depend on a local, uncommitted
    file is a suite that passes on one machine and fails on another.

    So the ambient value is replaced with "no limit" once, at session start.

    Session-scoped deliberately. As a function-scoped fixture this ran *after*
    module-scoped fixtures that deliberately set a ceiling — so a suite whose
    whole point was refusing a sixth concurrent call had its ceiling removed,
    accepted the sixth, and then hung waiting for a call that was never hung up.
    Running once, first, leaves every later fixture and monkeypatch free to set
    the ceiling its own test needs.
    """
    from app.config import settings

    previous = settings.realtime_max_active_sessions
    settings.realtime_max_active_sessions = 0
    yield
    settings.realtime_max_active_sessions = previous
