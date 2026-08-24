"""Phase 6.8: saying why a call was refused, and saying it accurately.

A live deployment spent a long time looking like a code defect. The gateway
logged `refused by the bank: rejected_capacity` for every call, so the obvious
reading was that the bank was full — while the concurrency limit was 1 and no
call was in progress. Meanwhile the application logged only
`connection failed: ConnectionClosedError`, which says nothing at all.

The provider had in fact opened the socket, refused the session and closed it.
Probing the endpoint directly returned:

    code=credit_balance_exhausted
    "You have no credits remaining. Add credits to continue using the API"

Nothing in the application was wrong. But nothing in the application could say
so, because the close reason was discarded and a startup failure was reported
as capacity exhaustion. These tests are about both halves of that.
"""

import asyncio

import pytest
from sqlalchemy import delete, select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.realtime.browser_calls import voice_call_manager
from app.realtime.realtime_manager import (
    RealtimeStage,
    describe_connection_failure,
)
from app.sessions import session_manager
from app.telephony import service
from app.telephony.bridge import phone_call_registry
from app.telephony.service import Outcome


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


# === the diagnosis =========================================================


class _Closed(Exception):
    """Shaped like a websockets ConnectionClosedError."""

    def __init__(self, reason, code=1008):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def test_a_provider_refusal_names_the_provider_code():
    """The line that would have ended the investigation on its first day."""
    error = _Closed("You have no credits remaining. credit_balance_exhausted")

    described = describe_connection_failure(error)

    assert "close=1008" in described
    assert "credit_balance_exhausted" in described


@pytest.mark.parametrize(
    "code",
    [
        "invalid_api_key",
        "insufficient_quota",
        "account_deactivated",
        "model_not_found",
        "beta_api_shape_disabled",
        "rate_limit_exceeded",
    ],
)
def test_each_known_provider_code_is_reported(code):
    """Each one sends an operator somewhere different."""
    assert code in describe_connection_failure(_Closed(f"refused: {code}"))


def test_an_unrecognised_close_reason_is_never_echoed():
    """A close reason is provider text, and provider text is not ours to log.

    Matched against a list rather than repeated, so nothing a provider chooses
    to put in it can reach a log line.
    """
    described = describe_connection_failure(
        _Closed("customer Jane Tan balance 12450.75 pin 1234 key sk-proj-abc", code=1006)
    )

    assert described == "_Closed close=1006"
    for secret in ("Jane", "12450.75", "1234", "sk-proj"):
        assert secret not in described


def test_an_ordinary_exception_reports_only_its_type():
    assert describe_connection_failure(RuntimeError("boom")) == "RuntimeError"


def test_the_failure_stages_are_named():
    """An operator can tell a refused handshake from a broken running call."""
    assert [stage.value for stage in RealtimeStage] == [
        "CONNECTING",
        "SESSION_CREATE",
        "SESSION_CONFIGURE",
        "RUNNING",
    ]


# === the admission outcome =================================================


def _event(call_id="admit-1", event_id="evt-admit-1"):
    from datetime import datetime, timezone

    from app.telephony.schemas import InboundCallEvent

    return InboundCallEvent(
        provider="TEST",
        provider_event_id=event_id,
        provider_call_id=call_id,
        event_type="incoming",
        event_timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def telephony_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_media_transport", "loopback")
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)
    return settings


def test_a_provider_that_will_not_connect_is_not_reported_as_capacity(
    telephony_on, monkeypatch
):
    """The mislabelling that sent an operator to the wrong place entirely."""

    async def refuse(_context):
        raise _Closed("You have no credits remaining. credit_balance_exhausted")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)

    result = asyncio.run(service.handle_event(_event()))

    assert result.outcome is Outcome.REJECTED_REALTIME_UNAVAILABLE, (
        "a provider outage was reported as a full switchboard"
    )
    assert result.outcome is not Outcome.REJECTED_CAPACITY


def test_genuine_capacity_exhaustion_is_still_reported_as_capacity(
    telephony_on, monkeypatch
):
    """The other half: the label must still be right when it *is* capacity.

    A ceiling of zero means *unlimited* in this application, so exhaustion has
    to be produced the way a caller would produce it — by holding the only slot
    with a call that is genuinely in progress.
    """
    import tests.test_telephony_webhook as webhook

    monkeypatch.setattr(service, "open_phone_realtime_session", webhook._stub_connector)

    async def scenario():
        first = await service.handle_event(_event(call_id="admit-one", event_id="e1"))
        second = await service.handle_event(_event(call_id="admit-two", event_id="e2"))
        await service.tear_down("admit-one", first.agent_session_id)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.outcome is Outcome.ACCEPTED
    assert second.outcome is Outcome.REJECTED_CAPACITY, (
        "a genuinely full switchboard was reported as something else"
    )


def test_the_two_refusals_are_distinguishable_values():
    assert Outcome.REJECTED_CAPACITY.value == "rejected_capacity"
    assert Outcome.REJECTED_REALTIME_UNAVAILABLE.value == "rejected_realtime_unavailable"
    assert Outcome.REJECTED_CAPACITY is not Outcome.REJECTED_REALTIME_UNAVAILABLE


def test_the_database_records_the_reason_that_actually_applied(
    telephony_on, monkeypatch
):
    """Logs, gateway and database must tell one story, not three."""

    async def refuse(_context):
        raise _Closed("refused: insufficient_quota")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)
    asyncio.run(service.handle_event(_event(call_id="admit-db")))

    with session_scope() as db:
        rows = list(db.scalars(select(AgentSession)))

    assert rows, "the refused call was not recorded at all"
    statuses = {row.status for row in rows}
    assert statuses == {"REJECTED"}, statuses


# === Workstream O: a refused call leaves nothing behind ====================


def test_a_realtime_startup_failure_leaks_no_capacity(telephony_on, monkeypatch):
    """The slot must come back, or one provider outage ends the service.

    With a ceiling of one, a leaked reservation means every later call is
    refused for capacity — and that refusal would be true, which is the
    hardest kind of failure to unpick.
    """

    async def refuse(_context):
        raise _Closed("refused: credit_balance_exhausted")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)

    before = voice_call_manager.used_capacity()
    for n in range(4):
        asyncio.run(
            service.handle_event(_event(call_id=f"leak-{n}", event_id=f"evt-leak-{n}"))
        )

    assert voice_call_manager.used_capacity() == before == 0
    assert phone_call_registry.active_count() == 0


def test_a_later_call_still_succeeds_once_the_provider_recovers(
    telephony_on, monkeypatch
):
    """Nothing about a refusal may poison the next call."""
    import tests.test_telephony_webhook as webhook

    async def refuse(_context):
        raise _Closed("refused: credit_balance_exhausted")

    monkeypatch.setattr(service, "open_phone_realtime_session", refuse)
    first = asyncio.run(service.handle_event(_event(call_id="down", event_id="evt-down")))

    monkeypatch.setattr(service, "open_phone_realtime_session", webhook._stub_connector)
    second = asyncio.run(service.handle_event(_event(call_id="up", event_id="evt-up")))

    assert first.outcome is Outcome.REJECTED_REALTIME_UNAVAILABLE
    assert second.outcome is Outcome.ACCEPTED, "a recovered provider was still refused"

    asyncio.run(service.tear_down("up", second.agent_session_id))
