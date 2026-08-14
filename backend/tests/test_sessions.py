"""Phase 3 session manager tests.

These need no database: the session layer is entirely in memory.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sessions import SessionManager, SessionNotFoundError, SessionStatus
from app.sessions import session_manager as shared_manager


@pytest.fixture
def manager():
    """A fresh, isolated SessionManager for each test."""
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


# --- creation ---------------------------------------------------------------


def test_new_session_can_be_created(manager):
    session = manager.create_session()
    assert session is not None
    assert session.session_id.startswith("SESSION-")


def test_session_ids_are_unique(manager):
    ids = {manager.create_session().session_id for _ in range(50)}
    assert len(ids) == 50


def test_new_session_is_unauthenticated(manager):
    assert manager.create_session().authenticated is False


def test_new_session_customer_id_is_none(manager):
    assert manager.create_session().customer_id is None


def test_authentication_attempts_begin_at_zero(manager):
    assert manager.create_session().authentication_attempts == 0


def test_new_session_status_is_active(manager):
    session = manager.create_session()
    assert session.status is SessionStatus.ACTIVE
    assert session.created_at is not None
    assert session.updated_at is not None


# --- retrieval --------------------------------------------------------------


def test_created_session_can_be_retrieved(manager):
    created = manager.create_session()
    assert manager.get_session(created.session_id) is created


def test_nonexistent_session_behaves_cleanly(manager):
    # Documented convention: get_session returns None, require_session raises.
    assert manager.get_session("SESSION-does-not-exist") is None
    with pytest.raises(SessionNotFoundError):
        manager.require_session("SESSION-does-not-exist")
    with pytest.raises(SessionNotFoundError):
        manager.update_session("SESSION-does-not-exist", current_domain="ACCOUNT")


# --- updates ----------------------------------------------------------------


def test_session_fields_can_be_updated(manager):
    session = manager.create_session()

    manager.update_session(
        session.session_id,
        customer_id="DEMO001",
        authenticated=True,
        authentication_attempts=1,
        current_domain="ACCOUNT",
        previous_intent="BALANCE_ENQUIRY",
        realtime_session_id="rt-placeholder",
    )

    assert session.customer_id == "DEMO001"
    assert session.authenticated is True
    assert session.authentication_attempts == 1
    assert session.current_domain == "ACCOUNT"
    assert session.previous_intent == "BALANCE_ENQUIRY"
    assert session.realtime_session_id == "rt-placeholder"


def test_updated_at_changes_after_modification(manager):
    session = manager.create_session()
    before = session.updated_at

    time.sleep(0.01)
    manager.update_session(session.session_id, current_domain="LOAN")

    assert session.updated_at > before
    assert session.created_at == session.created_at  # created_at is not reassigned


def test_session_id_cannot_be_updated(manager):
    session = manager.create_session()
    original_id = session.session_id

    with pytest.raises(ValueError):
        manager.update_session(session.session_id, session_id="SESSION-hijacked")

    assert session.session_id == original_id


# --- isolation --------------------------------------------------------------


def test_conversation_contexts_are_independent(manager):
    session_a = manager.create_session()
    session_b = manager.create_session()

    session_a.conversation_context["topic"] = "ACCOUNT"

    assert session_b.conversation_context == {}
    assert "topic" not in session_b.conversation_context
    assert session_a.conversation_context is not session_b.conversation_context


def test_two_sessions_hold_different_customer_ids(manager):
    session_a = manager.create_session()
    session_b = manager.create_session()

    manager.update_session(session_a.session_id, customer_id="DEMO001")
    manager.update_session(session_b.session_id, customer_id="DEMO002")

    assert session_a.customer_id == "DEMO001"
    assert session_b.customer_id == "DEMO002"


def test_two_sessions_hold_different_authentication_states(manager):
    session_a = manager.create_session()
    session_b = manager.create_session()

    manager.update_session(
        session_a.session_id, authenticated=True, current_domain="ACCOUNT"
    )

    assert session_a.authenticated is True
    assert session_a.current_domain == "ACCOUNT"
    assert session_b.authenticated is False
    assert session_b.current_domain is None


def test_updating_a_dict_does_not_share_state(manager):
    shared_input = {"topic": "LOAN"}
    session_a = manager.create_session()
    session_b = manager.create_session()

    manager.update_session(session_a.session_id, conversation_context=shared_input)
    manager.update_session(session_b.session_id, conversation_context=shared_input)

    session_a.conversation_context["only_a"] = True

    assert "only_a" not in session_b.conversation_context
    assert "only_a" not in shared_input


# --- teardown ---------------------------------------------------------------


def test_destroying_session_a_does_not_destroy_session_b(manager):
    session_a = manager.create_session()
    session_b = manager.create_session()

    assert manager.destroy_session(session_a.session_id) is True

    assert manager.get_session(session_a.session_id) is None
    assert manager.get_session(session_b.session_id) is session_b
    assert session_b.status is SessionStatus.ACTIVE


def test_destroyed_session_cannot_be_retrieved(manager):
    session = manager.create_session()
    manager.update_session(
        session.session_id,
        customer_id="DEMO001",
        authenticated=True,
        conversation_context={"topic": "ACCOUNT"},
    )

    manager.destroy_session(session.session_id)

    assert manager.get_session(session.session_id) is None
    # Authentication state and context are cleared on the way out.
    assert session.customer_id is None
    assert session.authenticated is False
    assert session.conversation_context == {}
    assert session.status is SessionStatus.COMPLETED
    # Destroying twice is harmless.
    assert manager.destroy_session(session.session_id) is False


def test_active_session_count_is_correct(manager):
    assert manager.active_session_count() == 0

    sessions = [manager.create_session() for _ in range(3)]
    assert manager.active_session_count() == 3
    assert len(manager.list_active_sessions()) == 3

    manager.destroy_session(sessions[0].session_id)
    assert manager.active_session_count() == 2


# --- concurrency ------------------------------------------------------------


def test_concurrent_session_creation(manager):
    with ThreadPoolExecutor(max_workers=10) as pool:
        sessions = list(pool.map(lambda _: manager.create_session(), range(10)))

    ids = {s.session_id for s in sessions}
    assert len(ids) == 10
    assert manager.active_session_count() == 10
    # No state bled between concurrently created sessions.
    assert all(s.customer_id is None and s.conversation_context == {} for s in sessions)


def test_concurrent_updates_stay_isolated(manager):
    sessions = [manager.create_session() for _ in range(10)]

    def assign(index: int) -> None:
        manager.update_session(
            sessions[index].session_id,
            customer_id=f"DEMO{index:03d}",
            conversation_context={"index": index},
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(assign, range(10)))

    for index, session in enumerate(sessions):
        assert session.customer_id == f"DEMO{index:03d}"
        assert session.conversation_context == {"index": index}


# --- development endpoints --------------------------------------------------


def test_dev_create_session_endpoint(client):
    response = client.post("/dev/sessions")
    assert response.status_code == 201

    body = response.json()
    assert body["session_id"].startswith("SESSION-")
    assert body["authenticated"] is False
    assert body["status"] == "ACTIVE"

    shared_manager.destroy_session(body["session_id"])


def test_dev_get_session_endpoint(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.get(f"/dev/sessions/{session_id}")
    assert response.status_code == 200

    body = response.json()
    assert body["session_id"] == session_id
    assert body["customer_id"] is None
    assert body["authenticated"] is False
    assert body["status"] == "ACTIVE"
    # No sensitive values are exposed.
    assert not {"pin", "pin_hash", "balance", "loans"} & set(body)

    assert client.get("/dev/sessions/SESSION-missing").status_code == 404

    shared_manager.destroy_session(session_id)


def test_dev_delete_session_endpoint(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.delete(f"/dev/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "destroyed": True}

    assert client.get(f"/dev/sessions/{session_id}").status_code == 404
    assert client.delete(f"/dev/sessions/{session_id}").status_code == 404


def test_dev_session_count_endpoint(client):
    before = client.get("/dev/sessions").json()["active_sessions"]

    first = client.post("/dev/sessions").json()["session_id"]
    second = client.post("/dev/sessions").json()["session_id"]
    assert client.get("/dev/sessions").json()["active_sessions"] == before + 2

    client.delete(f"/dev/sessions/{first}")
    assert client.get("/dev/sessions").json()["active_sessions"] == before + 1

    client.delete(f"/dev/sessions/{second}")


def test_health_endpoint_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
