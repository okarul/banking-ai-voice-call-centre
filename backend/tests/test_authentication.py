"""Phase 4 authentication tests.

Uses the synthetic demo credentials seeded in Phase 2. These are fake.
"""

import pytest
from fastapi.testclient import TestClient

from app.auth import authentication
from app.auth.normalization import normalize_customer_id, normalize_pin
from app.main import app
from app.sessions import SessionManager

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
DEMO001_PIN = "4821"
DEMO002_PIN = "7315"
WRONG_PIN = "0000"


@pytest.fixture
def manager():
    """Isolated SessionManager so tests never share authentication state."""
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


def _identified_session(manager, customer_id="DEMO001"):
    """Create a session that has passed customer identification."""
    session = manager.create_session()
    result = authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )
    assert result["success"] is True
    return session


# --- customer id normalisation ----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "DEMO001",
        "demo001",
        "Demo 001",
        "demo 001",
        "DEMO zero zero one",
        "demo zero zero one",
        "demo 0 0 1",
        "D E M O zero zero one",
        "D E M O 0 0 1",
        "  demo   zero zero one  ",
    ],
)
def test_customer_id_normalizes_to_demo001(raw):
    assert normalize_customer_id(raw) == "DEMO001"


def test_customer_id_normalizes_other_demo_customers():
    assert normalize_customer_id("demo zero zero two") == "DEMO002"
    assert normalize_customer_id("DEMO003") == "DEMO003"
    assert normalize_customer_id("demo 0 0 4") == "DEMO004"
    assert normalize_customer_id("D E M O zero zero five") == "DEMO005"


@pytest.mark.parametrize(
    "raw",
    [
        "hello",
        "my account",
        "customer number maybe five",
        "demo",
        "demo 01",
        "demo 0001",
        "",
        None,
    ],
)
def test_invalid_customer_text_does_not_normalize(raw):
    assert normalize_customer_id(raw) is None


# --- PIN normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "4821",
        "4 8 2 1",
        "four eight two one",
        "Four Eight Two One",
        "four, eight, two, one",
        "  4821  ",
    ],
)
def test_pin_normalizes_to_4821(raw):
    assert normalize_pin(raw) == DEMO001_PIN


def test_pin_normalizes_zero_forms():
    assert normalize_pin("zero zero zero zero") == "0000"
    assert normalize_pin("oh oh oh oh") == "0000"
    assert normalize_pin("0 0 0 0") == "0000"


@pytest.mark.parametrize(
    "raw",
    [
        "482",  # too short
        "48215",  # too long
        "four eight",  # too short spoken
        "four eight two one five",  # too long spoken
        "hello",
        "my pin",
        "",
        None,
    ],
)
def test_invalid_pin_is_rejected(raw):
    assert normalize_pin(raw) is None


# --- customer verification --------------------------------------------------


def test_valid_customer_can_be_found(manager):
    session = manager.create_session()

    result = authentication.verify_customer(
        session.session_id, "DEMO001", manager=manager
    )

    assert result == {"success": True, "customer_id": "DEMO001", "next_step": "PIN"}


def test_an_unknown_customer_id_is_accepted_for_the_pin_step(manager):
    """An id that matches nobody must be treated exactly like one that does.

    Rejecting it here would answer "does this customer bank with you?" for
    anyone willing to guess. The claim is taken, and it fails at the PIN.
    """
    session = manager.create_session()

    result = authentication.verify_customer(
        session.session_id, "DEMO999", manager=manager
    )

    assert result == {"success": True, "customer_id": "DEMO999", "next_step": "PIN"}
    # Held as a claim only. Nothing is verified, so nothing is promoted.
    assert session.candidate_customer_id == "DEMO999"
    assert session.customer_id is None
    assert session.authenticated is False


def test_valid_customer_id_does_not_authenticate_session(manager):
    session = _identified_session(manager)

    # The claim is recorded, but `customer_id` stays empty until a PIN proves
    # it — everything downstream trusts `customer_id`, so it must never hold
    # an unproven value.
    assert session.candidate_customer_id == "DEMO001"
    assert session.customer_id is None
    assert session.authenticated is False
    assert session.authentication_attempts == 0


def test_authentication_cannot_occur_for_nonexistent_session(manager):
    assert authentication.verify_customer(
        "SESSION-missing", "DEMO001", manager=manager
    ) == {"success": False, "reason": "SESSION_NOT_FOUND"}

    pin_result = authentication.verify_pin(
        "SESSION-missing", DEMO001_PIN, manager=manager
    )
    assert pin_result["success"] is False
    assert pin_result["reason"] == "SESSION_NOT_FOUND"


def test_pin_verification_requires_customer_identification_first(manager):
    session = manager.create_session()

    result = authentication.verify_pin(session.session_id, DEMO001_PIN, manager=manager)

    assert result["success"] is False
    assert result["reason"] == "CUSTOMER_NOT_IDENTIFIED"
    assert session.authenticated is False


# --- PIN verification -------------------------------------------------------


def test_correct_pin_authenticates_session(manager):
    session = _identified_session(manager)

    result = authentication.verify_pin(session.session_id, DEMO001_PIN, manager=manager)

    assert result["success"] is True
    assert result["authenticated"] is True
    assert session.authenticated is True
    assert session.customer_id == "DEMO001"
    assert session.authentication_attempts == 0


def test_wrong_pin_does_not_authenticate(manager):
    session = _identified_session(manager)

    result = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    assert result["success"] is False
    assert result["authenticated"] is False
    assert session.authenticated is False


def test_failed_attempts_increment_then_lock(manager):
    session = _identified_session(manager)

    first = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    assert session.authentication_attempts == 1
    assert first["attempts_remaining"] == 2
    assert session.authentication_locked is False

    second = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    assert session.authentication_attempts == 2
    assert second["attempts_remaining"] == 1
    assert session.authentication_locked is False

    third = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    assert session.authentication_attempts == 3
    assert third["reason"] == "AUTHENTICATION_LOCKED"
    assert third["attempts_remaining"] == 0
    assert session.authentication_locked is True
    assert session.authenticated is False


def test_correct_pin_rejected_after_lock(manager):
    session = _identified_session(manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    result = authentication.verify_pin(session.session_id, DEMO001_PIN, manager=manager)

    assert result["success"] is False
    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert result["authenticated"] is False
    assert session.authenticated is False


def test_locked_session_cannot_restart_identification(manager):
    session = _identified_session(manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    result = authentication.verify_customer(
        session.session_id, "DEMO002", manager=manager
    )

    assert result["success"] is False
    assert result["reason"] == "AUTHENTICATION_LOCKED"


def test_malformed_pin_does_not_count_as_an_attempt(manager):
    session = _identified_session(manager)

    result = authentication.submit_pin(session.session_id, "four eight", manager=manager)

    assert result["success"] is False
    assert result["reason"] == "INVALID_PIN_FORMAT"
    assert session.authentication_attempts == 0


# --- isolation --------------------------------------------------------------


def test_authentication_in_one_session_does_not_affect_another(manager):
    session_a = _identified_session(manager, "DEMO001")
    session_b = _identified_session(manager, "DEMO002")

    authentication.verify_pin(session_a.session_id, DEMO001_PIN, manager=manager)

    assert session_a.authenticated is True
    assert session_b.authenticated is False
    # B has claimed an identity but not proven one.
    assert session_b.candidate_customer_id == "DEMO002"
    assert session_b.customer_id is None


def test_two_customers_authenticate_independently(manager):
    session_a = _identified_session(manager, "DEMO001")
    session_b = _identified_session(manager, "DEMO002")

    authentication.verify_pin(session_a.session_id, DEMO001_PIN, manager=manager)
    authentication.verify_pin(session_b.session_id, DEMO002_PIN, manager=manager)

    assert (session_a.customer_id, session_a.authenticated) == ("DEMO001", True)
    assert (session_b.customer_id, session_b.authenticated) == ("DEMO002", True)


def test_lock_in_one_session_does_not_lock_another(manager):
    session_a = _identified_session(manager, "DEMO001")
    session_b = _identified_session(manager, "DEMO002")

    for _ in range(3):
        authentication.verify_pin(session_a.session_id, WRONG_PIN, manager=manager)

    assert session_a.authentication_locked is True
    assert session_b.authentication_locked is False

    result = authentication.verify_pin(session_b.session_id, DEMO002_PIN, manager=manager)
    assert result["authenticated"] is True


def test_demo001_pin_does_not_authenticate_demo002_session(manager):
    session_b = _identified_session(manager, "DEMO002")

    result = authentication.verify_pin(session_b.session_id, DEMO001_PIN, manager=manager)

    assert result["success"] is False
    assert session_b.authenticated is False


# --- development endpoints --------------------------------------------------


def _new_session_id(client):
    return client.post("/dev/sessions").json()["session_id"]


def test_dev_full_authentication_flow(client):
    session_id = _new_session_id(client)

    customer = client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "demo zero zero one"},
    )
    assert customer.status_code == 200
    assert customer.json() == {
        "success": True,
        "customer_id": "DEMO001",
        "next_step": "PIN",
    }

    status_after_id = client.get(f"/dev/auth/status/{session_id}").json()
    # No verified customer yet, so the status reports none.
    assert status_after_id["customer_id"] is None
    assert status_after_id["authenticated"] is False

    pin = client.post(
        "/dev/auth/pin",
        json={"session_id": session_id, "pin": "four eight two one"},
    )
    assert pin.status_code == 200
    body = pin.json()
    assert body["success"] is True
    assert body["authenticated"] is True

    session_view = client.get(f"/dev/sessions/{session_id}").json()
    assert session_view["customer_id"] == "DEMO001"
    assert session_view["authenticated"] is True

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_three_failed_attempts_lock_then_correct_pin_rejected(client):
    session_id = _new_session_id(client)
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "DEMO001"},
    )

    remaining = []
    for _ in range(3):
        body = client.post(
            "/dev/auth/pin", json={"session_id": session_id, "pin": "0000"}
        ).json()
        assert body["success"] is False
        assert body["authenticated"] is False
        remaining.append(body.get("attempts_remaining"))

    assert remaining == [2, 1, 0]

    locked = client.post(
        "/dev/auth/pin", json={"session_id": session_id, "pin": "four eight two one"}
    ).json()
    assert locked["success"] is False
    assert locked["authenticated"] is False
    assert locked["reason"] == "AUTHENTICATION_LOCKED"

    status_body = client.get(f"/dev/auth/status/{session_id}").json()
    assert status_body["authentication_locked"] is True
    assert status_body["authenticated"] is False

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_two_sessions_stay_isolated(client):
    session_a = _new_session_id(client)
    session_b = _new_session_id(client)

    client.post(
        "/dev/auth/customer",
        json={"session_id": session_a, "customer_id": "demo zero zero one"},
    )
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_b, "customer_id": "demo zero zero two"},
    )
    client.post(
        "/dev/auth/pin", json={"session_id": session_a, "pin": "four eight two one"}
    )
    client.post(
        "/dev/auth/pin", json={"session_id": session_b, "pin": "seven three one five"}
    )

    status_a = client.get(f"/dev/auth/status/{session_a}").json()
    status_b = client.get(f"/dev/auth/status/{session_b}").json()

    assert (status_a["customer_id"], status_a["authenticated"]) == ("DEMO001", True)
    assert (status_b["customer_id"], status_b["authenticated"]) == ("DEMO002", True)

    client.delete(f"/dev/sessions/{session_a}")
    client.delete(f"/dev/sessions/{session_b}")


def test_dev_invalid_customer_id_is_rejected(client):
    session_id = _new_session_id(client)

    body = client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "my account please"},
    ).json()

    assert body["success"] is False
    assert body["reason"] == "INVALID_CUSTOMER_ID_FORMAT"

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_responses_expose_no_pin_or_hash(client):
    session_id = _new_session_id(client)
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": "DEMO001"},
    )

    bodies = [
        client.post(
            "/dev/auth/pin", json={"session_id": session_id, "pin": "four eight two one"}
        ).text,
        client.get(f"/dev/auth/status/{session_id}").text,
        client.get(f"/dev/sessions/{session_id}").text,
    ]

    for body in bodies:
        lowered = body.lower()
        assert "pin_hash" not in lowered
        assert "pbkdf2" not in lowered
        assert DEMO001_PIN not in body

    client.delete(f"/dev/sessions/{session_id}")


def test_dev_status_unknown_session_returns_404(client):
    assert client.get("/dev/auth/status/SESSION-missing").status_code == 404


def test_health_endpoint_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
