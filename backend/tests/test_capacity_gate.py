"""Phase 7.2: one ceiling, whatever door a provider session comes through.

Phase 7.1's architecture review found the capacity boundary split in two.

`RealtimeManager` counts what it has admitted, and there were **two module-level
instances**:

* `browser_calls.voice_call_manager` - the production pool, shared by the
  browser channel and the telephone channel, and the one `readiness` reports;
* `realtime_manager.realtime_manager` - an older instance left from when the
  development router was its only consumer.

`app/routers/dev_realtime.py` still used the second one, and `main.py` mounted
that router unconditionally - despite the router's own docstring saying "This
router must not be exposed in a production deployment." Both instances read the
same `REALTIME_MAX_ACTIVE_SESSIONS`, so the effective ceiling was **twice** the
configured one, and `readiness.in_use` reported only half of it.

Nothing was wrong with the admission logic. Check-and-claim is atomic and has
been since Phase 12. The defect was that there were two of them.

These tests pin the property that matters and does not depend on how many
managers exist: **the configured ceiling is the total number of provider
sessions this process will hold, across every exposed route.**

Nothing here is billable - every call uses an injected connector.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.observability import readiness
from app.realtime.browser_calls import voice_call_manager
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.sessions import session_manager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    import importlib

    other = getattr(
        importlib.import_module("app.realtime.realtime_manager"),
        "realtime_manager",
        None,
    )
    if other is not None and other is not voice_call_manager:
        run(other.close_all())
        run(other.release_all())
    run(voice_call_manager.close_all())
    run(voice_call_manager.release_all())
    session_manager.clear()


@pytest.fixture(autouse=True)
def never_reach_a_provider(monkeypatch):
    """No test in this file may open a real provider session.

    The development route calls `start()` with no connector, so it falls
    through to `open_openai_session` - a real WebSocket to OpenAI. An earlier
    draft of this file did exactly that and opened one. Every manager reachable
    from here therefore has its default connector replaced, and the replacement
    is what a missing stub would otherwise have cost.
    """
    import importlib

    module = importlib.import_module("app.realtime.realtime_manager")

    # A working fake, not the refuser `conftest.block_live_provider` installs.
    # These tests have to watch a route actually take a slot, so the route must
    # be able to succeed - against a stand-in, never a provider. This overrides
    # the global guard deliberately and locally, which is the only way it is
    # ever meant to be overridden.
    monkeypatch.setattr(module, "open_openai_session", connector())

    for manager in (
        voice_call_manager,
        getattr(module, "realtime_manager", None),
    ):
        if manager is not None:
            monkeypatch.setattr(manager, "_connect", connector())
    yield


@pytest.fixture
def ceiling_of_one(monkeypatch):
    """The production setting Phase 7.2 runs under, unchanged."""
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    return 1


class FakeSession:
    """A provider session with no event stream."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def connector():
    async def connect(_context):
        return FakeSession()

    return connect


def a_session():
    return session_manager.create_session().session_id


def dev_start(client, session_id):
    """Whatever the development route does today, through HTTP."""
    return client.post(f"/dev/realtime/session/{session_id}/start")


def mounted_paths(application) -> list[str]:
    """Every path the application actually serves.

    Not `application.routes`: this FastAPI version records each
    `include_router` as an `_IncludedRouter` with no `.path`, so walking that
    list reports every mounted route as absent - which is exactly how the first
    draft of these tests passed while the defect was still there. The generated
    schema is what the application will really answer on.
    """
    return list(application.openapi()["paths"])


def dev_route_is_mounted() -> bool:
    return any(path.startswith("/dev/realtime") for path in mounted_paths(app))


# === CG-001 / CG-002: one ceiling, either order =============================


def test_the_dev_route_cannot_open_a_session_beyond_the_ceiling(ceiling_of_one):
    """CG-001. The production path takes the only slot; the dev route is refused.

    The suite runs outside production, so the development router is mounted
    and this test always has something to prove. It asserts that rather than
    skipping on it: a conditional skip here would go quiet exactly when
    somebody ran the suite in an environment where the route was absent, and
    quiet is indistinguishable from passing.
    """
    assert not settings.is_production, "this test is written for a dev/test run"
    assert dev_route_is_mounted(), "the development router should be mounted here"

    client = TestClient(app)
    voice_session = a_session()
    run(voice_call_manager.start(voice_session, connect=connector()))
    assert voice_call_manager.used_capacity() == 1

    response = dev_start(client, a_session())

    assert response.status_code >= 400, (
        "a second provider session was admitted past a ceiling of one"
    )
    assert voice_call_manager.used_capacity() == 1


def test_the_production_path_cannot_open_a_session_the_dev_route_already_holds(
    ceiling_of_one,
):
    """CG-002. The same question, asked in the other order."""
    assert not settings.is_production, "this test is written for a dev/test run"
    assert dev_route_is_mounted(), "the development router should be mounted here"

    client = TestClient(app)
    held = a_session()
    started = dev_start(client, held)
    assert started.status_code < 400, (
        f"the development route could not take the slot: {started.text}"
    )
    assert voice_call_manager.used_capacity() == 1

    with pytest.raises(RealtimeSessionError) as refused:
        run(voice_call_manager.start(a_session(), connect=connector()))

    assert refused.value.reason is Reason.REALTIME_AT_CAPACITY


# === CG-003: the operator is told the truth =================================


def test_readiness_counts_every_admitted_provider_session(ceiling_of_one):
    """CG-003. `in_use` must be the whole process, not one pool of it."""
    client = TestClient(app)
    run(voice_call_manager.start(a_session(), connect=connector()))

    if dev_route_is_mounted():
        dev_start(client, a_session())

    report = readiness.readiness_report()["checks"]["capacity"]
    assert report["ceiling"] == 1
    assert report["in_use"] == voice_call_manager.used_capacity()
    assert report["in_use"] <= report["ceiling"], (
        f"{report['in_use']} sessions admitted against a ceiling of "
        f"{report['ceiling']}"
    )


# === CG-004 to CG-006: the guarantees that already held, kept ==============


def test_two_simultaneous_admissions_at_a_ceiling_of_one_admit_exactly_one(
    ceiling_of_one,
):
    """CG-004."""

    async def scenario():
        first, second = a_session(), a_session()
        results = await asyncio.gather(
            voice_call_manager.start(first, connect=connector()),
            voice_call_manager.start(second, connect=connector()),
            return_exceptions=True,
        )
        admitted = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, RealtimeSessionError)]
        return admitted, refused

    admitted, refused = run(scenario())
    assert len(admitted) == 1, f"{len(admitted)} calls admitted at a ceiling of one"
    assert len(refused) == 1
    assert refused[0].reason is Reason.REALTIME_AT_CAPACITY
    assert voice_call_manager.used_capacity() == 1


def test_a_failed_connection_frees_the_slot_for_the_next_route(ceiling_of_one):
    """CG-005."""

    def failing():
        async def connect(_context):
            raise RuntimeError("provider refused")

        return connect

    with pytest.raises(RealtimeSessionError):
        run(voice_call_manager.start(a_session(), connect=failing()))

    assert voice_call_manager.used_capacity() == 0
    run(voice_call_manager.start(a_session(), connect=connector()))
    assert voice_call_manager.used_capacity() == 1


def test_tearing_down_twice_leaves_capacity_at_zero(ceiling_of_one):
    """CG-006."""
    session = a_session()
    run(voice_call_manager.start(session, connect=connector()))

    assert run(voice_call_manager.close(session)) is True
    assert run(voice_call_manager.close(session)) is False
    assert voice_call_manager.used_capacity() == 0

    run(voice_call_manager.release(session))
    assert voice_call_manager.used_capacity() == 0


# === CG-007 / CG-008: no other door =========================================


def test_only_one_capacity_pool_is_reachable_from_the_application(ceiling_of_one):
    """CG-007. Every mounted route that can admit a session shares one counter.

    Written against the property rather than the file layout: whatever routers
    exist, the number of provider sessions this process holds is the number the
    authoritative manager knows about.
    """
    import importlib

    # The module, not the instance `app.realtime` re-exports under the same
    # name - asking the package gives the object, and `getattr` on it then
    # finds nothing, which is a test that proves nothing.
    realtime_module = importlib.import_module("app.realtime.realtime_manager")

    pools = {id(voice_call_manager)}
    other = getattr(realtime_module, "realtime_manager", None)
    if other is not None:
        pools.add(id(other))

    assert len(pools) == 1, (
        "more than one RealtimeManager instance exists, so the configured "
        "ceiling is not the number of provider sessions this process will hold"
    )


def test_the_development_router_is_not_mounted_in_production(monkeypatch):
    """CG-008. The router's own docstring is the requirement.

    "This router must not be exposed in a production deployment."
    """
    monkeypatch.setattr(settings, "app_env", "production")

    from app.main import create_app

    production_app = create_app()
    exposed = [p for p in mounted_paths(production_app) if p.startswith("/dev/")]
    assert exposed == [], f"development routes exposed in production: {exposed}"
