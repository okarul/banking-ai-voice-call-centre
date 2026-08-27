"""Phase 6.12.1: the schema a deployment ships with must be the schema it needs.

`6a1d1af` declared `call_trace_events`, shipped code that queried it, and was
deployed. The first request for a call trace answered HTTP 500:

    psycopg.errors.UndefinedTable:
    relation "call_trace_events" does not exist

Nothing was wrong with the trace. The table had simply never been created,
because `create_tables` — the canonical additive initialiser, which does know
how to create it — was reachable only from `seed_database()`, and a deployment
runs that at most once, long before the table was declared. Production startup
did no schema work at all.

Every test in this file therefore drives the **real FastAPI lifespan**. Calling
`create_tables()` directly would prove that the initialiser works, which was
never in doubt; what needed proving is that something in production calls it.

These tests drop and recreate tables in the shared demo database. That is what
they are for.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app.database import seed
from app.database.connection import get_engine, session_scope
from app.database.models import Base, Customer
from app.main import create_app


def _existing_tables() -> set[str]:
    return set(inspect(get_engine()).get_table_names())


def _declared_tables() -> set[str]:
    return set(Base.metadata.tables)


def _drop(table: str) -> None:
    with session_scope() as db:
        db.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


@pytest.fixture(autouse=True)
def restore_schema():
    """Whatever a test drops, put back — and forget it was ever verified."""
    yield
    seed.reset_schema_guard()
    seed.create_tables()


def start_the_application() -> None:
    """Run the real production startup, exactly as uvicorn would.

    `TestClient` as a context manager is what executes the lifespan; without
    the `with`, startup never runs. That distinction is the whole defect.
    """
    with TestClient(create_app()):
        pass


# === 1. the live defect, reproduced =========================================


def test_startup_creates_a_newly_declared_table():
    """The fail-before-fix case, in the shape it took in production.

    An existing database with the old operational tables and no
    `call_trace_events`, and a release that declares one. Against `6a1d1af`
    startup leaves it missing and the first trace request is a 500.
    """
    seed.reset_schema_guard()
    _drop("call_trace_events")
    assert "call_trace_events" not in _existing_tables(), "the drop did not take"

    start_the_application()

    assert "call_trace_events" in _existing_tables(), (
        "production startup did not create a newly declared table - this is "
        "the Phase 6.12.1 live defect"
    )


def test_startup_creates_every_declared_table_not_just_the_new_one():
    """The fix must be general. No table is named in the startup path."""
    seed.reset_schema_guard()
    for table in ("call_trace_events", "agent_tool_events", "conversation_messages"):
        _drop(table)

    start_the_application()

    assert _declared_tables() <= _existing_tables(), (
        f"still missing: {sorted(_declared_tables() - _existing_tables())}"
    )


# === 2. what it must not do =================================================


def test_startup_does_not_drop_or_rewrite_existing_data():
    """The initialiser adds. It has never removed, and must not begin."""
    seed.reset_schema_guard()
    seed.create_tables()

    with session_scope() as db:
        before = sorted(c.customer_id for c in db.scalars(select(Customer)))
    assert before, "the seed data is missing, so nothing is being proved"

    _drop("call_trace_events")
    start_the_application()

    with session_scope() as db:
        after = sorted(c.customer_id for c in db.scalars(select(Customer)))

    assert after == before, "startup altered existing customer rows"


def test_repeated_startups_are_safe():
    """A restart, a redeploy, a health-check flap. All harmless."""
    seed.reset_schema_guard()
    _drop("call_trace_events")

    for _ in range(3):
        seed.reset_schema_guard()
        start_the_application()

    assert "call_trace_events" in _existing_tables()
    with session_scope() as db:
        assert db.scalars(select(Customer)).all(), "a restart lost the seed data"


def test_the_initialiser_is_idempotent():
    """Called twice in a row, the second call changes nothing and raises nothing."""
    seed.reset_schema_guard()
    seed.create_tables()
    before = _existing_tables()
    seed.create_tables()
    assert _existing_tables() == before


def test_the_schema_is_verified_once_per_process():
    """Once per process, not once per application.

    Production starts once and pays the reflection once. Tests build dozens of
    applications, and paying it on each would put tens of milliseconds on every
    one of them for a schema that cannot change under a running process.
    """
    seed.reset_schema_guard()
    calls = []

    original = seed.create_tables
    try:
        seed.create_tables = lambda: (calls.append(1), original())[1]
        seed.ensure_schema()
        seed.ensure_schema()
        seed.ensure_schema()
    finally:
        seed.create_tables = original

    assert len(calls) == 1, f"the schema was rebuilt {len(calls)} times"


def test_a_failed_verification_is_retried_rather_than_remembered():
    """A startup that failed because the database was down must not give up."""
    seed.reset_schema_guard()
    attempts = []

    original = seed.create_tables

    def failing():
        attempts.append(1)
        raise RuntimeError("database is down")

    try:
        seed.create_tables = failing
        for _ in range(2):
            with pytest.raises(RuntimeError):
                seed.ensure_schema()
    finally:
        seed.create_tables = original

    assert len(attempts) == 2, "a failed verification was remembered as done"


# === 3. indexes stay with the canonical initialiser =========================


def test_missing_indexes_are_restored_by_startup():
    """The initialiser owns columns and indexes too, and startup runs all of it."""
    seed.reset_schema_guard()
    seed.create_tables()

    with session_scope() as db:
        db.execute(text("DROP INDEX IF EXISTS ix_call_trace_events_session_sequence"))

    def indexes() -> set[str]:
        return {
            i["name"]
            for i in inspect(get_engine()).get_indexes("call_trace_events")
        }

    assert "ix_call_trace_events_session_sequence" not in indexes()

    seed.reset_schema_guard()
    start_the_application()

    assert "ix_call_trace_events_session_sequence" in indexes(), (
        "startup did not restore a declared index"
    )


# === 4. readiness must not claim a schema it does not have ==================


def test_readiness_reports_a_missing_table():
    """`SELECT 1` answering is not the same as the application being usable.

    Readiness checked only that the database replied. It would have reported
    this process ready throughout the live defect.
    """
    from app.observability import readiness

    seed.reset_schema_guard()
    _drop("call_trace_events")

    report = readiness.readiness_report()
    assert report["checks"]["database"]["ready"] is True, "the database is reachable"
    assert report["checks"]["schema"]["ready"] is False
    assert report["checks"]["schema"]["reason"] == "tables_missing"
    assert report["ready"] is False, "a process with no trace table called itself ready"

    seed.reset_schema_guard()
    start_the_application()

    healed = readiness.readiness_report()
    assert healed["checks"]["schema"]["ready"] is True
    assert healed["ready"] is True


def test_readiness_reports_a_failed_startup_initialisation():
    """And says so even while the tables happen to be present."""
    from app.observability import readiness

    try:
        readiness.record_schema_initialisation(
            ready=False, reason="initialisation_failed"
        )
        report = readiness.readiness_report()
        assert report["checks"]["schema"]["ready"] is False
        assert report["checks"]["schema"]["reason"] == "initialisation_failed"
        assert report["ready"] is False
    finally:
        readiness.record_schema_initialisation(ready=True)


def test_a_schema_failure_does_not_stop_the_process_starting(monkeypatch):
    """Liveness stays up. Restarting fixes neither a down database nor a bad
    migration, and this application deliberately starts without PostgreSQL."""
    from app.observability import readiness

    monkeypatch.setattr(
        seed, "create_tables", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )
    seed.reset_schema_guard()

    try:
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
            assert readiness.readiness_report()["checks"]["schema"]["ready"] is False
    finally:
        readiness.record_schema_initialisation(ready=True)


def test_no_connection_detail_reaches_the_startup_log(caplog):
    """A connection error renders the connection string, password included."""
    import logging

    from app.config import settings

    monkey = getattr(settings, "database_url", "") or ""
    password = ""
    if "://" in monkey and "@" in monkey:
        password = monkey.split("://", 1)[1].split("@", 1)[0].split(":")[-1]

    seed.reset_schema_guard()
    with caplog.at_level(logging.DEBUG):
        _drop("call_trace_events")
        start_the_application()

    logged = " ".join(record.getMessage() for record in caplog.records)
    if password:
        assert password not in logged, "the database password reached the log"
    assert "postgresql://" not in logged
    assert "psycopg" not in logged.lower() or "UndefinedTable" not in logged


# === 5. and the thing that was broken now works =============================


def test_a_trace_can_be_written_and_replayed_after_startup(monkeypatch):
    """The Phase 6.12 feature, end to end, on a database that lacked its table.

    No live call: this proves the deterministic half the live UAT could not,
    because on `6a1d1af` the table was not there to write to.
    """
    from app.config import settings
    from app.observability import recorder, trace
    from app.sessions import SessionManager

    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "telephony_trace_utterances", True)
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(
        settings, "telephony_webhook_secret", "startup-test-secret-not-a-credential"
    )

    seed.reset_schema_guard()
    _drop("call_trace_events")
    assert "call_trace_events" not in _existing_tables()

    # A call id of its own, per run. A provider call is claimed once and for
    # all, so a fixed id makes the second run of this test a DUPLICATE and the
    # claim creates nothing - the same trap the lockout test fell into.
    call_id = f"startup-schema-trace-{uuid.uuid4()}"
    with TestClient(create_app()) as client:
        # Startup has run; the table must exist before anything uses it.
        assert "call_trace_events" in _existing_tables()

        manager = SessionManager()
        banking = manager.create_session()
        recorder.claim_phone_call(
            banking.session_id,
            provider_call_id=call_id,
            provider_event_id=f"evt-{call_id}",
        )
        trace.record(
            banking.session_id,
            trace.TraceEvent(
                kind=trace.KIND_LIFECYCLE,
                speaker=trace.SPEAKER_SYSTEM,
                event_type="call_ended",
            ),
            session=manager.get_session(banking.session_id),
        )

        response = client.get(f"/api/telephony/calls/{call_id}/trace")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["call"]["provider_call_id"] == call_id
    assert len(body["events"]) == 1
    assert body["events"][0]["kind"] == trace.KIND_LIFECYCLE
