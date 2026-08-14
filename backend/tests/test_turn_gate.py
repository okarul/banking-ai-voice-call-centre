"""Phase 11: the scope gate in front of the banking-data tools.

`test_banking_scope.py` proves `classify_scope` reaches the right verdict.
These tests prove the verdict is *enforced* — that a refused turn reaches no
banking tool at all, rather than being fetched and then declined out loud.

The distinction is the whole point. A cross-customer comparison that answers
"I can only access information for the verified customer" after reading the
caller's balance has still read a balance in order to answer a question about
someone else. Under a strict customer-data policy that retrieval must not
happen, so the refusal is asserted at the tool boundary, not in the wording.

Everything here is offline: no OpenAI, no network, no paid usage.
"""

import asyncio
import json

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext
from fastapi.testclient import TestClient

from app.auth import authentication
from app.main import app
from app.realtime import tools as realtime_tools
from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import RealtimeManager, _user_text
from app.realtime.turn_gate import (
    BANKING_DATA_TOOLS,
    GATE_KEY,
    REASON_OUT_OF_SCOPE,
    REASON_UNCLASSIFIED,
    open_turn,
    record_turn,
    refusal_for,
)
from app.scope import ScopeCategory
from app.sessions import SessionManager

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {"DEMO001": "4821", "DEMO002": "7315"}

# DEMO001's own seeded values. If any of these appear in a refused turn's
# result, protected data was read for a question that was not allowed one.
OWN_VALUES = ("12450.75", "3820.10", "284500.00", "XXXX1001", "HL-DEMO001")

# Every way the brief describes a cross-customer comparison.
COMPARISONS = [
    "Who has more in savings, me or DEMO002?",
    "Is DEMO002 richer than me?",
    "Does DEMO002 have more money than I do?",
    "Compare my balance with DEMO002.",
    "Is my balance higher than DEMO002?",
    "Who owes more on their loan, me or DEMO002?",
    "Compare my home loan with DEMO002.",
    "Does DEMO002 have a bigger loan than me?",
    # As the transcriber actually renders a spoken id.
    "Compare my balance with demo002.",
    "Who has more in savings, me or demo002?",
]

PROTECTED_TOOLS = [
    realtime_tools.get_account_balance,
    realtime_tools.get_account_details,
    realtime_tools.get_recent_transactions,
    realtime_tools.get_loan_balance,
    realtime_tools.get_loan_details,
    realtime_tools.get_next_instalment,
]


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


def _authenticated(manager, customer_id="DEMO001"):
    session = manager.create_session()
    assert authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )["success"]
    assert authentication.verify_pin(
        session.session_id, PINS[customer_id], manager=manager
    )["success"]
    return session


async def _call(tool, context: BankingRealtimeContext, **arguments):
    """Invoke a realtime function tool exactly as the SDK would."""
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


def run(coro):
    return asyncio.run(coro)


# --- the gate's own ruling ---------------------------------------------------


@pytest.mark.parametrize("utterance", COMPARISONS)
def test_a_comparison_with_another_customer_is_a_cross_customer_request(
    utterance, manager
):
    session = _authenticated(manager)
    decision = record_turn(session, utterance)

    assert decision is not None
    assert decision.category is ScopeCategory.CROSS_CUSTOMER_REQUEST
    assert decision.allowed is False


@pytest.mark.parametrize("utterance", COMPARISONS)
@pytest.mark.parametrize("tool_name", sorted(BANKING_DATA_TOOLS))
def test_no_banking_data_tool_may_run_on_a_comparison_turn(
    utterance, tool_name, manager
):
    """Not one of the six, for any of the phrasings."""
    session = _authenticated(manager)
    record_turn(session, utterance)

    refusal = refusal_for(session, tool_name)

    assert refusal is not None
    assert refusal["success"] is False
    assert refusal["reason"] == REASON_OUT_OF_SCOPE
    assert refusal["category"] == ScopeCategory.CROSS_CUSTOMER_REQUEST.value


@pytest.mark.parametrize("tool_name", ["submit_customer_id", "submit_pin",
                                       "get_authentication_status"])
def test_authentication_tools_are_never_gated(tool_name, manager):
    """Checking whether the caller is verified reveals nobody's banking."""
    session = _authenticated(manager)
    record_turn(session, "Who has more in savings, me or DEMO002?")

    assert refusal_for(session, tool_name) is None


def test_a_supported_enquiry_is_not_blocked(manager):
    session = _authenticated(manager)
    record_turn(session, "What is my savings balance?")

    for tool_name in sorted(BANKING_DATA_TOOLS):
        assert refusal_for(session, tool_name) is None


def test_an_unclassified_turn_fails_closed(manager):
    """A turn in flight does not get to read a balance on the last one's ruling."""
    session = _authenticated(manager)
    record_turn(session, "What is my savings balance?")
    open_turn(session)  # the caller has started speaking again

    refusal = refusal_for(session, "get_account_balance")

    assert refusal is not None
    assert refusal["reason"] == REASON_UNCLASSIFIED


def test_an_allowed_turn_does_not_authorise_the_next_one(manager):
    """The ruling is per turn, not per call."""
    session = _authenticated(manager)

    record_turn(session, "What is my savings balance?")
    assert refusal_for(session, "get_account_balance") is None

    record_turn(session, "Compare my balance with DEMO002.")
    assert refusal_for(session, "get_account_balance") is not None


def test_the_gate_lives_on_the_session_not_at_module_level(manager):
    """Two callers, two rulings, no crossing."""
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")

    record_turn(first, "Compare my balance with DEMO002.")
    record_turn(second, "What is my savings balance?")

    assert refusal_for(first, "get_account_balance") is not None
    assert refusal_for(second, "get_account_balance") is None
    assert first.conversation_context[GATE_KEY] is not second.conversation_context[GATE_KEY]


def test_the_gate_never_stores_the_caller_words(manager):
    """The transcript is ruled on and discarded — a PIN travels in one."""
    session = _authenticated(manager)
    record_turn(session, "My PIN is four eight two one.")

    stored = json.dumps(session.conversation_context[GATE_KEY].to_safe_dict())
    assert "four" not in stored
    assert "4821" not in stored


# --- the tools themselves ----------------------------------------------------


@pytest.mark.parametrize("utterance", COMPARISONS)
def test_a_comparison_reaches_no_banking_data_through_the_real_tools(
    utterance, manager
):
    """End to end through the tool objects the model actually calls."""
    session = _authenticated(manager)
    record_turn(session, utterance)
    context = BankingRealtimeContext(session_id=session.session_id, manager=manager)

    for tool in PROTECTED_TOOLS:
        result = run(_call(tool, context))

        assert result["success"] is False, tool.name
        assert result["reason"] == REASON_OUT_OF_SCOPE, tool.name
        # Nothing of the caller's own was fetched to make the comparison.
        body = json.dumps(result)
        for value in OWN_VALUES:
            assert value not in body, f"{tool.name} leaked {value}"


@pytest.mark.parametrize("utterance", COMPARISONS)
def test_a_comparison_leaves_the_session_untouched(utterance, manager):
    session = _authenticated(manager)
    record_turn(session, utterance)
    context = BankingRealtimeContext(session_id=session.session_id, manager=manager)

    run(_call(realtime_tools.get_account_balance, context))

    live = manager.get_session(session.session_id)
    assert live.customer_id == "DEMO001"
    assert live.authenticated is True
    assert live.authentication_locked is False


def test_a_supported_enquiry_still_answers_through_the_real_tools(manager):
    """The gate must not cost a legitimate caller their answer."""
    session = _authenticated(manager)
    record_turn(session, "What is my savings balance?")
    context = BankingRealtimeContext(session_id=session.session_id, manager=manager)

    result = run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    assert result["success"] is True
    assert result["available_balance"] == "12450.75"


def test_a_refused_turn_returns_a_generic_sentence(manager):
    """One sentence for every refusal — it distinguishes nobody."""
    session = _authenticated(manager)
    record_turn(session, "Who has more in savings, me or DEMO002?")
    context = BankingRealtimeContext(session_id=session.session_id, manager=manager)

    real = run(_call(realtime_tools.get_account_balance, context))

    invented = _authenticated(manager)
    record_turn(invented, "Who has more in savings, me or DEMO999?")
    invented_context = BankingRealtimeContext(
        session_id=invented.session_id, manager=manager
    )
    fake = run(_call(realtime_tools.get_account_balance, invented_context))

    # A real customer id and an invented one are refused identically, so
    # nothing about who banks here can be learned by asking.
    assert real == fake


# --- the event pump feeds the gate -------------------------------------------


class _Item:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _Entry:
    def __init__(self, transcript=None, text=None):
        self.transcript = transcript
        self.text = text


def test_user_text_reads_a_spoken_transcript():
    item = _Item("user", [_Entry(transcript="Compare my balance with DEMO002.")])
    assert _user_text(item) == "Compare my balance with DEMO002."


def test_user_text_reads_typed_text():
    item = _Item("user", [_Entry(text="What is my savings balance?")])
    assert _user_text(item) == "What is my savings balance?"


def test_user_text_is_empty_when_the_transcript_has_not_arrived():
    assert _user_text(_Item("user", [])) == ""
    assert _user_text(_Item("user", None)) == ""


class _Raw:
    def __init__(self, type_, transcript=None, data=None):
        self.type = type_
        self.transcript = transcript
        self.data = data


class _Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


def test_the_pump_classifies_a_spoken_turn(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(manager=manager)

    realtime._feed_gate(
        session.session_id,
        _Event(
            "raw_model_event",
            data=_Raw(
                "input_audio_transcription_completed",
                transcript="Compare my balance with DEMO002.",
            ),
        ),
    )

    assert refusal_for(session, "get_account_balance") is not None


def test_the_pump_opens_a_new_turn_when_the_caller_starts_speaking(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(manager=manager)
    record_turn(session, "What is my savings balance?")

    realtime._feed_gate(
        session.session_id,
        _Event(
            "raw_model_event",
            data=_Raw("raw_server_event",
                      data={"type": "input_audio_buffer.speech_started"}),
        ),
    )

    refusal = refusal_for(session, "get_account_balance")
    assert refusal is not None
    assert refusal["reason"] == REASON_UNCLASSIFIED


def test_the_pump_classifies_a_typed_turn(manager):
    session = _authenticated(manager)
    realtime = RealtimeManager(manager=manager)

    realtime._feed_gate(
        session.session_id,
        _Event("history_added",
               item=_Item("user", [_Entry(text="Is DEMO002 richer than me?")])),
    )

    assert refusal_for(session, "get_account_balance") is not None


def test_the_pump_ignores_an_assistant_turn(manager):
    """Only the caller sets scope. The model does not classify itself."""
    session = _authenticated(manager)
    realtime = RealtimeManager(manager=manager)
    record_turn(session, "What is my savings balance?")

    realtime._feed_gate(
        session.session_id,
        _Event("history_added",
               item=_Item("assistant", [_Entry(transcript="Compare with DEMO002")])),
    )

    assert refusal_for(session, "get_account_balance") is None


def test_a_broken_event_does_not_open_the_gate(manager):
    """A malformed event must never be read as permission."""
    session = _authenticated(manager)
    open_turn(session)

    realtime = RealtimeManager(manager=manager)
    realtime._feed_gate(session.session_id, object())

    refusal = refusal_for(session, "get_account_balance")
    assert refusal is not None
    assert refusal["reason"] == REASON_UNCLASSIFIED


def test_the_pump_tolerates_a_session_that_has_ended(manager):
    realtime = RealtimeManager(manager=manager)
    realtime._feed_gate("SESSION-does-not-exist", _Event("history_added", item=None))


# --- the browser path shares the same gate -----------------------------------


def test_the_browser_scope_endpoint_records_the_ruling(client):
    """A page that asks the gate also arms it, so /tool cannot bypass it."""
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post("/dev/auth/customer",
                json={"session_id": session_id, "customer_id": "DEMO001"})
    client.post("/dev/auth/pin", json={"session_id": session_id, "pin": PINS["DEMO001"]})

    scoped = client.post("/api/call/scope", json={
        "session_id": session_id,
        "transcript": "Who has more in savings, me or DEMO002?",
    })
    assert scoped.json()["category"] == ScopeCategory.CROSS_CUSTOMER_REQUEST.value

    answered = client.post("/api/call/tool", json={
        "session_id": session_id,
        "name": "get_account_balance",
        "arguments": {"account_type": "Savings"},
    })

    result = answered.json()["result"]
    assert result["success"] is False
    assert result["reason"] == REASON_OUT_OF_SCOPE
    assert "12450.75" not in json.dumps(result)

    client.post("/api/call/end", json={"session_id": session_id})


def test_the_browser_path_still_answers_a_supported_enquiry(client):
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post("/dev/auth/customer",
                json={"session_id": session_id, "customer_id": "DEMO001"})
    client.post("/dev/auth/pin", json={"session_id": session_id, "pin": PINS["DEMO001"]})

    client.post("/api/call/scope", json={
        "session_id": session_id, "transcript": "What is my savings balance?",
    })
    answered = client.post("/api/call/tool", json={
        "session_id": session_id,
        "name": "get_account_balance",
        "arguments": {"account_type": "Savings"},
    })

    result = answered.json()["result"]
    assert result["success"] is True
    assert result["available_balance"] == "12450.75"

    client.post("/api/call/end", json={"session_id": session_id})
