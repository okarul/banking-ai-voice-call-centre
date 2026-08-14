"""Phase 9 deterministic realtime tests.

Every test here runs offline. The OpenAI connection is injected, so the whole
lifecycle — start, associate, send, fail, close, clean up — is exercised with
no network, no API key, no microphone and no paid usage. The live API is
covered separately in test_realtime_integration.py, which is deselected by
default.

What these tests are really defending is the identity boundary: the model
chooses tool arguments, but it never chooses whose data is read.
"""

import asyncio
import json
import logging

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext
from fastapi.testclient import TestClient

from app.agents.registry import FORBIDDEN_ARGUMENTS
from app.auth import authentication
from app.config import settings
from app.main import app
from app.realtime import events as realtime_events
from app.realtime import tools as realtime_tools
from app.realtime.banking_realtime import (
    INSTRUCTIONS,
    build_banking_agent,
    model_settings,
    run_config,
)
from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import (
    RealtimeManager,
    RealtimeSessionError,
    Reason,
)
from app.sessions import SessionManager

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {"DEMO001": "4821", "DEMO002": "7315", "DEMO003": "2648"}


# --- fakes ------------------------------------------------------------------


class FakeRealtimeSession:
    """Stands in for agents.realtime.RealtimeSession.

    Records what was sent and can replay a scripted list of events, so the
    manager's behaviour is observable without touching OpenAI.
    """

    def __init__(self, events=None) -> None:
        self.audio_chunks: list[bytes] = []
        self.messages: list[str] = []
        self.interrupted = 0
        self.closed = False
        self._events = list(events or [])

    async def send_audio(self, audio: bytes) -> None:
        self.audio_chunks.append(audio)

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def close(self) -> None:
        self.closed = True

    async def __aiter__(self):
        for event in self._events:
            yield event
        # Stay open like a real call rather than ending the stream.
        while True:
            await asyncio.sleep(0.01)


class FakeEvent:
    """A minimal stand-in for a realtime session event."""

    def __init__(self, type_: str, **fields) -> None:
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def fake_connector(session=None, *, fail_with=None):
    """Build a connector for RealtimeManager that never touches the network."""
    created = []

    async def connect(context: BankingRealtimeContext):
        if fail_with is not None:
            raise fail_with
        made = session or FakeRealtimeSession()
        created.append((context, made))
        return made

    connect.created = created
    return connect


def run(coro):
    """Run one coroutine to completion. Avoids a pytest-asyncio dependency."""
    return asyncio.run(coro)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


def _authenticated(manager, customer_id="DEMO001"):
    """Create a session and take it through the real authentication flow."""
    session = manager.create_session()
    assert authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )["success"]
    assert authentication.verify_pin(
        session.session_id, PINS[customer_id], manager=manager
    )["success"]
    return session


def _context(session, manager):
    return BankingRealtimeContext(session_id=session.session_id, manager=manager)


async def _call(tool, context: BankingRealtimeContext, **arguments):
    """Invoke a realtime function tool exactly as the SDK would.

    Arguments arrive as a JSON string, which is what the model produces, so a
    field the tool does not declare is rejected here and not by Python.
    """
    payload = json.dumps(arguments)
    tool_context = ToolContext.from_agent_context(
        RunContextWrapper(context),
        tool_call_id="test-call",
        tool_name=tool.name,
        tool_arguments=payload,
    )
    result = await tool.on_invoke_tool(tool_context, payload)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result


# === 1-6, 19: realtime manager lifecycle ====================================


def test_manager_can_start_a_call_on_a_banking_session(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    connection = run(realtime.start(session.session_id))

    assert connection.banking_session_id == session.session_id
    assert connection.realtime_session_id.startswith("REALTIME-")
    assert realtime.is_active(session.session_id)
    assert realtime.active_count() == 1


def test_manager_rejects_a_nonexistent_banking_session(manager):
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.start("SESSION-does-not-exist"))

    assert error.value.reason == Reason.SESSION_NOT_FOUND
    assert realtime.active_count() == 0


def test_a_call_may_start_before_authentication(manager):
    """Verifying the caller by voice is the first thing a call does."""
    session = manager.create_session()
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    connection = run(realtime.start(session.session_id))

    assert connection.realtime_session_id


def test_starting_twice_is_rejected(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(session.session_id))

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.start(session.session_id))

    assert error.value.reason == Reason.REALTIME_ALREADY_ACTIVE
    assert realtime.active_count() == 1


def test_realtime_id_is_recorded_on_the_right_banking_session(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    connection = run(realtime.start(first.session_id))

    assert manager.get_session(first.session_id).realtime_session_id == (
        connection.realtime_session_id
    )
    assert manager.get_session(second.session_id).realtime_session_id is None


def test_realtime_ids_stay_isolated_between_calls(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    one = run(realtime.start(first.session_id))
    two = run(realtime.start(second.session_id))

    assert one.realtime_session_id != two.realtime_session_id
    assert one.session is not two.session
    assert realtime.active_count() == 2


def test_closing_removes_the_association(manager):
    session = _authenticated(manager)
    fake = FakeRealtimeSession()
    realtime = RealtimeManager(connect=fake_connector(fake), manager=manager)
    run(realtime.start(session.session_id))

    closed = run(realtime.close(session.session_id))

    assert closed is True
    assert fake.closed is True
    assert realtime.is_active(session.session_id) is False
    assert manager.get_session(session.session_id).realtime_session_id is None


def test_closing_a_call_does_not_destroy_the_banking_session(manager):
    """Two lifecycles: the call ends, the customer session survives."""
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(session.session_id))

    run(realtime.close(session.session_id))

    still_there = manager.get_session(session.session_id)
    assert still_there is not None
    assert still_there.authenticated is True
    assert still_there.customer_id == "DEMO001"


def test_closing_twice_is_harmless(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(session.session_id))

    assert run(realtime.close(session.session_id)) is True
    assert run(realtime.close(session.session_id)) is False


def test_closing_one_call_leaves_others_running(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(first.session_id))
    run(realtime.start(second.session_id))

    run(realtime.close(first.session_id))

    assert realtime.is_active(second.session_id) is True
    assert manager.get_session(second.session_id).realtime_session_id is not None


def test_connection_failure_leaves_the_banking_session_intact(manager):
    """A provider outage must not corrupt customer state."""
    session = _authenticated(manager)
    realtime = RealtimeManager(
        connect=fake_connector(fail_with=RuntimeError("socket died")), manager=manager
    )

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.start(session.session_id))

    assert error.value.reason == Reason.REALTIME_CONNECTION_FAILED
    after = manager.get_session(session.session_id)
    assert after.authenticated is True
    assert after.customer_id == "DEMO001"
    assert after.realtime_session_id is None
    assert realtime.active_count() == 0


def test_connection_failure_does_not_leak_provider_detail(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(
        connect=fake_connector(fail_with=RuntimeError("api key sk-secret rejected")),
        manager=manager,
    )

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.start(session.session_id))

    assert "sk-secret" not in str(error.value)
    assert "sk-secret" not in json.dumps(error.value.to_dict())


def test_close_all_releases_every_call(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(first.session_id))
    run(realtime.start(second.session_id))

    closed = run(realtime.close_all())

    assert closed == 2
    assert realtime.active_count() == 0
    assert manager.get_session(first.session_id).realtime_session_id is None
    assert manager.get_session(second.session_id).realtime_session_id is None


def test_audio_and_control_reach_the_bound_call(manager):
    session = _authenticated(manager)
    fake = FakeRealtimeSession()
    realtime = RealtimeManager(connect=fake_connector(fake), manager=manager)
    run(realtime.start(session.session_id))

    run(realtime.send_audio(session.session_id, b"\x00\x01"))
    run(realtime.interrupt(session.session_id))

    assert fake.audio_chunks == [b"\x00\x01"]
    assert fake.interrupted == 1


def test_sending_audio_without_a_call_is_refused(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)

    with pytest.raises(RealtimeSessionError) as error:
        run(realtime.send_audio(session.session_id, b"\x00"))

    assert error.value.reason == Reason.REALTIME_NOT_ACTIVE


def test_event_pump_forwards_events_and_stops_on_close(manager):
    session = _authenticated(manager)
    fake = FakeRealtimeSession(
        events=[FakeEvent("agent_start", agent=FakeTool("officer"))]
    )
    realtime = RealtimeManager(connect=fake_connector(fake), manager=manager)
    seen = []

    async def scenario():
        await realtime.start(session.session_id, on_event=lambda sid, e: seen.append(e))
        await asyncio.sleep(0.05)
        connection = realtime.get(session.session_id)
        await realtime.close(session.session_id)
        return connection

    connection = run(scenario())

    assert [event.type for event in seen] == ["agent_start"]
    assert connection.pump is None
    assert fake.closed is True


def test_a_call_with_no_event_stream_starts_no_pump(manager):
    """A browser call carries its own events; there is nothing here to drain.

    The pump exists to feed the scope gate from the provider's event stream, so
    it runs whether or not a handler was passed. A browser-hosted call has no
    stream on this side — its turns are classified through POST /api/call/scope
    — and starting a pump on one would spawn a task per call whose only possible
    outcome is a TypeError in the log.
    """

    class NoStream:
        """A provider session that is not iterable, like a browser call."""

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    stub = NoStream()
    realtime = RealtimeManager(connect=fake_connector(stub), manager=manager)
    session = _authenticated(manager)

    connection = run(realtime.start(session.session_id))

    assert connection.pump is None
    # The call is still a real, closeable call in every other respect.
    assert realtime.active_count() == 1
    assert run(realtime.close(session.session_id)) is True
    assert stub.closed is True


# === 7-8, 12-13: banking tools through the realtime wrapper =================


def test_account_tool_runs_through_the_realtime_wrapper(manager):
    session = _authenticated(manager, "DEMO001")

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
        )
    )

    assert result["success"] is True
    assert result["available_balance"] == "12450.75"
    assert result["masked_account"] == "XXXX1001"


def test_loan_tool_runs_through_the_realtime_wrapper(manager):
    session = _authenticated(manager, "DEMO001")

    result = run(
        _call(
            realtime_tools.get_loan_balance,
            _context(session, manager),
            loan_type="Home Loan",
        )
    )

    assert result["success"] is True
    assert result["outstanding_balance"] == "284500.00"


def test_transactions_tool_respects_the_requested_limit(manager):
    session = _authenticated(manager, "DEMO001")

    result = run(
        _call(
            realtime_tools.get_recent_transactions,
            _context(session, manager),
            account_type="Savings",
            limit=2,
        )
    )

    assert result["success"] is True
    assert len(result["transactions"]) == 2


def test_instalment_tool_returns_seeded_values(manager):
    session = _authenticated(manager, "DEMO002")

    result = run(
        _call(
            realtime_tools.get_next_instalment,
            _context(session, manager),
            loan_type="Personal Loan",
        )
    )

    assert result["next_instalment_amount"] == "620.00"
    assert result["next_instalment_date"] == "2026-09-12"


def test_two_calls_read_their_own_customers(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")

    one = run(
        _call(
            realtime_tools.get_account_balance,
            _context(first, manager),
            account_type="Savings",
        )
    )
    two = run(
        _call(
            realtime_tools.get_account_balance,
            _context(second, manager),
            account_type="Savings",
        )
    )

    assert one["masked_account"] == "XXXX1001"
    assert two["masked_account"] == "XXXX1002"


# === 9-10: the model cannot choose an identity ==============================


def test_no_realtime_tool_declares_an_identity_argument():
    """The heart of Phase 9: identity is not something the model can say."""
    for tool in realtime_tools.BANKING_TOOLS:
        declared = set(tool.params_json_schema.get("properties", {}))
        assert FORBIDDEN_ARGUMENTS.isdisjoint(declared), tool.name


def test_context_parameter_is_hidden_from_the_model():
    """The SDK excludes the run context, so it cannot be supplied or forged."""
    for tool in realtime_tools.BANKING_TOOLS:
        assert "context" not in tool.params_json_schema.get("properties", {})


def test_a_customer_id_argument_is_ignored_not_honoured(manager):
    """A hallucinated customer_id does not switch customer.

    The SDK validates arguments against the tool's schema and drops anything
    the tool did not declare, so the extra field never reaches the wrapper. The
    answer is still the bound customer's — which is the property that matters.
    """
    session = _authenticated(manager, "DEMO001")

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
            customer_id="DEMO002",
        )
    )

    assert result["masked_account"] == "XXXX1001"
    assert result["available_balance"] == "12450.75"
    # Nothing belonging to DEMO002 came back.
    assert "8730.20" not in json.dumps(result)
    assert "XXXX1002" not in json.dumps(result)


def test_a_session_id_argument_cannot_repoint_the_call(manager):
    session = _authenticated(manager, "DEMO001")
    other = _authenticated(manager, "DEMO002")

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
            session_id=other.session_id,
        )
    )

    assert result["masked_account"] == "XXXX1001"
    assert "8730.20" not in json.dumps(result)


def test_the_wrapper_only_ever_forwards_declared_arguments(manager):
    """Second line of defence: the Phase 8 registry rejects an identity too."""
    from app.agents.registry import dispatch

    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"account_type": "Savings", "customer_id": "DEMO002"},
        manager=manager,
    )

    assert result["success"] is False
    assert result["reason"] == "UNKNOWN_ARGUMENT"


def test_the_binding_context_is_frozen(manager):
    """A tool cannot repoint a live call at another session."""
    session = _authenticated(manager, "DEMO001")
    context = _context(session, manager)

    with pytest.raises(Exception):
        context.session_id = "SESSION-somebody-else"


# === 11, 17, 18: no data without a valid, authenticated session =============


def test_unauthenticated_call_cannot_read_an_account(manager):
    session = manager.create_session()

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
        )
    )

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_unauthenticated_call_cannot_read_a_loan(manager):
    session = manager.create_session()

    result = run(
        _call(
            realtime_tools.get_loan_balance,
            _context(session, manager),
            loan_type="Home Loan",
        )
    )

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_identified_but_unverified_call_cannot_read_data(manager):
    """Saying a customer ID is not authentication; the PIN still decides."""
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
        )
    )

    assert result["reason"] == "NOT_AUTHENTICATED"


def test_locked_authentication_cannot_reach_banking_data(manager):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, "0000", manager=manager)

    result = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
        )
    )

    assert result["reason"] == "AUTHENTICATION_LOCKED"


def test_the_model_cannot_unlock_a_locked_session(manager):
    """Retrying through the tool keeps the backend's locked verdict."""
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, "0000", manager=manager)

    result = run(
        _call(
            realtime_tools.submit_pin,
            _context(session, manager),
            spoken_pin="four eight two one",
        )
    )

    assert result["authenticated"] is False
    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert manager.get_session(session.session_id).authenticated is False


def test_destroyed_banking_session_ends_realtime_banking_access(manager):
    session = _authenticated(manager, "DEMO001")
    context = _context(session, manager)
    assert run(_call(realtime_tools.get_account_balance, context,
                     account_type="Savings"))["success"] is True

    manager.destroy_session(session.session_id)

    result = run(
        _call(realtime_tools.get_account_balance, context, account_type="Savings")
    )
    assert result == {"success": False, "reason": "SESSION_NOT_FOUND"}


def test_closing_a_call_for_a_destroyed_session_is_clean(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(connect=fake_connector(), manager=manager)
    run(realtime.start(session.session_id))
    manager.destroy_session(session.session_id)

    assert run(realtime.close(session.session_id)) is True
    assert realtime.active_count() == 0


# === 14-15: follow-up context survives, shared with Phase 8 =================


def test_account_follow_up_keeps_the_chosen_account(manager):
    session = _authenticated(manager, "DEMO001")
    context = _context(session, manager)
    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    # The caller does not say "savings" again.
    result = run(_call(realtime_tools.get_recent_transactions, context))

    assert result["success"] is True
    assert result["masked_account"] == "XXXX1001"


def test_loan_follow_up_keeps_the_chosen_loan(manager):
    session = _authenticated(manager, "DEMO003")
    context = _context(session, manager)
    run(_call(realtime_tools.get_loan_balance, context, loan_type="Home Loan"))

    result = run(_call(realtime_tools.get_next_instalment, context))

    assert result["success"] is True
    assert result["loan_type"] == "Home Loan"


def test_a_named_account_overrides_the_carried_one(manager):
    session = _authenticated(manager, "DEMO001")
    context = _context(session, manager)
    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    result = run(
        _call(realtime_tools.get_account_balance, context, account_type="Current")
    )

    assert result["masked_account"] == "XXXX2001"


def test_selection_does_not_carry_across_domains(manager):
    session = _authenticated(manager, "DEMO003")
    context = _context(session, manager)
    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    result = run(_call(realtime_tools.get_loan_balance, context))

    assert result["success"] is False
    assert result["reason"] == "LOAN_TYPE_REQUIRED"


def test_ambiguous_account_asks_rather_than_guesses(manager):
    session = _authenticated(manager, "DEMO001")

    result = run(_call(realtime_tools.get_account_balance, _context(session, manager)))

    assert result["reason"] == "ACCOUNT_TYPE_REQUIRED"
    assert set(result["available_account_types"]) == {"Savings", "Current"}


# === authentication through the realtime wrapper ============================


def test_spoken_customer_id_is_normalised_and_verified(manager):
    session = manager.create_session()

    result = run(
        _call(
            realtime_tools.submit_customer_id,
            _context(session, manager),
            spoken_customer_id="demo zero zero one",
        )
    )

    assert result["success"] is True
    assert result["customer_id"] == "DEMO001"
    assert result["next_step"] == "PIN"
    # Identified, not yet authenticated.
    assert manager.get_session(session.session_id).authenticated is False


def test_spoken_pin_authenticates_through_backend_logic(manager):
    session = manager.create_session()
    context = _context(session, manager)
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="demo zero zero one"))

    result = run(
        _call(realtime_tools.submit_pin, context, spoken_pin="four eight two one")
    )

    assert result["success"] is True
    assert result["authenticated"] is True
    assert manager.get_session(session.session_id).authenticated is True


def test_a_wrong_spoken_pin_counts_down_attempts(manager):
    session = manager.create_session()
    context = _context(session, manager)
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="demo zero zero one"))

    result = run(_call(realtime_tools.submit_pin, context, spoken_pin="zero zero zero zero"))

    assert result["authenticated"] is False
    # One failure for every kind of wrong credential, so the reply cannot say
    # which part was wrong.
    assert result["reason"] == "INVALID_CREDENTIALS"
    assert result["attempts_remaining"] == 2


def test_three_wrong_pins_lock_the_session(manager):
    session = manager.create_session()
    context = _context(session, manager)
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="demo zero zero one"))

    for _ in range(3):
        result = run(_call(realtime_tools.submit_pin, context, spoken_pin="one one one one"))

    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert manager.get_session(session.session_id).authentication_locked is True


def test_authentication_status_carries_no_credential(manager):
    session = _authenticated(manager, "DEMO001")

    result = run(_call(realtime_tools.get_authentication_status, _context(session, manager)))

    assert result["authenticated"] is True
    body = json.dumps(result)
    assert "pin" not in body.lower() or "pin_hash" not in body
    assert PINS["DEMO001"] not in body


# === 16: unsupported requests have no tool to reach for =====================


def test_the_tool_surface_covers_only_authentication_accounts_and_loans():
    names = {tool.name for tool in realtime_tools.BANKING_TOOLS}

    assert names == {
        "submit_customer_id",
        "submit_pin",
        "get_authentication_status",
        "get_account_balance",
        "get_account_details",
        "get_recent_transactions",
        "get_loan_balance",
        "get_loan_details",
        "get_next_instalment",
    }


def test_no_tool_exists_for_an_unsupported_banking_action():
    """There is no transfer, payment or card tool to call, whatever is asked."""
    names = " ".join(tool.name for tool in realtime_tools.BANKING_TOOLS)

    for forbidden in ("transfer", "payment", "pay_", "card", "beneficiary",
                      "block", "address", "invest", "insurance", "fraud"):
        assert forbidden not in names


def test_no_realtime_tool_writes_to_the_database():
    """Every tool is an enquiry; none performs a transaction."""
    for tool in realtime_tools.BANKING_TOOLS:
        assert tool.name.startswith(("get_", "submit_"))


# === 21: PIN privacy ========================================================


def test_pin_arguments_are_redacted_in_event_summaries():
    event = FakeEvent(
        "tool_start",
        tool=FakeTool("submit_pin"),
        arguments='{"spoken_pin": "four eight two one"}',
        agent=FakeTool("officer"),
    )

    summary = realtime_events.describe_event(event)

    assert summary["arguments"] == realtime_events.REDACTED
    assert "four eight two one" not in json.dumps(summary)


def test_non_secret_tool_arguments_are_kept_for_debugging():
    event = FakeEvent(
        "tool_start",
        tool=FakeTool("get_account_balance"),
        arguments='{"account_type": "Savings"}',
        agent=FakeTool("officer"),
    )

    assert "Savings" in realtime_events.describe_event(event)["arguments"]


def test_tool_end_records_only_whether_it_succeeded():
    """Balances must not be copied into log lines."""
    event = FakeEvent(
        "tool_end",
        tool=FakeTool("get_account_balance"),
        arguments="{}",
        output='{"success": true, "available_balance": "12450.75"}',
        agent=FakeTool("officer"),
    )

    summary = realtime_events.describe_event(event)

    assert summary["ok"] is True
    assert "12450.75" not in json.dumps(summary)


def test_the_pin_never_reaches_the_log(manager, caplog):
    session = manager.create_session()
    context = _context(session, manager)
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="demo zero zero one"))

    with caplog.at_level(logging.DEBUG):
        run(_call(realtime_tools.submit_pin, context, spoken_pin="four eight two one"))

    assert "four eight two one" not in caplog.text
    assert PINS["DEMO001"] not in caplog.text


def test_the_pin_is_not_returned_by_the_authentication_tool(manager):
    session = manager.create_session()
    context = _context(session, manager)
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="demo zero zero one"))

    result = run(_call(realtime_tools.submit_pin, context, spoken_pin="four eight two one"))

    assert PINS["DEMO001"] not in json.dumps(result)


def test_the_pin_is_not_held_on_the_session(manager):
    session = _authenticated(manager, "DEMO001")

    body = json.dumps(manager.get_session(session.session_id).to_safe_dict())

    assert PINS["DEMO001"] not in body


# === agent configuration ====================================================


def test_the_agent_is_built_with_the_controlled_tool_surface():
    agent = build_banking_agent()

    assert {tool.name for tool in agent.tools} == {
        tool.name for tool in realtime_tools.BANKING_TOOLS
    }


def test_the_agent_instructions_carry_the_required_rules():
    lowered = INSTRUCTIONS.lower()

    assert "never invent financial values" in lowered
    assert "source of truth" in lowered
    assert "account and loan enquiries only" in lowered
    assert "never repeat the pin" in lowered
    assert "do not perform transactions" in lowered


def test_model_settings_use_the_configured_model_and_barge_in():
    config = model_settings()

    assert config["model_name"] == settings.realtime_model
    turn_detection = config["audio"]["input"]["turn_detection"]
    assert turn_detection["interrupt_response"] is True
    assert config["audio"]["output"]["voice"] == settings.realtime_voice


def test_tracing_is_disabled_so_no_pin_is_uploaded():
    assert run_config()["tracing_disabled"] is True


def test_the_agent_config_holds_no_api_key():
    body = json.dumps(run_config(), default=str)

    assert "api_key" not in body
    if settings.openai_api_key:
        assert settings.openai_api_key not in body


# === 20: the API key never leaves the backend ===============================


def test_realtime_status_endpoint_reports_without_the_key(client):
    response = client.get("/dev/realtime/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"configured", "model", "voice", "active_calls"}
    assert isinstance(body["configured"], bool)
    if settings.openai_api_key:
        assert settings.openai_api_key not in response.text


def test_no_development_endpoint_returns_the_api_key(client):
    session_id = client.post("/dev/sessions").json()["session_id"]
    key = settings.openai_api_key

    responses = [
        client.get("/health"),
        client.get("/"),
        client.get("/openapi.json"),
        client.get("/dev/realtime/status"),
        client.get(f"/dev/realtime/session/{session_id}"),
        client.get("/dev/agents/tools"),
    ]

    for response in responses:
        assert "OPENAI_API_KEY" not in response.text
        if key:
            assert key not in response.text


def test_realtime_session_endpoint_reports_no_active_call(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.get(f"/dev/realtime/session/{session_id}")

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "realtime_session_id": None,
        "active": False,
    }


def test_realtime_session_endpoint_404s_for_an_unknown_session(client):
    response = client.get("/dev/realtime/session/SESSION-nope")

    assert response.status_code == 404


def test_closing_an_inactive_call_is_reported_not_an_error(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.delete(f"/dev/realtime/session/{session_id}")

    assert response.status_code == 200
    assert response.json()["closed"] is False


def test_starting_a_call_on_an_unknown_session_404s(client):
    response = client.post("/dev/realtime/session/SESSION-nope/start")

    assert response.status_code == 404


# === the Phase 8 text mode is untouched =====================================


def test_developer_text_mode_still_answers(client):
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "DEMO002"},
    )
    client.post("/dev/auth/pin", json={"session_id": session_id, "pin": PINS["DEMO002"]})

    response = client.post(
        "/dev/agents/turn",
        json={"session_id": session_id, "text": "what is my savings balance"},
    )

    assert response.status_code == 200
    assert "8,730.20 SGD" in response.json()["speech"]


def test_both_interfaces_read_the_same_value(manager, client):
    """Voice and text are two doors onto one banking core."""
    session = _authenticated(manager, "DEMO002")
    through_realtime = run(
        _call(
            realtime_tools.get_account_balance,
            _context(session, manager),
            account_type="Savings",
        )
    )

    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "DEMO002"},
    )
    client.post("/dev/auth/pin", json={"session_id": session_id, "pin": PINS["DEMO002"]})
    through_text = client.post(
        "/dev/agents/turn",
        json={"session_id": session_id, "text": "what is my savings balance"},
    ).json()

    assert through_realtime["available_balance"] == (
        through_text["data"]["available_balance"]
    )
