"""The PIN lockout that survives hanging up.

Phase 1 shipped a per-session lockout and recorded its own limitation honestly:
three wrong guesses ended a *call*, and a new call started the count again. On
a browser that is tedious. On a telephone line it is a loop — dial, guess,
hang up, redial — and a four-digit PIN is 10 000 guesses.

So the count now also lives in the database, keyed by the customer id the
caller claimed. These tests are mostly about the two ways that control could be
wrong: too weak to stop the redial, or so strong it becomes a way to deny a
shared demonstration customer to a whole classroom.

Every customer and PIN here is synthetic seed data. Time is passed in, never
slept for.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth import lockout
from app.auth.authentication import (
    MAX_AUTHENTICATION_ATTEMPTS,
    submit_customer_id,
    submit_pin,
)
from app.config import settings
from app.database.connection import session_scope
from app.database.models import CustomerAuthLock
from app.sessions import session_manager

REAL_PIN = "4821"
WRONG_PIN = "0000"
CUSTOMER = "DEMO001"


def fresh_call() -> str:
    """A new banking session, exactly as a new call would produce."""
    session = session_manager.create_session()
    submit_customer_id(session.session_id, CUSTOMER)
    return session.session_id


def guess(session_id: str, pin: str = WRONG_PIN) -> dict:
    return submit_pin(session_id, pin)


def lock_row(customer_id: str = CUSTOMER) -> CustomerAuthLock | None:
    with session_scope() as db:
        return db.scalars(
            select(CustomerAuthLock).where(
                CustomerAuthLock.customer_id == customer_id
            )
        ).first()


# === the redial loop, which is the whole point ==============================


def test_failures_are_remembered_after_the_call_ends():
    """The Phase 1 gap, stated as a test."""
    first = fresh_call()
    guess(first)
    guess(first)
    session_manager.destroy_session(first)

    # A brand-new call, with no memory of its own.
    assert lock_row().failed_attempts == 2


def test_redialling_does_not_refresh_the_attempt_budget():
    """One guess per call, across enough calls to exhaust the persistent limit."""
    for _ in range(settings.pin_lockout_max_attempts):
        session_id = fresh_call()
        guess(session_id)
        session_manager.destroy_session(session_id)

    final = fresh_call()
    result = guess(final, REAL_PIN)

    assert result["success"] is False
    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert result["authenticated"] is False


def test_the_correct_pin_does_not_open_a_locked_id():
    """A lock that the right PIN could open would not be a lock."""
    for _ in range(settings.pin_lockout_max_attempts):
        session_id = fresh_call()
        guess(session_id)

    session_id = fresh_call()
    assert guess(session_id, REAL_PIN)["authenticated"] is False
    assert session_manager.get_session(session_id).customer_id is None


def test_the_per_call_lockout_still_applies_first():
    """The Phase 1 behaviour is unchanged: three wrong PINs end this call."""
    session_id = fresh_call()

    for _ in range(MAX_AUTHENTICATION_ATTEMPTS - 1):
        assert guess(session_id)["reason"] == "INVALID_CREDENTIALS"

    assert guess(session_id)["reason"] == "AUTHENTICATION_LOCKED"
    assert session_manager.get_session(session_id).authentication_locked is True


def test_a_successful_verification_clears_the_count():
    """An honest customer who mistypes twice is not two guesses from a lock."""
    session_id = fresh_call()
    guess(session_id)
    guess(session_id)
    assert lock_row().failed_attempts == 2

    verified = fresh_call()
    assert guess(verified, REAL_PIN)["authenticated"] is True

    assert lock_row() is None


# === the lock must let go ===================================================


def test_a_lock_expires_on_its_own():
    """An unbounded lock on a shared demo customer is a denial of service."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    for _ in range(settings.pin_lockout_max_attempts):
        lockout.record_failure(CUSTOMER, now=now)

    assert lockout.is_locked(CUSTOMER, now=now) is not None

    later = now + timedelta(minutes=settings.pin_lockout_minutes + 1)
    assert lockout.is_locked(CUSTOMER, now=later) is None


def test_old_failures_are_not_counted_towards_a_lock():
    """The window is the memory, not just the sentence."""
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    for _ in range(settings.pin_lockout_max_attempts - 1):
        lockout.record_failure(CUSTOMER, now=now)

    much_later = now + timedelta(minutes=settings.pin_lockout_minutes + 5)
    state = lockout.record_failure(CUSTOMER, now=much_later)

    # The run restarted rather than tipping over the threshold.
    assert state.failed_attempts == 1
    assert state.locked is False


def test_an_expired_lock_lets_the_right_pin_through_again():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    for _ in range(settings.pin_lockout_max_attempts):
        lockout.record_failure(CUSTOMER, now=now)
    assert lockout.is_locked(CUSTOMER, now=now) is not None

    # Wind the stored expiry into the past, as the clock would.
    with session_scope() as db:
        row = db.scalars(
            select(CustomerAuthLock).where(CustomerAuthLock.customer_id == CUSTOMER)
        ).one()
        row.locked_until = now - timedelta(minutes=1)
        row.last_failed_at = now - timedelta(hours=2)

    session_id = fresh_call()
    assert guess(session_id, REAL_PIN)["authenticated"] is True


# === it must not become an enumeration oracle ===============================


def test_an_unknown_customer_id_is_counted_too():
    """Otherwise watching which ids lock would separate real ones from invented.

    This is the same reasoning as the single generic failure message: any
    observable difference between a real customer and a made-up one is a
    customer list, one guess at a time.
    """
    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO999")
    submit_pin(session.session_id, WRONG_PIN)

    assert lock_row("DEMO999") is not None
    assert lock_row("DEMO999").failed_attempts == 1


def test_a_locked_unknown_id_answers_exactly_like_a_locked_real_one():
    for customer_id in (CUSTOMER, "DEMO999"):
        for _ in range(settings.pin_lockout_max_attempts):
            lockout.record_failure(customer_id)

    def attempt(customer_id):
        session = session_manager.create_session()
        submit_customer_id(session.session_id, customer_id)
        return submit_pin(session.session_id, WRONG_PIN)

    assert attempt(CUSTOMER) == attempt("DEMO999")


def test_locking_one_customer_does_not_lock_another():
    for _ in range(settings.pin_lockout_max_attempts):
        lockout.record_failure(CUSTOMER)

    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO002")
    result = submit_pin(session.session_id, "7315")

    assert result["authenticated"] is True
    assert result["customer_id"] == "DEMO002"


# === the phone channel is not exempt ========================================


def test_the_lock_applies_to_a_phone_session(monkeypatch):
    """A telephone call is where this control actually earns its place."""
    from fastapi.testclient import TestClient

    import tests.test_telephony_webhook as webhook

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_webhook_secret", webhook.TEST_SECRET)
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 0)

    from app.main import create_app

    client = TestClient(create_app())

    # Burn the persistent budget from browser calls...
    for _ in range(settings.pin_lockout_max_attempts):
        session_id = fresh_call()
        guess(session_id)
        session_manager.destroy_session(session_id)

    # ...and the telephone caller arrives already locked.
    body = webhook.event_body(call_id="call-lockout", event_id="evt-lockout")
    client.post(
        webhook.ENDPOINT, content=body, headers=webhook.signed_headers(body)
    )
    phone_session_id = webhook.phone_rows()[0].banking_session_id

    submit_customer_id(phone_session_id, CUSTOMER)
    result = submit_pin(phone_session_id, REAL_PIN)

    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert result["authenticated"] is False


# === the counter must survive being raced ===================================


def test_concurrent_failures_are_all_counted():
    """A read-modify-write in Python would lose some of these.

    Two calls guessing at the same instant must cost two attempts. If they
    could both read "two so far" and both write three, an attacker running
    calls in parallel would get more guesses than the limit allows.
    """
    import threading

    def fail():
        lockout.record_failure(CUSTOMER)

    threads = [threading.Thread(target=fail) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert lock_row().failed_attempts == 8


def test_the_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "pin_lockout_max_attempts", 2)

    assert lockout.record_failure(CUSTOMER).locked is False
    assert lockout.record_failure(CUSTOMER).locked is True


def test_a_zero_threshold_disables_rather_than_locking_everyone(monkeypatch):
    """A misconfigured limit must not take the bank offline."""
    monkeypatch.setattr(settings, "pin_lockout_max_attempts", 0)

    assert lockout.record_failure(CUSTOMER).locked is False
    assert lockout.is_locked(CUSTOMER) is None

    session_id = fresh_call()
    assert guess(session_id, REAL_PIN)["authenticated"] is True


# === no secret reaches the lock table =======================================


def test_the_lock_record_stores_no_pin():
    session_id = fresh_call()
    guess(session_id, "1234")

    row = lock_row()
    stored = " ".join(
        str(getattr(row, column.name)) for column in CustomerAuthLock.__table__.columns
    )
    assert "1234" not in stored
    for forbidden in ("pin", "hash", "secret"):
        assert forbidden not in {c.name for c in CustomerAuthLock.__table__.columns}


@pytest.fixture(autouse=True)
def clear_sessions():
    yield
    session_manager.clear()
