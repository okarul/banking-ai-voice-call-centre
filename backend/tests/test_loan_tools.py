"""Phase 6 loan tool tests.

Values are checked against the Phase 2 synthetic seed data.
"""

import inspect
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import authentication
from app.main import app
from app.sessions import SessionManager
from app.tools import loans

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {
    "DEMO001": "4821",
    "DEMO002": "7315",
    "DEMO003": "2648",
    "DEMO005": "6072",
}

# Seeded expectations.
DEMO001_LOAN = {
    "loan_type": "Home Loan",
    "loan_reference": "HL-DEMO001",
    "outstanding_balance": "284500.00",
    "interest_rate": "3.250",
    "next_instalment_amount": "1985.40",
    "next_instalment_date": "2026-09-05",
    "maturity_date": "2041-08-05",
}
DEMO002_LOAN = {
    "loan_type": "Personal Loan",
    "loan_reference": "PL-DEMO002",
    "outstanding_balance": "18400.00",
}


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


def test_unauthenticated_session_cannot_retrieve_loan_balance(manager):
    session = manager.create_session()

    result = loans.get_loan_balance(session.session_id, manager=manager)

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_identified_but_unverified_session_cannot_retrieve_loans(manager):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)

    assert (
        loans.get_loan_balance(session.session_id, manager=manager)["reason"]
        == "NOT_AUTHENTICATED"
    )


def test_nonexistent_session_cannot_retrieve_loan_balance(manager):
    result = loans.get_loan_balance("SESSION-missing", manager=manager)
    assert result == {"success": False, "reason": "SESSION_NOT_FOUND"}


def test_all_loan_tools_reject_unauthenticated_sessions(manager):
    session = manager.create_session()

    for call in (
        lambda: loans.get_loan_balance(session.session_id, manager=manager),
        lambda: loans.get_next_instalment(session.session_id, manager=manager),
        lambda: loans.get_loan_details(session.session_id, manager=manager),
    ):
        assert call()["reason"] == "NOT_AUTHENTICATED"


def test_loan_tools_do_not_accept_a_customer_id_parameter():
    """The caller must never be able to name the customer."""
    for tool in (
        loans.get_loan_balance,
        loans.get_next_instalment,
        loans.get_loan_details,
    ):
        params = set(inspect.signature(tool).parameters)
        assert "customer_id" not in params
        assert params <= {"session_id", "loan_type", "manager"}


# --- balance ----------------------------------------------------------------


def test_demo001_can_retrieve_own_loan_balance(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_balance(session.session_id, manager=manager)

    assert result["success"] is True
    assert result["loan_type"] == DEMO001_LOAN["loan_type"]
    assert result["loan_reference"] == DEMO001_LOAN["loan_reference"]
    assert result["outstanding_balance"] == DEMO001_LOAN["outstanding_balance"]
    assert result["currency"] == "SGD"


def test_demo002_can_retrieve_own_loan_balance(manager):
    session = _authenticated(manager, "DEMO002")

    result = loans.get_loan_balance(session.session_id, manager=manager)

    assert result["loan_type"] == DEMO002_LOAN["loan_type"]
    assert result["loan_reference"] == DEMO002_LOAN["loan_reference"]
    assert result["outstanding_balance"] == DEMO002_LOAN["outstanding_balance"]


def test_single_loan_customer_needs_no_clarification(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_balance(session.session_id, manager=manager)

    assert result["success"] is True


def test_multiple_loans_require_loan_type(manager):
    session = _authenticated(manager, "DEMO003")

    result = loans.get_loan_balance(session.session_id, manager=manager)

    assert result["success"] is False
    assert result["reason"] == "LOAN_TYPE_REQUIRED"
    assert sorted(result["available_loan_types"]) == ["Car Loan", "Home Loan"]


def test_multiple_loan_customer_can_select_each_loan(manager):
    session = _authenticated(manager, "DEMO003")

    car = loans.get_loan_balance(session.session_id, "Car Loan", manager=manager)
    home = loans.get_loan_balance(session.session_id, "Home Loan", manager=manager)

    assert car["loan_reference"] == "CL-DEMO003"
    assert car["outstanding_balance"] == "46200.00"
    assert home["loan_reference"] == "HL-DEMO003"
    assert home["outstanding_balance"] == "512000.00"


@pytest.mark.parametrize(
    "spelling", ["Home Loan", "home loan", "HOME LOAN", "  hOmE lOaN  "]
)
def test_loan_type_matching_is_case_insensitive(manager, spelling):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_balance(session.session_id, spelling, manager=manager)

    assert result["loan_reference"] == DEMO001_LOAN["loan_reference"]


def test_invalid_loan_type_returns_loan_not_found(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_balance(
        session.session_id, "Investment Loan", manager=manager
    )

    assert result["success"] is False
    assert result["reason"] == "LOAN_NOT_FOUND"
    # No silent substitution of another loan.
    assert "loan_reference" not in result


def test_loan_type_the_customer_does_not_hold_is_rejected(manager):
    """DEMO002 holds a Personal Loan, so a Home Loan request must fail."""
    session = _authenticated(manager, "DEMO002")

    result = loans.get_loan_balance(session.session_id, "Home Loan", manager=manager)

    assert result["reason"] == "LOAN_NOT_FOUND"
    assert result["available_loan_types"] == ["Personal Loan"]


def test_loan_reference_is_synthetic(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_balance(session.session_id, manager=manager)

    reference = result["loan_reference"]
    assert reference == "HL-DEMO001"
    assert reference.startswith("HL-DEMO")
    # No database primary key leaks out.
    assert "id" not in result


# --- instalment -------------------------------------------------------------


def test_next_instalment_works(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_next_instalment(session.session_id, manager=manager)

    assert result["success"] is True
    assert result["loan_type"] == DEMO001_LOAN["loan_type"]
    assert result["next_instalment_amount"] == DEMO001_LOAN["next_instalment_amount"]
    assert result["next_instalment_date"] == DEMO001_LOAN["next_instalment_date"]
    assert result["currency"] == "SGD"
    assert set(result) == {
        "success",
        "loan_type",
        "next_instalment_amount",
        "next_instalment_date",
        "currency",
    }


def test_next_instalment_amount_preserves_precision(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_next_instalment(session.session_id, manager=manager)

    assert isinstance(result["next_instalment_amount"], str)
    assert result["next_instalment_amount"] == "1985.40"
    assert Decimal(result["next_instalment_amount"]) == Decimal("1985.40")


# --- details ----------------------------------------------------------------


def test_loan_details_work(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_details(session.session_id, manager=manager)

    assert result["success"] is True
    for field, expected in DEMO001_LOAN.items():
        assert result[field] == expected
    assert result["currency"] == "SGD"
    assert result["status"] == "ACTIVE"
    assert set(result) == {
        "success",
        "loan_type",
        "loan_reference",
        "outstanding_balance",
        "interest_rate",
        "next_instalment_amount",
        "next_instalment_date",
        "maturity_date",
        "currency",
        "status",
    }


def test_outstanding_balance_preserves_precision(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_details(session.session_id, manager=manager)

    assert isinstance(result["outstanding_balance"], str)
    assert result["outstanding_balance"] == "284500.00"
    assert Decimal(result["outstanding_balance"]) == Decimal("284500.00")


def test_interest_rate_preserves_precision(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_details(session.session_id, manager=manager)

    assert isinstance(result["interest_rate"], str)
    assert result["interest_rate"] == "3.250"
    assert Decimal(result["interest_rate"]) == Decimal("3.250")


def test_dates_are_iso_formatted(manager):
    session = _authenticated(manager, "DEMO001")

    result = loans.get_loan_details(session.session_id, manager=manager)

    assert result["next_instalment_date"] == "2026-09-05"
    assert result["maturity_date"] == "2041-08-05"


# --- ownership and isolation ------------------------------------------------


def test_two_sessions_receive_their_own_loan_data(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    result_a = loans.get_loan_balance(session_a.session_id, manager=manager)
    result_b = loans.get_loan_balance(session_b.session_id, manager=manager)

    assert result_a["loan_reference"] == DEMO001_LOAN["loan_reference"]
    assert result_b["loan_reference"] == DEMO002_LOAN["loan_reference"]
    assert result_a["loan_reference"] != result_b["loan_reference"]
    assert result_a["outstanding_balance"] != result_b["outstanding_balance"]


def test_session_a_cannot_reach_session_b_loan_data(manager):
    session_a = _authenticated(manager, "DEMO001")
    _authenticated(manager, "DEMO002")

    for loan_type in (None, "Personal Loan", "Home Loan", "personal loan"):
        result = loans.get_loan_balance(
            session_a.session_id, loan_type, manager=manager
        )
        assert result.get("loan_reference") != DEMO002_LOAN["loan_reference"]


def test_instalment_and_details_do_not_leak_between_customers(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    instalment_a = loans.get_next_instalment(session_a.session_id, manager=manager)
    instalment_b = loans.get_next_instalment(session_b.session_id, manager=manager)
    details_a = loans.get_loan_details(session_a.session_id, manager=manager)
    details_b = loans.get_loan_details(session_b.session_id, manager=manager)

    assert instalment_a["next_instalment_amount"] == "1985.40"
    assert instalment_b["next_instalment_amount"] == "620.00"
    assert details_a["loan_reference"] == "HL-DEMO001"
    assert details_b["loan_reference"] == "PL-DEMO002"


def test_loan_context_does_not_cross_sessions(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    loans.get_loan_balance(session_a.session_id, manager=manager)

    assert session_a.current_domain == "LOAN"
    assert session_a.conversation_context["loan_type"] == "Home Loan"
    assert session_b.conversation_context == {}
    assert session_b.current_domain is None


def test_loan_context_records_the_selected_loan(manager):
    session = _authenticated(manager, "DEMO003")

    loans.get_loan_balance(session.session_id, "Car Loan", manager=manager)
    assert session.conversation_context["loan_type"] == "Car Loan"

    loans.get_loan_balance(session.session_id, "Home Loan", manager=manager)
    assert session.conversation_context["loan_type"] == "Home Loan"
    assert session.current_domain == "LOAN"


def test_full_loan_flow_for_demo001(manager):
    session = _authenticated(manager, "DEMO001")
    assert session.authenticated is True
    assert session.customer_id == "DEMO001"

    balance = loans.get_loan_balance(session.session_id, manager=manager)
    instalment = loans.get_next_instalment(session.session_id, manager=manager)
    details = loans.get_loan_details(session.session_id, manager=manager)

    for result in (balance, instalment, details):
        assert result["success"] is True
        assert result["loan_type"] == "Home Loan"
    assert balance["loan_reference"] == details["loan_reference"] == "HL-DEMO001"


# --- development endpoints --------------------------------------------------


def test_dev_loan_balance_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(f"/dev/loans/balance/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["loan_reference"] == "HL-DEMO001"
    assert body["outstanding_balance"] == "284500.00"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_instalment_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/loans/instalment/{session_id}", params={"loan_type": "home loan"}
    ).json()

    assert body["next_instalment_amount"] == "1985.40"
    assert body["next_instalment_date"] == "2026-09-05"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_loan_details_endpoint_works(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/loans/details/{session_id}", params={"loan_type": "HOME LOAN"}
    ).json()

    assert body["interest_rate"] == "3.250"
    assert body["maturity_date"] == "2041-08-05"
    assert body["status"] == "ACTIVE"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_loan_endpoints_reject_unauthenticated_sessions(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    for path in ("balance", "instalment", "details"):
        response = client.get(f"/dev/loans/{path}/{session_id}")
        assert response.status_code == 401
        assert response.json()["detail"]["reason"] == "NOT_AUTHENTICATED"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_loan_endpoints_reject_unknown_session(client):
    response = client.get("/dev/loans/balance/SESSION-missing")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "SESSION_NOT_FOUND"


def test_dev_loan_endpoint_invalid_loan_type_returns_404(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(
        f"/dev/loans/balance/{session_id}", params={"loan_type": "Investment Loan"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "LOAN_NOT_FOUND"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_loan_endpoint_asks_for_loan_type_when_ambiguous(client):
    session_id = _authenticated_via_http(client, "DEMO003")

    response = client.get(f"/dev/loans/balance/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "LOAN_TYPE_REQUIRED"
    assert sorted(body["available_loan_types"]) == ["Car Loan", "Home Loan"]

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_loan_endpoints_take_no_customer_id(client):
    """A customer_id query parameter must not influence the result."""
    session_id = _authenticated_via_http(client, "DEMO001")

    body = client.get(
        f"/dev/loans/balance/{session_id}", params={"customer_id": "DEMO002"}
    ).json()

    assert body["loan_reference"] == "HL-DEMO001"

    schema = client.get("/openapi.json").json()
    for path in (
        "/dev/loans/balance/{session_id}",
        "/dev/loans/instalment/{session_id}",
        "/dev/loans/details/{session_id}",
    ):
        names = {p["name"] for p in schema["paths"][path]["get"].get("parameters", [])}
        assert "customer_id" not in names

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_two_customers_loans_stay_isolated(client):
    session_a = _authenticated_via_http(client, "DEMO001")
    session_b = _authenticated_via_http(client, "DEMO002")

    body_a = client.get(f"/dev/loans/balance/{session_a}").json()
    body_b = client.get(f"/dev/loans/balance/{session_b}").json()

    assert body_a["loan_reference"] == "HL-DEMO001"
    assert body_b["loan_reference"] == "PL-DEMO002"
    assert body_a["outstanding_balance"] != body_b["outstanding_balance"]

    client.delete(f"/dev/sessions/{session_a}")
    client.delete(f"/dev/sessions/{session_b}")


def test_dev_loan_responses_expose_no_credentials(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    bodies = [
        client.get(f"/dev/loans/balance/{session_id}").text,
        client.get(f"/dev/loans/instalment/{session_id}").text,
        client.get(f"/dev/loans/details/{session_id}").text,
    ]

    for body in bodies:
        lowered = body.lower()
        assert "pin" not in lowered
        assert "hash" not in lowered
        assert "pbkdf2" not in lowered

    client.delete(f"/dev/sessions/{session_id}")


# --- account tools remain intact --------------------------------------------


def test_account_tools_still_work_alongside_loans(client):
    """Phase 5 behaviour is unchanged by the shared authentication helper."""
    session_id = _authenticated_via_http(client, "DEMO001")

    account = client.get(
        f"/dev/accounts/balance/{session_id}", params={"account_type": "Savings"}
    ).json()
    loan = client.get(f"/dev/loans/balance/{session_id}").json()

    assert account["masked_account"] == "XXXX1001"
    assert account["available_balance"] == "12450.75"
    assert loan["loan_reference"] == "HL-DEMO001"

    client.delete(f"/dev/sessions/{session_id}")


def test_health_endpoint_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
