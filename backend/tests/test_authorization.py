"""Phase 7 authorization and ownership tests.

These treat the caller as untrusted: the checks below are what stands between a
future language model's tool arguments and another customer's banking data.
"""

import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import authentication
from app.authorization import (
    AuthorizationError,
    Reason,
    require_account_owned_by_session,
    require_authenticated_customer,
    require_loan_owned_by_session,
)
from app.database.connection import session_scope
from app.database.models import Account, Customer, Loan
from app.main import app
from app.sessions import SessionManager, SessionStatus
from app.tools import accounts, loans

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {"DEMO001": "4821", "DEMO002": "7315"}
WRONG_PIN = "0000"


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


def _authenticated(manager, customer_id="DEMO001"):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, customer_id, manager=manager)
    authentication.verify_pin(session.session_id, PINS[customer_id], manager=manager)
    assert session.authenticated is True
    return session


def _row_for(model, customer_id):
    """Fetch a real account/loan row belonging to another customer."""
    with session_scope() as db:
        return db.scalar(
            select(model)
            .join(Customer)
            .where(Customer.customer_id == customer_id)
        )


# --- core guard -------------------------------------------------------------


def test_nonexistent_session_is_rejected(manager):
    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer("SESSION-missing", manager=manager)
    assert exc.value.reason == Reason.SESSION_NOT_FOUND


def test_unauthenticated_session_is_rejected(manager):
    session = manager.create_session()

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)

    assert exc.value.reason == Reason.NOT_AUTHENTICATED


def test_authenticated_session_is_accepted(manager):
    session = _authenticated(manager, "DEMO001")

    context = require_authenticated_customer(session.session_id, manager=manager)

    assert context.customer_id == "DEMO001"
    assert context.session_id == session.session_id
    assert context.to_dict() == {
        "session_id": session.session_id,
        "customer_id": "DEMO001",
    }


def test_context_exposes_no_credentials(manager):
    session = _authenticated(manager, "DEMO001")

    context = require_authenticated_customer(session.session_id, manager=manager)

    serialised = str(context.to_dict()).lower()
    assert "pin" not in serialised
    assert "hash" not in serialised


def test_missing_customer_context_is_rejected(manager):
    """authenticated=True with no customer id must fail closed."""
    session = manager.create_session()
    manager.update_session(session.session_id, authenticated=True, customer_id=None)

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)

    assert exc.value.reason == Reason.CUSTOMER_CONTEXT_MISSING


def test_authentication_locked_session_is_rejected(manager):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    assert session.authentication_locked is True

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)

    assert exc.value.reason == Reason.AUTHENTICATION_LOCKED


def test_locked_session_after_authentication_is_rejected(manager):
    """Even a previously authenticated session is refused once locked."""
    session = _authenticated(manager, "DEMO001")
    manager.update_session(session.session_id, authentication_locked=True)

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)

    assert exc.value.reason == Reason.AUTHENTICATION_LOCKED


def test_inactive_session_is_rejected(manager):
    """A non-ACTIVE session is refused even though it authenticated earlier."""
    session = _authenticated(manager, "DEMO001")
    manager.update_session(session.session_id, status=SessionStatus.COMPLETED)

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)

    assert exc.value.reason == Reason.SESSION_NOT_ACTIVE


def test_destroyed_session_is_rejected(manager):
    session = _authenticated(manager, "DEMO001")
    assert accounts.get_account_balance(
        session.session_id, "Savings", manager=manager
    )["success"] is True

    manager.destroy_session(session.session_id)

    with pytest.raises(AuthorizationError) as exc:
        require_authenticated_customer(session.session_id, manager=manager)
    assert exc.value.reason == Reason.SESSION_NOT_FOUND


def test_error_messages_leak_nothing_sensitive():
    for reason in vars(Reason).values():
        if not isinstance(reason, str) or reason.startswith("__"):
            continue
        message = AuthorizationError(reason).message.lower()
        for forbidden in ("select", "insert", "postgres", "psycopg", "pin", "hash",
                          "traceback", "password"):
            assert forbidden not in message


# --- ownership guards -------------------------------------------------------


def test_account_ownership_succeeds_for_own_account(manager):
    session = _authenticated(manager, "DEMO001")

    with session_scope() as db:
        account = db.scalar(
            select(Account).join(Customer).where(Customer.customer_id == "DEMO001")
        )
        context = require_account_owned_by_session(
            session.session_id, account, manager=manager
        )

    assert context.customer_id == "DEMO001"


def test_account_ownership_fails_for_another_customers_account(manager):
    """The row exists in the database; it still must not be reachable."""
    session = _authenticated(manager, "DEMO001")

    with session_scope() as db:
        foreign = db.scalar(
            select(Account).join(Customer).where(Customer.customer_id == "DEMO002")
        )
        assert foreign is not None

        with pytest.raises(AuthorizationError) as exc:
            require_account_owned_by_session(
                session.session_id, foreign, manager=manager
            )

    assert exc.value.reason == Reason.ACCOUNT_NOT_OWNED


def test_loan_ownership_succeeds_for_own_loan(manager):
    session = _authenticated(manager, "DEMO001")

    with session_scope() as db:
        loan = db.scalar(
            select(Loan).join(Customer).where(Customer.customer_id == "DEMO001")
        )
        context = require_loan_owned_by_session(
            session.session_id, loan, manager=manager
        )

    assert context.customer_id == "DEMO001"


def test_loan_ownership_fails_for_another_customers_loan(manager):
    session = _authenticated(manager, "DEMO001")

    with session_scope() as db:
        foreign = db.scalar(
            select(Loan).join(Customer).where(Customer.customer_id == "DEMO002")
        )
        assert foreign is not None

        with pytest.raises(AuthorizationError) as exc:
            require_loan_owned_by_session(session.session_id, foreign, manager=manager)

    assert exc.value.reason == Reason.LOAN_NOT_OWNED


def test_ownership_guards_fail_closed_on_missing_object(manager):
    session = _authenticated(manager, "DEMO001")

    with pytest.raises(AuthorizationError) as account_exc:
        require_account_owned_by_session(session.session_id, None, manager=manager)
    assert account_exc.value.reason == Reason.ACCOUNT_NOT_OWNED

    with pytest.raises(AuthorizationError) as loan_exc:
        require_loan_owned_by_session(session.session_id, None, manager=manager)
    assert loan_exc.value.reason == Reason.LOAN_NOT_OWNED


def test_ownership_guards_still_validate_the_session(manager):
    """An unauthenticated session fails before ownership is even considered."""
    session = manager.create_session()
    account = _row_for(Account, "DEMO001")

    with pytest.raises(AuthorizationError) as exc:
        require_account_owned_by_session(session.session_id, account, manager=manager)

    assert exc.value.reason == Reason.NOT_AUTHENTICATED


# --- tools cannot be told who the customer is -------------------------------


def test_no_banking_tool_accepts_a_customer_id():
    for tool in (
        accounts.get_account_balance,
        accounts.get_account_details,
        accounts.get_recent_transactions,
        loans.get_loan_balance,
        loans.get_next_instalment,
        loans.get_loan_details,
    ):
        assert "customer_id" not in inspect.signature(tool).parameters


def test_tools_reject_every_invalid_session_state(manager):
    """Each denial reason reaches the tool layer as a structured result."""
    missing = accounts.get_account_balance("SESSION-missing", manager=manager)
    assert missing == {"success": False, "reason": Reason.SESSION_NOT_FOUND}

    unauth = manager.create_session()
    assert accounts.get_account_balance(unauth.session_id, manager=manager)["reason"] == (
        Reason.NOT_AUTHENTICATED
    )

    locked = _authenticated(manager, "DEMO001")
    manager.update_session(locked.session_id, authentication_locked=True)
    assert loans.get_loan_balance(locked.session_id, manager=manager)["reason"] == (
        Reason.AUTHENTICATION_LOCKED
    )

    inactive = _authenticated(manager, "DEMO001")
    manager.update_session(inactive.session_id, status=SessionStatus.COMPLETED)
    assert loans.get_loan_balance(inactive.session_id, manager=manager)["reason"] == (
        Reason.SESSION_NOT_ACTIVE
    )


# --- cross-customer isolation -----------------------------------------------


def test_demo001_cannot_reach_demo002_account_or_loan(manager):
    session = _authenticated(manager, "DEMO001")

    account = accounts.get_account_balance(
        session.session_id, "Savings", manager=manager
    )
    loan = loans.get_loan_balance(session.session_id, manager=manager)

    assert account["masked_account"] == "XXXX1001"
    assert account["masked_account"] != "XXXX1002"
    assert loan["loan_reference"] == "HL-DEMO001"
    assert loan["loan_reference"] != "PL-DEMO002"


def test_demo002_cannot_reach_demo001_account_or_loan(manager):
    session = _authenticated(manager, "DEMO002")

    account = accounts.get_account_balance(session.session_id, manager=manager)
    loan = loans.get_loan_balance(session.session_id, manager=manager)

    assert account["masked_account"] == "XXXX1002"
    assert loan["loan_reference"] == "PL-DEMO002"

    # DEMO002 holds neither of these.
    assert accounts.get_account_balance(
        session.session_id, "Current", manager=manager
    )["reason"] == Reason.ACCOUNT_NOT_FOUND
    assert loans.get_loan_balance(
        session.session_id, "Home Loan", manager=manager
    )["reason"] == Reason.LOAN_NOT_FOUND


def test_two_sessions_stay_isolated_across_all_tools(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    a_account = accounts.get_account_balance(
        session_a.session_id, "Savings", manager=manager
    )
    b_account = accounts.get_account_balance(session_b.session_id, manager=manager)
    a_txns = accounts.get_recent_transactions(
        session_a.session_id, "Savings", manager=manager
    )
    b_txns = accounts.get_recent_transactions(session_b.session_id, manager=manager)
    a_loan = loans.get_loan_balance(session_a.session_id, manager=manager)
    b_loan = loans.get_loan_balance(session_b.session_id, manager=manager)

    assert a_account["masked_account"] == "XXXX1001"
    assert b_account["masked_account"] == "XXXX1002"
    assert a_txns["masked_account"] != b_txns["masked_account"]
    assert a_loan["loan_reference"] == "HL-DEMO001"
    assert b_loan["loan_reference"] == "PL-DEMO002"


def test_context_updates_do_not_cross_sessions(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    accounts.get_account_balance(session_a.session_id, "Savings", manager=manager)
    loans.get_loan_balance(session_b.session_id, manager=manager)

    assert session_a.current_domain == "ACCOUNT"
    assert session_a.conversation_context == {"account_type": "Savings"}
    assert session_b.current_domain == "LOAN"
    assert session_b.conversation_context == {"loan_type": "Personal Loan"}


def test_destroying_session_a_leaves_session_b_working(manager):
    session_a = _authenticated(manager, "DEMO001")
    session_b = _authenticated(manager, "DEMO002")

    manager.destroy_session(session_a.session_id)

    denied = accounts.get_account_balance(
        session_a.session_id, "Savings", manager=manager
    )
    assert denied["reason"] == Reason.SESSION_NOT_FOUND
    assert loans.get_loan_balance(session_a.session_id, manager=manager)["reason"] == (
        Reason.SESSION_NOT_FOUND
    )

    still_working = accounts.get_account_balance(session_b.session_id, manager=manager)
    assert still_working["masked_account"] == "XXXX1002"
    assert loans.get_loan_balance(session_b.session_id, manager=manager)["success"]


# --- tools still work -------------------------------------------------------


def test_all_six_tools_still_work_for_an_authenticated_customer(manager):
    session = _authenticated(manager, "DEMO001")
    sid = session.session_id

    assert accounts.get_account_balance(sid, "Savings", manager=manager)["success"]
    assert accounts.get_account_details(sid, "Savings", manager=manager)["success"]
    assert accounts.get_recent_transactions(sid, "Savings", manager=manager)["success"]
    assert loans.get_loan_balance(sid, manager=manager)["success"]
    assert loans.get_next_instalment(sid, manager=manager)["success"]
    assert loans.get_loan_details(sid, manager=manager)["success"]


# --- development endpoints --------------------------------------------------


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


def test_dev_authorization_check_for_authenticated_session(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    response = client.get(f"/dev/authorization/check/{session_id}")

    assert response.status_code == 200
    assert response.json() == {
        "authorized": True,
        "session_id": session_id,
        "customer_id": "DEMO001",
    }

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_authorization_check_for_unauthenticated_session(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    body = client.get(f"/dev/authorization/check/{session_id}").json()

    assert body == {"authorized": False, "reason": Reason.NOT_AUTHENTICATED}

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_authorization_check_for_unknown_session(client):
    body = client.get("/dev/authorization/check/SESSION-missing").json()
    assert body == {"authorized": False, "reason": Reason.SESSION_NOT_FOUND}


def test_dev_authorization_check_exposes_no_banking_values(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    text = client.get(f"/dev/authorization/check/{session_id}").text.lower()

    for forbidden in ("pin", "hash", "balance", "loan_reference", "12450"):
        assert forbidden not in text

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoints_map_authorization_reasons_to_statuses(client):
    # A session that identified itself and then failed the PIN three times.
    #
    # It is built from a fresh session on purpose: a session that has already
    # been verified cannot be sent back through identification or the PIN step
    # at all, so it can no longer be locked out from under its own caller.
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "DEMO001"},
    )
    for _ in range(3):
        client.post("/dev/auth/pin", json={"session_id": session_id, "pin": WRONG_PIN})

    account_response = client.get(
        f"/dev/accounts/balance/{session_id}", params={"account_type": "Savings"}
    )
    loan_response = client.get(f"/dev/loans/balance/{session_id}")

    assert account_response.status_code == 403
    assert account_response.json()["detail"]["reason"] == Reason.AUTHENTICATION_LOCKED
    assert loan_response.status_code == 403
    assert loan_response.json()["detail"]["reason"] == Reason.AUTHENTICATION_LOCKED

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_endpoints_still_work_end_to_end(client):
    session_id = _authenticated_via_http(client, "DEMO001")

    assert client.get(
        f"/dev/accounts/balance/{session_id}", params={"account_type": "Savings"}
    ).json()["masked_account"] == "XXXX1001"
    assert client.get(
        f"/dev/accounts/details/{session_id}", params={"account_type": "Savings"}
    ).status_code == 200
    assert client.get(
        f"/dev/accounts/transactions/{session_id}", params={"account_type": "Savings"}
    ).status_code == 200
    assert client.get(f"/dev/loans/balance/{session_id}").json()["loan_reference"] == (
        "HL-DEMO001"
    )
    assert client.get(f"/dev/loans/instalment/{session_id}").status_code == 200
    assert client.get(f"/dev/loans/details/{session_id}").status_code == 200

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_destroyed_session_loses_banking_access(client):
    session_a = _authenticated_via_http(client, "DEMO001")
    session_b = _authenticated_via_http(client, "DEMO002")

    assert client.get(
        f"/dev/accounts/balance/{session_a}", params={"account_type": "Savings"}
    ).status_code == 200

    client.delete(f"/dev/sessions/{session_a}")

    denied = client.get(
        f"/dev/accounts/balance/{session_a}", params={"account_type": "Savings"}
    )
    assert denied.status_code == 404
    assert denied.json()["detail"]["reason"] == Reason.SESSION_NOT_FOUND

    # Session B is untouched.
    assert client.get(f"/dev/accounts/balance/{session_b}").status_code == 200
    assert client.get(f"/dev/loans/balance/{session_b}").status_code == 200

    client.delete(f"/dev/sessions/{session_b}")


def test_dev_error_responses_leak_no_internals(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    bodies = [
        client.get(f"/dev/accounts/balance/{session_id}").text,
        client.get(f"/dev/loans/balance/{session_id}").text,
        client.get("/dev/accounts/balance/SESSION-missing").text,
        client.get("/dev/loans/details/SESSION-missing").text,
    ]

    for body in bodies:
        lowered = body.lower()
        for forbidden in ("traceback", "select ", "psycopg", "postgresql", "password",
                          "pin_hash", "pbkdf2", "sqlalchemy"):
            assert forbidden not in lowered

    client.delete(f"/dev/sessions/{session_id}")


def test_health_endpoint_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
