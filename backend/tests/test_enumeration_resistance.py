"""Before a caller is verified, nothing may reveal who banks here.

The identification step used to answer a question nobody should be able to ask.
Saying "no such customer" to DEMO999 and "now your PIN" to DEMO001 is a customer
directory: guess ids, keep the ones that get as far as the PIN. This file pins
the replacement — one uniform flow, where existence is decided silently at the
PIN step and never reported.

The property under test is *indistinguishability*: a real id and an invented one
must produce the same response, the same status, the same session state and
roughly the same timing, through every unauthenticated entry point.

All customers and PINs are synthetic Phase 2 seed data.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.auth import authentication
from app.auth.authentication import (
    ALREADY_AUTHENTICATED,
    INVALID_CREDENTIALS,
    MAX_AUTHENTICATION_ATTEMPTS,
)
from app.main import app
from app.sessions import SessionManager, session_manager

REAL = "DEMO001"
REAL_PIN = "4821"
OTHER_REAL = "DEMO002"
UNKNOWN = "DEMO999"
ALSO_UNKNOWN = "DEMO742"
WRONG_PIN = "0000"

# The four the brief names: two that exist, one that does not, and a
# syntactically valid id picked at random.
PROBES = [REAL, OTHER_REAL, UNKNOWN, ALSO_UNKNOWN]


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture(autouse=True)
def clean_shared_store():
    yield
    session_manager.clear()


@pytest.fixture
def client():
    return TestClient(app)


def identify(manager, customer_id):
    session = manager.create_session()
    result = authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )
    return session, result


# === 1-3: the identification step says the same thing to everyone ===========


@pytest.mark.parametrize("probe", PROBES)
def test_every_customer_id_is_accepted_for_the_pin_step(manager, probe):
    _session, result = identify(manager, probe)

    assert result == {"success": True, "customer_id": probe, "next_step": "PIN"}


def test_known_and_unknown_ids_are_byte_for_byte_indistinguishable(manager):
    """The only difference between the responses is the id the caller said."""
    _a, real = identify(manager, REAL)
    _b, unknown = identify(manager, UNKNOWN)

    assert set(real) == set(unknown)
    assert real["success"] == unknown["success"] is True
    assert real["next_step"] == unknown["next_step"] == "PIN"
    # Nothing in either response reports existence.
    for body in (real, unknown):
        assert "reason" not in body
        assert "exists" not in str(body).lower()


@pytest.mark.parametrize("probe", PROBES)
def test_the_session_looks_the_same_whichever_id_was_claimed(manager, probe):
    session, _ = identify(manager, probe)

    assert session.candidate_customer_id == probe
    assert session.customer_id is None
    assert session.authenticated is False
    assert session.authentication_locked is False


def test_identification_never_touches_the_customer_table(manager, monkeypatch):
    """Existence cannot leak from a lookup that does not happen."""
    from app.auth import authentication as auth_module

    def refuse(*_args, **_kwargs):
        raise AssertionError("identification must not query the customer table")

    monkeypatch.setattr(auth_module, "get_customer_by_customer_id", refuse)

    for probe in PROBES:
        identify(manager, probe)


# === 4-6: the PIN step decides, and says only that it failed =================


def test_a_real_id_with_its_real_pin_authenticates(manager):
    session, _ = identify(manager, REAL)

    result = authentication.verify_pin(session.session_id, REAL_PIN, manager=manager)

    assert result["success"] is True
    assert result["authenticated"] is True
    assert result["customer_id"] == REAL
    # Only now is the claim promoted to a verified identity.
    assert session.customer_id == REAL
    assert session.candidate_customer_id == REAL


def test_a_real_id_with_the_wrong_pin_fails(manager):
    session, _ = identify(manager, REAL)

    result = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    assert result["success"] is False
    assert result["reason"] == INVALID_CREDENTIALS
    assert session.customer_id is None
    assert session.authenticated is False


@pytest.mark.parametrize("probe", [UNKNOWN, ALSO_UNKNOWN])
def test_an_unknown_id_fails_exactly_like_a_wrong_pin(manager, probe):
    """The two failures must be impossible to tell apart."""
    known_session, _ = identify(manager, REAL)
    unknown_session, _ = identify(manager, probe)

    wrong_pin = authentication.verify_pin(
        known_session.session_id, WRONG_PIN, manager=manager
    )
    unknown_id = authentication.verify_pin(
        unknown_session.session_id, REAL_PIN, manager=manager
    )

    assert wrong_pin == unknown_id


def test_a_real_customers_pin_does_not_work_under_another_id(manager):
    """DEMO002's PIN must not authenticate a session claiming DEMO001."""
    session, _ = identify(manager, REAL)

    result = authentication.verify_pin(session.session_id, "7315", manager=manager)

    assert result["reason"] == INVALID_CREDENTIALS
    assert session.customer_id is None


# === 7-8: the attempt policy still holds ====================================


def test_the_attempt_counter_still_counts_down(manager):
    session, _ = identify(manager, REAL)

    first = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    second = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    assert first["attempts_remaining"] == MAX_AUTHENTICATION_ATTEMPTS - 1
    assert second["attempts_remaining"] == MAX_AUTHENTICATION_ATTEMPTS - 2
    assert session.authentication_locked is False


@pytest.mark.parametrize("probe", [REAL, UNKNOWN])
def test_lockout_still_applies_and_looks_the_same_either_way(manager, probe):
    session, _ = identify(manager, probe)

    results = [
        authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
        for _ in range(MAX_AUTHENTICATION_ATTEMPTS)
    ]

    assert results[-1]["reason"] == "AUTHENTICATION_LOCKED"
    assert results[-1]["attempts_remaining"] == 0
    assert session.authentication_locked is True
    # And a correct PIN afterwards changes nothing.
    after = authentication.verify_pin(session.session_id, REAL_PIN, manager=manager)
    assert after["reason"] == "AUTHENTICATION_LOCKED"
    assert session.authenticated is False


def test_re_identifying_cannot_reset_the_attempt_counter(manager):
    """Naming an id again between guesses must not buy more attempts.

    Otherwise the lockout is decorative: guess twice, say the id again, repeat
    for as long as you like.
    """
    session, _ = identify(manager, REAL)

    authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)
    # Try to wipe the slate.
    authentication.verify_customer(session.session_id, REAL, manager=manager)
    third = authentication.verify_pin(session.session_id, WRONG_PIN, manager=manager)

    assert session.authentication_attempts == MAX_AUTHENTICATION_ATTEMPTS
    assert third["reason"] == "AUTHENTICATION_LOCKED"
    assert session.authentication_locked is True


# === 9-10: every unauthenticated entry point ================================


@pytest.mark.parametrize("probe", PROBES)
def test_the_dev_auth_route_reveals_nothing(client, probe):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.post(
        "/dev/auth/customer", json={"session_id": session_id, "customer_id": probe}
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "customer_id": probe,
        "next_step": "PIN",
    }


def test_the_dev_auth_route_answers_identically_for_real_and_invented_ids(client):
    real_session = client.post("/dev/sessions").json()["session_id"]
    fake_session = client.post("/dev/sessions").json()["session_id"]

    real = client.post(
        "/dev/auth/customer", json={"session_id": real_session, "customer_id": REAL}
    )
    fake = client.post(
        "/dev/auth/customer", json={"session_id": fake_session, "customer_id": UNKNOWN}
    )

    assert real.status_code == fake.status_code
    assert set(real.json()) == set(fake.json())

    # And the same after a wrong PIN on each.
    real_pin = client.post(
        "/dev/auth/pin", json={"session_id": real_session, "pin": WRONG_PIN}
    )
    fake_pin = client.post(
        "/dev/auth/pin", json={"session_id": fake_session, "pin": REAL_PIN}
    )

    assert real_pin.status_code == fake_pin.status_code
    assert real_pin.json() == fake_pin.json()


@pytest.mark.parametrize("probe", PROBES)
def test_the_browser_call_route_reveals_nothing(client, probe):
    """/api/call/tool is the customer-facing path and must be no different."""
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": probe},
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "success": True,
        "customer_id": probe,
        "next_step": "PIN",
    }


def test_the_browser_call_route_fails_identically_for_both(client):
    real_session = client.post("/dev/sessions").json()["session_id"]
    fake_session = client.post("/dev/sessions").json()["session_id"]

    def submit(session_id, name, arguments):
        return client.post(
            "/api/call/tool",
            json={"session_id": session_id, "name": name, "arguments": arguments},
        )

    submit(real_session, "submit_customer_id", {"spoken_customer_id": REAL})
    submit(fake_session, "submit_customer_id", {"spoken_customer_id": UNKNOWN})

    real = submit(real_session, "submit_pin", {"spoken_pin": WRONG_PIN})
    fake = submit(fake_session, "submit_pin", {"spoken_pin": REAL_PIN})

    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()


def test_the_authentication_status_view_reports_no_unverified_claim(client):
    """The status a voice agent reads must not echo an unproven identity."""
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer", json={"session_id": session_id, "customer_id": UNKNOWN}
    )

    status = client.get(f"/dev/auth/status/{session_id}").json()

    assert status["customer_id"] is None
    assert status["authenticated"] is False
    assert UNKNOWN not in str(status)


# === 11: the voice prompts ==================================================


def test_the_instructions_forbid_commenting_on_the_customer_id():
    from app.realtime.banking_realtime import INSTRUCTIONS

    lowered = " ".join(INSTRUCTIONS.lower().split())

    assert "never comment on the customer id itself" in lowered
    assert "i'm unable to verify those details. please try again." in lowered
    assert "say nothing about which part was wrong" in lowered


# === timing =================================================================


def test_an_unknown_id_does_not_fail_noticeably_faster(manager):
    """A fast "no such customer" is an oracle even when the wording is uniform.

    Exact constant time is not the goal here; what matters is that an unknown
    id does the same key-derivation work as a wrong PIN, so the two cannot be
    told apart by a stopwatch.
    """

    def elapsed(customer_id, pin):
        session, _ = identify(manager, customer_id)
        start = time.perf_counter()
        authentication.verify_pin(session.session_id, pin, manager=manager)
        return time.perf_counter() - start

    # Warm the connection pool so the first call is not an outlier.
    elapsed(REAL, WRONG_PIN)

    wrong_pin = min(elapsed(REAL, WRONG_PIN) for _ in range(3))
    unknown_id = min(elapsed(UNKNOWN, REAL_PIN) for _ in range(3))

    slower, faster = sorted((wrong_pin, unknown_id), reverse=True)
    assert slower < faster * 5, (
        f"unknown-id {unknown_id:.4f}s vs wrong-pin {wrong_pin:.4f}s "
        "— one path is fast enough to enumerate with"
    )


# === 12-13: nothing that worked before stopped working ======================


def test_an_authenticated_customer_still_reads_their_own_account(manager):
    session, _ = identify(manager, REAL)
    authentication.verify_pin(session.session_id, REAL_PIN, manager=manager)

    from app.tools.accounts import get_account_balance

    result = get_account_balance(session.session_id, "Savings", manager=manager)

    assert result["success"] is True
    assert result["masked_account"] == "XXXX1001"
    assert result["available_balance"] == "12450.75"


def test_an_authenticated_customer_still_reads_their_own_loan(manager):
    session, _ = identify(manager, REAL)
    authentication.verify_pin(session.session_id, REAL_PIN, manager=manager)

    from app.tools.loans import get_loan_balance

    result = get_loan_balance(session.session_id, "Home Loan", manager=manager)

    assert result["success"] is True
    assert result["loan_reference"] == "HL-DEMO001"


def test_an_unproven_claim_never_reaches_banking_data(manager):
    """Claiming an identity is not being one."""
    session, _ = identify(manager, REAL)

    from app.tools.accounts import get_account_balance

    result = get_account_balance(session.session_id, "Savings", manager=manager)

    assert result == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_identity_is_still_settled_once_per_call(manager):
    """The Phase 10 isolation rule must survive this change."""
    session, _ = identify(manager, REAL)
    authentication.verify_pin(session.session_id, REAL_PIN, manager=manager)

    result = authentication.verify_customer(
        session.session_id, OTHER_REAL, manager=manager
    )

    assert result["reason"] == ALREADY_AUTHENTICATED
    assert session.customer_id == REAL
    assert session.authenticated is True
