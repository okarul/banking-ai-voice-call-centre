"""Phase 6.8: the things production asks that a demo never does.

Why did this call end, and do the log, the database and the operator agree?
Can this process serve a call right now, and if not which part is missing?
When a supplier fails, does the service survive it and give the slot back?
And is there anything in a log line that should not be written down?

Workstreams C, L, M, N, O and P, deterministically and offline.
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.realtime.browser_calls import voice_call_manager
from app.sessions import session_manager
from app.telephony import reasons, service
from app.telephony.bridge import phone_call_registry
from app.telephony.lifecycle import EndReason


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    asyncio.run(phone_call_registry.close_all())
    asyncio.run(voice_call_manager.close_all())
    asyncio.run(voice_call_manager.release_all())
    session_manager.clear()
    wipe()


def run(coro):
    return asyncio.run(coro)


def event(call_id, event_id=None, event_type="incoming"):
    from datetime import datetime, timezone

    from app.telephony.schemas import InboundCallEvent

    return InboundCallEvent(
        provider="TEST",
        provider_event_id=event_id or f"evt-{call_id}-{event_type}",
        provider_call_id=call_id,
        event_type=event_type,
        event_timestamp=datetime.now(timezone.utc),
    )


def row(call_id):
    with session_scope() as db:
        return db.scalars(
            select(AgentSession).where(AgentSession.provider_call_id == call_id)
        ).one()


@pytest.fixture
def phone(monkeypatch):
    import tests.test_telephony_webhook as webhook

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_media_transport", "loopback")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    monkeypatch.setattr(
        service, "open_phone_realtime_session", webhook._stub_connector
    )
    return settings


# === C: one vocabulary, three systems =======================================


def test_the_taxonomy_covers_every_required_category():
    """Each one leads somewhere different, so each one has a name."""
    required = {
        "CALLER_GOODBYE",
        "CALLER_SILENT",
        "CALLER_HANGUP",
        "PROVIDER_HANGUP",
        "REALTIME_START_FAILURE",
        "REALTIME_RUNTIME_FAILURE",
        "MEDIA_FAILURE",
        "PROTOCOL_MISMATCH",
        "APPLICATION_ERROR",
        "APPLICATION_END",
    }
    for name in required:
        assert hasattr(reasons, name), f"no reason named {name}"
    assert len(set(reasons.ALL)) == len(reasons.ALL), "two categories share a value"


def test_every_lifecycle_ending_maps_to_a_recorded_reason():
    for ending in EndReason:
        recorded = reasons.for_end_reason(ending.value)
        assert recorded in reasons.ALL, f"{ending} maps outside the taxonomy"


def test_an_unknown_ending_does_not_invent_a_category():
    assert reasons.for_end_reason("SOMETHING_NEW") == reasons.APPLICATION_END


def test_the_caller_hangup_value_stays_backward_compatible():
    """Dashboards and history already use this string."""
    assert reasons.CALLER_HANGUP == "CUSTOMER_ENDED"


def test_idle_abandonment_is_not_called_silence():
    """Two unrelated events that used to share one word.

    A quiet customer and a call nobody is on lead to completely different
    investigations, and `SILENCE_TIMEOUT` meant both.
    """
    assert reasons.IDLE_TIMEOUT == "IDLE_TIMEOUT"
    assert reasons.IDLE_TIMEOUT != reasons.CALLER_SILENT


def test_realtime_and_media_failures_are_told_apart():
    """`PROVIDER_FAILURE` covered both and named neither supplier."""
    assert reasons.REALTIME_RUNTIME_FAILURE != reasons.MEDIA_FAILURE


def test_a_goodbye_records_caller_goodbye(phone):
    async def scenario():
        result = await service.handle_event(event("say-bye"))
        bridge = phone_call_registry.get("say-bye")
        await service._on_call_ended(
            "say-bye", bridge.banking_session_id, EndReason.CALLER_GOODBYE.value
        )
        return result

    run(scenario())

    assert row("say-bye").disconnect_reason == reasons.CALLER_GOODBYE


def test_a_silent_caller_records_caller_silent(phone):
    async def scenario():
        await service.handle_event(event("gone-quiet"))
        bridge = phone_call_registry.get("gone-quiet")
        await service._on_call_ended(
            "gone-quiet", bridge.banking_session_id, EndReason.CALLER_SILENT.value
        )

    run(scenario())

    assert row("gone-quiet").disconnect_reason == reasons.CALLER_SILENT


def test_a_lost_model_session_records_a_realtime_failure(phone):
    async def scenario():
        await service.handle_event(event("model-gone"))
        bridge = phone_call_registry.get("model-gone")
        await service._on_call_lost(
            "model-gone",
            bridge.banking_session_id,
            reasons.REALTIME_RUNTIME_FAILURE,
        )

    run(scenario())

    assert row("model-gone").disconnect_reason == reasons.REALTIME_RUNTIME_FAILURE


def test_a_lost_media_path_records_a_media_failure(phone):
    async def scenario():
        await service.handle_event(event("media-gone"))
        bridge = phone_call_registry.get("media-gone")
        await service._on_call_lost(
            "media-gone", bridge.banking_session_id, reasons.MEDIA_FAILURE
        )

    run(scenario())

    assert row("media-gone").disconnect_reason == reasons.MEDIA_FAILURE


def test_a_realtime_startup_failure_records_its_own_reason(phone, monkeypatch):
    async def refuse(_context):
        raise RuntimeError("provider refused")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)
    run(service.handle_event(event("never-started")))

    record = row("never-started")
    assert record.status == "REJECTED"
    assert record.disconnect_reason == reasons.REALTIME_START_FAILURE


def test_an_ending_already_recorded_is_not_overwritten_by_the_socket(phone):
    """The route's `finally` must not relabel a call that already ended.

    A goodbye that has been recorded stays a goodbye even though the media
    socket closes immediately afterwards — which it always does.
    """
    from app.observability import recorder

    async def scenario():
        await service.handle_event(event("keeps-reason"))
        bridge = phone_call_registry.get("keeps-reason")
        await service._on_call_ended(
            "keeps-reason", bridge.banking_session_id, EndReason.CALLER_GOODBYE.value
        )
        # Exactly what the route does when the socket then closes.
        return recorder.close_phone_call(
            "keeps-reason", reason=reasons.CALLER_HANGUP
        )

    second = run(scenario())

    assert second is None, "a closed call was closed again"
    assert row("keeps-reason").disconnect_reason == reasons.CALLER_GOODBYE


# === L: failure injection ===================================================


def test_the_service_survives_a_provider_that_always_refuses(phone, monkeypatch):
    """Ten refused calls in a row, and the eleventh still works."""
    import tests.test_telephony_webhook as webhook

    async def refuse(_context):
        raise RuntimeError("provider refused")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)
    for n in range(10):
        run(service.handle_event(event(f"refuse-{n}", f"evt-refuse-{n}")))

    assert voice_call_manager.used_capacity() == 0, "a slot leaked on every refusal"
    assert phone_call_registry.active_count() == 0

    monkeypatch.setattr(service, "open_phone_realtime_session", webhook._stub_connector)
    result = run(service.handle_event(event("recovered", "evt-recovered")))

    assert result.outcome.value == "accepted"
    run(service.tear_down("recovered", result.agent_session_id))


def test_a_database_failure_during_admission_does_not_crash_the_service(
    phone, monkeypatch
):
    """A refusal an operator can read beats a stack trace and a held slot."""
    from app.observability import recorder

    def explode(*args, **kwargs):
        raise RuntimeError("database unavailable")

    # `raising=True` deliberately: a patch that silently attaches a new
    # attribute would leave this test passing while testing nothing, which is
    # exactly how it first went wrong.
    monkeypatch.setattr(recorder, "claim_phone_call", explode)

    try:
        run(service.handle_event(event("db-down", "evt-db-down")))
    except Exception:
        pass  # the assertion is about what is left behind, not what was raised

    assert voice_call_manager.used_capacity() == 0, "a slot survived a database failure"
    assert phone_call_registry.active_count() == 0


def test_a_tool_failure_does_not_end_the_call(phone):
    """A controlled tool error is an answer the agent gives, not a hang-up."""

    async def scenario():
        await service.handle_event(event("tool-fails"))
        bridge = phone_call_registry.get("tool-fails")

        class Event:
            def __init__(self, type_, **fields):
                self.type = type_
                for name, value in fields.items():
                    setattr(self, name, value)

        bridge.on_realtime_event(
            bridge.banking_session_id,
            Event("tool_start", tool=Event("t", name="get_account_balance"), arguments="{}"),
        )
        await asyncio.sleep(0.1)
        alive = bridge.lifecycle.end_reason is None and not bridge.closed
        await service.tear_down("tool-fails", bridge.banking_session_id)
        return alive

    assert run(scenario()) is True


def test_every_failure_path_gives_the_capacity_slot_back(phone, monkeypatch):
    """Workstream O, stated as the property that matters with a ceiling of one."""
    import tests.test_telephony_webhook as webhook

    failures = {
        "start-fail": RuntimeError("provider refused"),
        "start-timeout": asyncio.TimeoutError(),
    }
    for call_id, error in failures.items():

        async def refuse(_context, _error=error):
            raise _error

        monkeypatch.setattr(service, "open_phone_realtime_session", refuse)
        run(service.handle_event(event(call_id, f"evt-{call_id}")))
        assert voice_call_manager.used_capacity() == 0, f"{call_id} held a slot"

    monkeypatch.setattr(service, "open_phone_realtime_session", webhook._stub_connector)

    async def normal():
        result = await service.handle_event(event("clean", "evt-clean"))
        bridge = phone_call_registry.get("clean")
        await service._on_call_ended(
            "clean", bridge.banking_session_id, EndReason.CALLER_GOODBYE.value
        )
        return bridge

    bridge = run(normal())

    assert voice_call_manager.used_capacity() == 0
    assert phone_call_registry.active_count() == 0
    assert session_manager.get_session(bridge.banking_session_id) is None
    assert bridge.closed is True
    assert bridge.transport.ended is True
    assert bridge.lifecycle._silence_task is None, "a silence timer outlived the call"


# === M: readiness ===========================================================


@pytest.fixture
def app_client():
    from app.main import create_app

    return TestClient(create_app())


def test_health_is_liveness_only(app_client):
    """Cheap and dependency-free: a database blip must not restart a process."""
    response = app_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_each_dependency(app_client):
    response = app_client.get("/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert set(body["checks"]) == {"database", "realtime", "telephony", "capacity"}


def test_readiness_never_opens_a_paid_realtime_session(app_client, monkeypatch):
    """A monitoring system polling this must not be able to spend money."""
    opened = []

    async def forbidden(_context):
        opened.append(1)
        raise AssertionError("readiness opened a realtime session")

    monkeypatch.setattr(service, "open_phone_realtime_session", forbidden)

    for _ in range(5):
        app_client.get("/readiness")

    assert opened == []
    assert app_client.get("/readiness").json()["checks"]["realtime"]["probed"] is False


def test_readiness_exposes_no_credential(app_client):
    body = app_client.get("/readiness").text

    key = getattr(settings, "openai_api_key", "") or ""
    assert key not in body
    if len(key) > 12:
        assert key[:12] not in body
    for forbidden in ("Bearer", "Authorization", "password", "postgresql://"):
        assert forbidden not in body


def test_readiness_refuses_when_the_database_is_unreachable(app_client, monkeypatch):
    from app.observability import readiness as readiness_module

    monkeypatch.setattr(
        readiness_module, "_database", lambda: {"ready": False, "reason": "unreachable"}
    )

    response = app_client.get("/readiness")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["checks"]["database"]["reason"] == "unreachable"


def test_readiness_refuses_without_realtime_configuration(app_client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)

    response = app_client.get("/readiness")

    assert response.status_code == 503
    assert response.json()["checks"]["realtime"]["reason"] == "no_api_key"


def test_a_full_switchboard_is_busy_not_unready(app_client, monkeypatch):
    """Capacity is reported for the operator and excluded from the verdict."""
    from app.observability import readiness as readiness_module

    monkeypatch.setattr(
        readiness_module,
        "_capacity",
        lambda: {"ready": True, "in_use": 1, "ceiling": 1},
    )

    body = app_client.get("/readiness").json()

    assert body["ready"] is True
    assert body["checks"]["capacity"]["in_use"] == 1


def test_a_disabled_telephone_channel_is_not_unready(app_client, monkeypatch):
    monkeypatch.setattr(settings, "telephony_enabled", False)

    body = app_client.get("/readiness").json()

    assert body["ready"] is True
    assert body["checks"]["telephony"]["enabled"] is False


def test_an_enabled_channel_without_a_webhook_secret_is_unready(
    app_client, monkeypatch
):
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", None)

    response = app_client.get("/readiness")

    assert response.status_code == 503
    assert response.json()["checks"]["telephony"]["reason"] == "no_webhook_secret"


def test_readiness_reports_the_media_protocol_version(app_client, monkeypatch):
    """So a version skew is visible before a call proves it."""
    from app.telephony.media import PROTOCOL_VERSION

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", "x" * 32)

    body = app_client.get("/readiness").json()

    assert body["checks"]["telephony"]["media_protocol_version"] == PROTOCOL_VERSION


# === N: what may and may not be written down ================================


def test_a_call_can_be_reconstructed_from_its_logs(phone, caplog):
    """One provider call id, and the story of what happened to it."""
    with caplog.at_level(logging.INFO):

        async def scenario():
            await service.handle_event(event("trace-me"))
            bridge = phone_call_registry.get("trace-me")
            await service._on_call_ended(
                "trace-me", bridge.banking_session_id, EndReason.CALLER_GOODBYE.value
            )

        run(scenario())

    lines = [record.getMessage() for record in caplog.records]
    about_this_call = [line for line in lines if "trace-me" in line]

    assert about_this_call, "nothing was logged about this call at all"
    assert any("ended" in line for line in about_this_call), about_this_call
    assert any(EndReason.CALLER_GOODBYE.value in line for line in about_this_call)


def test_no_log_line_carries_a_pin_or_a_balance(phone, caplog):
    """The redaction that matters: this path carries spoken PINs."""
    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import LoopbackMediaTransport

    class Model:
        async def send_audio(self, session_id, audio):
            return None

        async def send_message(self, session_id, text):
            return None

    class Item:
        def __init__(self, role, text):
            self.item_id = "i1"
            self.role = role
            self.type = "message"
            self.content = [
                type("C", (), {"type": "input_audio", "transcript": text, "text": None})()
            ]

    class Event:
        def __init__(self, type_, **fields):
            self.type = type_
            for name, value in fields.items():
                setattr(self, name, value)

    secrets = ["4321", "S1234567D", "12450.75"]

    with caplog.at_level(logging.DEBUG):

        async def scenario():
            session = session_manager.create_session()
            bridge = PhoneCallBridge(
                provider_call_id="secretive",
                banking_session_id=session.session_id,
                transport=LoopbackMediaTransport(),
                realtime_manager=Model(),
                outbound_max_frames=50,
            )
            await bridge.start()
            bridge.on_realtime_event(
                bridge.banking_session_id,
                Event(
                    "history_updated",
                    history=[
                        Item("user", "my pin is 4321 and my nric is S1234567D"),
                        Item("assistant", "Your balance is 12450.75 SGD."),
                    ],
                ),
            )
            await asyncio.sleep(0.1)
            await bridge.close()

        run(scenario())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in logged, f"{secret!r} reached a log line"


def test_the_connection_diagnostic_never_echoes_provider_text():
    """A close reason is provider text and is matched, never repeated."""
    from app.realtime.realtime_manager import describe_connection_failure

    class Closed(Exception):
        def __init__(self):
            self.reason = "customer Jane Tan pin 4321 key sk-proj-secret balance 12450.75"
            self.code = 1006

    described = describe_connection_failure(Closed())

    for secret in ("Jane", "4321", "sk-proj", "12450.75"):
        assert secret not in described
