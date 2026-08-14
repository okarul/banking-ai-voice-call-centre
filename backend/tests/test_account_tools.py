"""Phase 5 account tool tests.

Values are checked against the Phase 2 synthetic seed data.
"""

import inspect
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import authentication
from app.main import app
from app.sessions import SessionManager
from app.tools import accounts

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {"DEMO001": "4821", "DEMO002": "7315", "DEMO004": "9153"}

# Seeded expectations.
DEMO001_SAVINGS = ("XXXX1001", Decimal("12450.75"))
DEMO001_CURRENT = ("XXXX2001", Decimal("3820.10"))
DEMO002_SAVINGS = ("XXXX1002", Decimal("8730.20"))


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


def _authenticated_via_http(client, customer_id="DEMO001"):
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": customer_id},
    )
    client.post(
        "/dev/auth/pin", json={"session_id": session_id, "pin": PINS[customer_id]}
    )
    return session_id


# --- access control ---------------------------------------------------------


def test_unauthenticated_session_cannot_retrieve_balance(manager):
    session = manager.create_session()

    result = accounts.get_account_balance(
        session.session_id, "Savings", manager=manager
    )

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_identified_but_unverified_session_cannot_retrieve_balance(manager):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)

    result = accounts.get_account_balance(
        session.session_id, "Savings", manager=manager
    )

    assert result["reason"] == "NOT_AUTHENTICATED"


def test_nonexistent_session_cannot_retrieve_balance(manager):
    result = accounts.get_account_balance("SESSION-missing", "Savings", manager=manager)
    assert result == {"success": False, "reason": "SESSION_NOT_FOUND"}


def test_all_tools_reject_unauthenticated_sessions(manager):
    session = manager.create_session()

    for call in (
        lambda: accounts.get_account_balance(session.session_id, manager=manager),
        lambda: accounts.get_account_details(session.session_id, manager=manager),
        lambda: accounts.get_recent_transactions(session.session_id, manager=manager),
    ):
        assert call()["reason"] == "NOT_AUTHENTICATED"


def test_tools_do_not_accept_a_customer_id_parameter():
    """The caller must never be able to name the customer."""
    for tool in (
        accounts.get_account_balance,
        accounts.get_account_details,
        accounts.get_recent_transactions,
    ):
        params = set(inspect.signature(tool).parameters)
        assert "customer_id" not in params
        assert params <= {"session_id", "account_type", "limit", "manager"}


# --- balance ----------------------------------------------------------------


def test_demo001_can_retrieve_own_savings_balance(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, "Savings", manager=manager)

    masked, balance = DEMO001_SAVINGS
    assert result["success"] is True
    assert result["masked_account"] == masked
    assert result["available_balance"] == str(balance)
    assert result["account_type"] == "Savings"
    assert result["currency"] == "SGD"


def test_demo002_can_retrieve_own_balance(manager):
    session = _authenticated(manager, "DEMO002")

    result = accounts.get_account_balance(session.session_id, manager=manager)

    masked, balance = DEMO002_SAVINGS
    assert result["masked_account"] == masked
    assert result["available_balance"] == str(balance)


def test_single_account_customer_needs_no_clarification(manager):
    session = _authenticated(manager, "DEMO002")

    result = accounts.get_account_balance(session.session_id, manager=manager)

    assert result["success"] is True
    assert result["masked_account"] == DEMO002_SAVINGS[0]


def test_multiple_accounts_require_account_type(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, manager=manager)

    assert result["success"] is False
    assert result["reason"] == "ACCOUNT_TYPE_REQUIRED"
    assert sorted(result["available_account_types"]) == ["Current", "Savings"]


@pytest.mark.parametrize("spelling", ["Savings", "savings", "SAVINGS", "  sAvInGs  "])
def test_account_type_matching_is_case_insensitive(manager, spelling):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, spelling, manager=manager)

    assert result["masked_account"] == DEMO001_SAVINGS[0]


def test_current_account_returns_current_data(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, "Current", manager=manager)

    masked, balance = DEMO001_CURRENT
    assert result["masked_account"] == masked
    assert result["available_balance"] == str(balance)


def test_invalid_account_type_returns_account_not_found(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(
        session.session_id, "Investment", manager=manager
    )

    assert result["success"] is False
    assert result["reason"] == "ACCOUNT_NOT_FOUND"
    # No silent substitution of another account.
    assert "masked_account" not in result


def test_balance_is_decimal_exact(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, "Savings", manager=manager)

    assert isinstance(result["available_balance"], str)
    assert Decimal(result["available_balance"]) == DEMO001_SAVINGS[1]
    assert result["available_balance"] == "12450.75"


def test_account_number_is_masked(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_balance(session.session_id, "Savings", manager=manager)

    assert result["masked_account"].startswith("XXXX")
    assert len(result["masked_account"]) == 8
    # No database primary key or raw account identifier leaks out.
    assert "id" not in result
    assert "account_number" not in result


# --- details ----------------------------------------------------------------


def test_account_details_work(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_account_details(session.session_id, "Savings", manager=manager)

    assert result["success"] is True
    assert result["account_type"] == "Savings"
    assert result["masked_account"] == DEMO001_SAVINGS[0]
    assert result["available_balance"] == str(DEMO001_SAVINGS[1])
    assert result["currency"] == "SGD"
    assert result["status"] == "ACTIVE"
    assert set(result) == {
        "success",
        "account_type",
        "masked_account",
        "available_balance",
        "currency",
        "status",
    }


# --- transactions -----------------------------------------------------------


def test_recent_transactions_work(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", manager=manager
    )

    assert result["success"] is True
    assert result["masked_account"] == DEMO001_SAVINGS[0]
    assert len(result["transactions"]) == 3
    first = result["transactions"][0]
    assert set(first) == {"date", "description", "amount", "transaction_type"}
    assert first["transaction_type"] in {"CREDIT", "DEBIT"}


def test_transactions_are_newest_first(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", limit=6, manager=manager
    )

    dates = [t["date"] for t in result["transactions"]]
    assert dates == sorted(dates, reverse=True)


def test_default_transaction_limit_is_three(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", manager=manager
    )

    assert accounts.DEFAULT_TRANSACTION_LIMIT == 3
    assert len(result["transactions"]) == 3


def test_transaction_limit_of_one(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", limit=1, manager=manager
    )

    assert len(result["transactions"]) == 1


def test_transaction_limit_maximum_is_allowed(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", limit=accounts.MAX_TRANSACTION_LIMIT,
        manager=manager,
    )

    # The account holds fewer rows than the maximum, so all of them come back.
    assert result["success"] is True
    assert len(result["transactions"]) == 6


@pytest.mark.parametrize("bad_limit", [0, -1, -50, 11, 1000])
def test_invalid_transaction_limits_are_rejected(manager, bad_limit):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", limit=bad_limit, manager=manager
    )

    assert result["success"] is False
    assert result["reason"] == "INVALID_LIMIT"
    assert result["min_limit"] == 1
    assert result["max_limit"] == 10


def test_transaction_amounts_keep_decimal_precision(manager):
    session = _authenticated(manager, "DEMO001")

    result = accounts.get_recent_transactions(
        session.session_id, "Savings", limit=6, manager=manager
    )

    by_description = {t["description"]: t for t in result["transactions"]}
    salary = by_description["Salary Credit"]
    assert salary["amount"] == "5200.00"
    assert salary["transaction_type"] == "CREDIT"

    groceries = by_description["NTUC FairPrice"]
    assert groceries["amount"] == "-86.40"
    assert groceries["transaction_type"] == "DEBIT"
    assert Decimal(groceries["amount"]) == Decimal("-86.40")


# --- ownership and isolation ------------------------------------------------


def test_two_sessions_receive_their_own_customer_data(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    result_a = accounts.get_account_balance(
        session_a.session_id, "Savings", manager=manager
    )
    result_b = accounts.get_account_balance(
        session_b.session_id, "Savings", manager=manager
    )

    assert result_a["masked_account"] == DEMO001_SAVINGS[0]
    assert result_b["masked_account"] == DEMO002_SAVINGS[0]
    assert result_a["masked_account"] != result_b["masked_account"]
    assert result_a["available_balance"] != result_b["available_balance"]


def test_session_a_cannot_reach_session_b_customer_data(manager):
    session_a = _authenticated(manager, "DEMO001")
    _authenticated(manager, "DEMO002")

    # DEMO002's only account is Savings XXXX1002; DEMO001 must never see it.
    for account_type in (None, "Savings", "Current", "savings"):
        result = accounts.get_account_balance(
            session_a.session_id, account_type, manager=manager
        )
        assert result.get("masked_account") != DEMO002_SAVINGS[0]


def test_transactions_do_not_leak_between_customers(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    a = accounts.get_recent_transactions(
        session_a.session_id, "Savings", limit=6, manager=manager
    )
    b = accounts.get_recent_transactions(
        session_b.session_id, "Savings", limit=6, manager=manager
    )

    assert a["masked_account"] == DEMO001_SAVINGS[0]
    assert b["masked_account"] == DEMO002_SAVINGS[0]
    assert a["transactions"] != b["transactions"]


def test_account_context_does_not_cross_sessions(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    accounts.get_account_balance(session_a.session_id, "Current", manager=manager)

    assert session_a.current_domain == "ACCOUNT"
    assert session_a.conversation_context["account_type"] == "Current"
    assert session_b.conversation_context == {}
    assert session_b.current_domain is None


def test_full_account_flow_for_demo001(manager):
    session = _authenticated(manager, "DEMO001")
    assert session.authenticated is True
    assert session.customer_id == "DEMO001"

    balance = accounts.get_account_balance(session.session_id, "Savings", manager=manager)
    transactions = accounts.get_recent_transactions(
        session.session_id, "Savings", manager=manager
    )
    details = accounts.get_account_details(session.session_id, "Savings", manager=manager)

    for result in (balance, transactions, details):
        assert result["success"] is True
        assert result["masked_account"] == DEMO001_SAVINGS[0]


# --- development endpoints --------------------------------------------------


def test_dev_balance_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(f"/dev/accounts/balance/{session_id}?account_type=Savings")

    assert response.status_code == 200
    body = response.json()
    assert body["masked_account"] == DEMO001_SAVINGS[0]
    assert body["available_balance"] == "12450.75"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_details_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/accounts/details/{session_id}", params={"account_type": "savings"}
    ).json()

    assert body["account_type"] == "Savings"
    assert body["status"] == "ACTIVE"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_transactions_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/accounts/transactions/{session_id}",
        params={"account_type": "Savings", "limit": 2},
    ).json()

    assert len(body["transactions"]) == 2
    assert body["masked_account"] == DEMO001_SAVINGS[0]

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoints_reject_unauthenticated_sessions(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    for path in ("balance", "details", "transactions"):
        response = client.get(f"/dev/accounts/{path}/{session_id}")
        assert response.status_code == 401
        assert response.json()["detail"]["reason"] == "NOT_AUTHENTICATED"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoints_reject_unknown_session(client):
    response = client.get("/dev/accounts/balance/SESSION-missing")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "SESSION_NOT_FOUND"


def test_dev_endpoint_invalid_account_type_returns_404(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(
        f"/dev/accounts/balance/{session_id}", params={"account_type": "Investment"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "ACCOUNT_NOT_FOUND"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoint_asks_for_account_type_when_ambiguous(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(f"/dev/accounts/balance/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "ACCOUNT_TYPE_REQUIRED"
    assert sorted(body["available_account_types"]) == ["Current", "Savings"]

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoint_rejects_invalid_limit(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(
        f"/dev/accounts/transactions/{session_id}",
        params={"account_type": "Savings", "limit": 0},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "INVALID_LIMIT"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_account_endpoints_take_no_customer_id(client):
    """A customer_id query parameter must not influence the result."""
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/accounts/balance/{session_id}",
        params={"account_type": "Savings", "customer_id": "DEMO002"},
    ).json()

    assert body["masked_account"] == DEMO001_SAVINGS[0]

    schema = client.get("/openapi.json").json()
    for path in (
        "/dev/accounts/balance/{session_id}",
        "/dev/accounts/details/{session_id}",
        "/dev/accounts/transactions/{session_id}",
    ):
        names = {p["name"] for p in schema["paths"][path]["get"].get("parameters", [])}
        assert "customer_id" not in names

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_two_customers_stay_isolated(client):
    session_a = _authenticated_via_http(client, "DEMO001")
    session_b = _authenticated_via_http(client, "DEMO002")

    body_a = client.get(
        f"/dev/accounts/balance/{session_a}", params={"account_type": "Savings"}
    ).json()
    body_b = client.get(f"/dev/accounts/balance/{session_b}").json()

    assert body_a["masked_account"] == DEMO001_SAVINGS[0]
    assert body_b["masked_account"] == DEMO002_SAVINGS[0]

    client.delete(f"/dev/sessions/{session_a}")
    client.delete(f"/dev/sessions/{session_b}")


def test_dev_account_responses_expose_no_credentials(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    bodies = [
        client.get(f"/dev/accounts/balance/{session_id}?account_type=Savings").text,
        client.get(f"/dev/accounts/details/{session_id}?account_type=Savings").text,
        client.get(f"/dev/accounts/transactions/{session_id}?account_type=Savings").text,
    ]

    for body in bodies:
        lowered = body.lower()
        assert "pin" not in lowered
        assert "hash" not in lowered
        assert "pbkdf2" not in lowered

    client.delete(f"/dev/sessions/{session_id}")


def test_health_endpoint_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
