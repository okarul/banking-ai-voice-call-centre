"""Phase 7.2: an offline test cannot open a real provider session.

Phase 7.2 found this the expensive way. A capacity test drove the development
realtime route over HTTP; that route called `start()` with no connector, so it
fell through to `open_openai_session` and opened a genuine OpenAI Realtime
session. Every *other* provider test in this suite injects its own connector,
so nothing had ever needed a backstop - and the absence of one cost a real
session before anybody noticed.

`conftest.block_live_provider` is that backstop. These tests are the backstop's
own regression suite: a guard nobody checks is a guard that quietly stops
working.

The repository already had the right vocabulary for this and it did not need a
new one. `pytest.ini` carries `addopts = -m "not integration"`, and marks
`integration` as "reaches a real external service; deselected by default" and
`realtime` as "uses the live OpenAI Realtime API and consumes paid usage". The
guard simply exempts those two and blocks everything else.

Nothing here contacts a provider, including the test that proves the exemption
exists.
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.realtime.browser_calls import voice_call_manager
from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.sessions import session_manager

GUARD_MESSAGE = "an offline test tried to open a real OpenAI Realtime session"


def run(coro):
    return asyncio.run(coro)


def realtime_module():
    return importlib.import_module("app.realtime.realtime_manager")


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    run(voice_call_manager.close_all())
    run(voice_call_manager.release_all())
    session_manager.clear()


def a_session():
    return session_manager.create_session().session_id


# === PG-001: the telephone path cannot reach the provider ===================


def test_the_phone_connector_seam_cannot_reach_a_real_provider():
    """PG-001.

    `telephony.service.open_phone_realtime_session` imports the connector late, which is
    exactly the seam an offline test would otherwise slip through.
    """
    from app.telephony import service

    context = BankingRealtimeContext(session_id=a_session())

    with pytest.raises(AssertionError) as blocked:
        run(service.open_phone_realtime_session(context))

    assert GUARD_MESSAGE in str(blocked.value)


# === PG-002: the development route cannot reach the provider ================


def test_the_dev_realtime_route_cannot_reach_a_real_provider():
    """PG-002.

    Whether the route is mounted or not, the outcome an offline run must never
    produce is a live session. If it is mounted it must fail closed; if it is
    absent there is nothing to fail.
    """
    client = TestClient(app)
    paths = list(app.openapi()["paths"])
    start_path = "/dev/realtime/session/{session_id}/start"

    if start_path not in paths:
        assert voice_call_manager.used_capacity() == 0
        return

    response = client.post(f"/dev/realtime/session/{a_session()}/start")

    assert response.status_code >= 400, (
        "the development route opened a session in an offline test"
    )
    assert voice_call_manager.used_capacity() == 0


# === PG-003: the manager's own default is blocked, injection still works =====


def test_a_manager_holding_the_live_connector_is_blocked(monkeypatch):
    """PG-003.

    The guard blocks the *live* connector wherever it is installed - not every
    manager's default. `voice_call_manager` carries `open_browser_call`, a
    stand-in that never leaves this process, and blocking that would break the
    browser path while preventing nothing.
    """
    from app.realtime.realtime_manager import RealtimeManager

    # A manager built the way a live one is built: default connector, no
    # override. `__init__` reads the module attribute, which the guard has
    # already replaced.
    live_shaped = RealtimeManager()

    with pytest.raises(RealtimeSessionError) as refused:
        run(live_shaped.start(a_session()))

    assert refused.value.reason is Reason.REALTIME_CONNECTION_FAILED
    assert live_shaped.used_capacity() == 0, "a blocked start kept a slot"


def test_an_offline_default_connector_is_left_working(monkeypatch):
    """PG-003, the other half. The guard must not break offline paths."""

    class FakeSession:
        async def close(self):
            return None

    async def offline(_context):
        return FakeSession()

    from app.realtime.realtime_manager import RealtimeManager

    manager = RealtimeManager(connect=offline)
    run(manager.start(a_session()))
    assert manager.used_capacity() == 1
    run(manager.close_all())


def test_injection_still_works_on_the_authoritative_manager():
    """PG-003. A test that supplies its own connector is unaffected."""

    class FakeSession:
        async def close(self):
            return None

    async def connector(_context):
        return FakeSession()

    run(voice_call_manager.start(a_session(), connect=connector))
    assert voice_call_manager.used_capacity() == 1


def test_the_live_connector_is_replaced_everywhere_it_could_be_reached():
    """PG-003, stated over the objects rather than one path through them."""
    module = realtime_module()
    assert module.open_openai_session.__name__ == "refuse_live_provider", (
        "the live connector is still reachable by name"
    )

    managers = [voice_call_manager]
    orphan = getattr(module, "realtime_manager", None)
    if orphan is not None and orphan is not voice_call_manager:
        managers.append(orphan)

    for manager in managers:
        assert manager._connect.__name__ != "open_openai_session", (
            "a reachable RealtimeManager kept the live connector"
        )


# === PG-004: the deliberate opt-in still exists =============================


def test_the_repository_still_has_an_explicit_paid_provider_opt_in():
    """PG-004, structurally. No provider is contacted to prove this.

    The opt-in is `pytest.ini`'s `integration` marker, deselected by default,
    and `tests/test_realtime_integration.py` is what claims it.
    """
    import configparser
    import pathlib

    ini = configparser.ConfigParser()
    ini.read(pathlib.Path(__file__).resolve().parents[1] / "pytest.ini")
    assert 'not integration' in ini["pytest"]["addopts"], (
        "integration tests are no longer deselected by default"
    )

    markers = ini["pytest"]["markers"]
    assert "integration:" in markers
    assert "realtime:" in markers

    source = (
        pathlib.Path(__file__).with_name("test_realtime_integration.py")
    ).read_text(encoding="utf-8")
    assert "pytest.mark.integration" in source
    assert "pytest.mark.realtime" in source


@pytest.mark.integration
@pytest.mark.realtime
def test_a_marked_test_keeps_the_real_connector():
    """PG-004, behaviourally - and still without contacting anything.

    Deselected by default. When somebody deliberately selects it, all it checks
    is that the guard stood aside: the module's connector is the real function,
    not the refuser. It never calls it.
    """
    module = realtime_module()
    assert module.open_openai_session.__name__ == "open_openai_session", (
        "the guard did not stand aside for an explicitly marked test"
    )


# === the guard costs the ordinary suite nothing =============================


def test_the_guard_replaces_connectors_and_nothing_else():
    """It must not quietly change configuration to achieve its effect.

    A guard that switched the deployment into "unconfigured" would block the
    provider just as well and would also silently change what every other test
    is testing.
    """
    module = realtime_module()
    assert module.open_openai_session.__name__ == "refuse_live_provider"

    # Configuration is exactly what it was.
    assert settings.realtime_configured is bool(settings.openai_api_key)
    assert voice_call_manager.max_active == settings.realtime_max_active_sessions
