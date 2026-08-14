"""Phase 8 tool registry tests.

The registry's job is to make one rule impossible to break: the caller of a
tool never chooses whose data is read. Most of what follows is that rule from a
different angle — a hallucinated `customer_id`, a smuggled `session_id`, a
wrong type — plus a check that the declared surface is exactly the six tools.
"""

import pytest

from app.agents import registry
from app.agents.registry import FORBIDDEN_ARGUMENTS, TOOL_SPECS, ToolError, dispatch
from app.auth import authentication
from app.sessions import SessionManager
from app.tools import accounts, loans

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {"DEMO001": "4821", "DEMO002": "7315"}

EXPECTED_TOOLS = {
    "get_account_balance": accounts.get_account_balance,
    "get_account_details": accounts.get_account_details,
    "get_recent_transactions": accounts.get_recent_transactions,
    "get_loan_balance": loans.get_loan_balance,
    "get_loan_details": loans.get_loan_details,
    "get_next_instalment": loans.get_next_instalment,
}


@pytest.fixture
def manager():
    return SessionManager()


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


# --- the declared surface ---------------------------------------------------


def test_registry_exposes_exactly_the_six_banking_tools():
    assert set(registry.tool_names()) == set(EXPECTED_TOOLS)


def test_each_spec_points_at_the_real_tool_function():
    for name, function in EXPECTED_TOOLS.items():
        assert registry.TOOLS[name].function is function


def test_no_schema_declares_an_identity_argument():
    """The heart of the registry: identity is never a parameter."""
    for spec in TOOL_SPECS:
        properties = set(spec.parameters["properties"])
        assert FORBIDDEN_ARGUMENTS.isdisjoint(properties), spec.name


def test_every_schema_refuses_undeclared_properties():
    for spec in TOOL_SPECS:
        assert spec.parameters["additionalProperties"] is False, spec.name
        assert spec.parameters["type"] == "object"


def test_no_argument_is_required():
    """Every parameter is optional, so a tool can always be called with none."""
    for spec in TOOL_SPECS:
        assert spec.parameters["required"] == []


def test_tool_schemas_are_serialisable_descriptions():
    schemas = registry.tool_schemas()

    assert len(schemas) == len(TOOL_SPECS)
    for schema in schemas:
        assert set(schema) == {"name", "description", "parameters"}
        assert schema["description"]


# --- rejecting arguments that would choose an identity ----------------------


def test_customer_id_argument_is_rejected(manager):
    """A caller cannot name a customer, even its own."""
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"customer_id": "DEMO002"},
        manager=manager,
    )

    assert result["success"] is False
    assert result["reason"] == ToolError.UNKNOWN_ARGUMENT
    assert result["argument"] == "customer_id"
    # Nothing about any account was returned.
    assert "available_balance" not in result


def test_session_id_argument_is_rejected(manager):
    """A session id in the arguments cannot displace the real one."""
    other = _authenticated(manager, "DEMO002")
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"session_id": other.session_id},
        manager=manager,
    )

    assert result["success"] is False
    assert result["reason"] == ToolError.UNKNOWN_ARGUMENT
    assert result["argument"] == "session_id"


def test_manager_argument_is_rejected(manager):
    """The session store is not something a caller may swap out."""
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"manager": SessionManager()},
        manager=manager,
    )

    assert result["success"] is False
    assert result["reason"] == ToolError.UNKNOWN_ARGUMENT


def test_unknown_argument_lists_the_accepted_ones(manager):
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_loan_balance", session.session_id, {"nonsense": "x"}, manager=manager
    )

    assert result["reason"] == ToolError.UNKNOWN_ARGUMENT
    assert result["accepted_arguments"] == ["loan_type"]


def test_unknown_tool_is_rejected(manager):
    session = _authenticated(manager, "DEMO001")

    result = dispatch("transfer_money", session.session_id, {}, manager=manager)

    assert result["success"] is False
    assert result["reason"] == ToolError.UNKNOWN_TOOL
    assert "get_account_balance" in result["available_tools"]


# --- argument types ---------------------------------------------------------


def test_wrong_argument_type_is_rejected_before_the_tool_runs(manager):
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_recent_transactions",
        session.session_id,
        {"limit": "three"},
        manager=manager,
    )

    assert result["success"] is False
    assert result["reason"] == ToolError.INVALID_ARGUMENT_TYPE
    assert result["argument"] == "limit"


def test_boolean_is_not_accepted_as_an_integer(manager):
    """`True` is `1` in Python; as a limit it is a caller error."""
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_recent_transactions",
        session.session_id,
        {"limit": True},
        manager=manager,
    )

    assert result["reason"] == ToolError.INVALID_ARGUMENT_TYPE


def test_null_is_accepted_where_a_type_is_optional(manager):
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"account_type": None},
        manager=manager,
    )

    # DEMO001 holds two accounts, so the tool asks which one — it was reached.
    assert result["reason"] == "ACCOUNT_TYPE_REQUIRED"


def test_range_checking_is_left_to_the_tool(manager):
    """The registry checks shape; the tool owns its own limits."""
    session = _authenticated(manager, "DEMO001")

    result = dispatch(
        "get_recent_transactions",
        session.session_id,
        {"account_type": "Savings", "limit": 99},
        manager=manager,
    )

    assert result["reason"] == "INVALID_LIMIT"


# --- dispatch reaches the right session -------------------------------------


def test_dispatch_reads_the_session_it_was_given(manager):
    session = _authenticated(manager, "DEMO002")

    result = dispatch(
        "get_account_balance",
        session.session_id,
        {"account_type": "Savings"},
        manager=manager,
    )

    assert result["success"] is True
    assert result["masked_account"] == "XXXX1002"
    assert result["available_balance"] == "8730.20"


def test_dispatch_on_an_unauthenticated_session_is_refused(manager):
    session = manager.create_session()

    result = dispatch("get_account_balance", session.session_id, {}, manager=manager)

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_two_sessions_dispatching_the_same_tool_stay_separate(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")

    one = dispatch(
        "get_account_balance",
        first.session_id,
        {"account_type": "Savings"},
        manager=manager,
    )
    two = dispatch(
        "get_account_balance",
        second.session_id,
        {"account_type": "Savings"},
        manager=manager,
    )

    assert one["masked_account"] == "XXXX1001"
    assert two["masked_account"] == "XXXX1002"
