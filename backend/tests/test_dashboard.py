"""The operations dashboard: what it records, and what it must never record.

Two halves. The first checks that a call produces a useful operational record —
timestamps, duration, domain, tool counts, token totals, disconnect reason. The
second is the part that matters more: that no PIN, no credential and no other
customer's conversation can reach it.

The rule the whole feature rests on is that observability may not break a call.
There is a test for that too: with the database unreachable, the customer still
gets their balance.

All customers and PINs are Phase 2 synthetic seed data.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.main import app
from app.observability import recorder
from app.observability.estimates import estimate_carbon_grams, estimate_cost_usd
from app.observability.redaction import redact_transcript
from app.realtime.browser_calls import browser_call_manager
from app.sessions import session_manager

PINS = {"DEMO001": "4821", "DEMO002": "7315"}


@pytest.fixture(autouse=True)
def clean_operational_tables():
    """Each test starts with an empty operations log."""
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    asyncio.run(browser_call_manager.close_all())
    session_manager.clear()
    wipe()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def call(client, monkeypatch):
    """One started browser call, with no provider request made."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    return client.post("/api/call/start").json()


def row(agent_session_id: str) -> dict:
    with session_scope() as db:
        record = db.scalars(
            select(AgentSession).where(
                AgentSession.agent_session_id == agent_session_id
            )
        ).one()
        return {
            column.name: getattr(record, column.name)
            for column in AgentSession.__table__.columns
        }


def only_row() -> dict:
    with session_scope() as db:
        record = db.scalars(select(AgentSession).order_by(AgentSession.id)).first()
        assert record is not None, "no agent session was recorded"
        return {
            column.name: getattr(record, column.name)
            for column in AgentSession.__table__.columns
        }


# === a call produces a record ===============================================


def test_starting_a_call_creates_an_active_agent_session(call):
    record = only_row()

    assert record["agent_session_id"].startswith("AGT-")
    assert record["banking_session_id"] == call["session_id"]
    assert record["status"] == "ACTIVE"
    assert record["ended_at"] is None
    assert record["started_at"] is not None
    # Nobody has been verified yet, so there is no customer on the row.
    assert record["customer_id"] is None
    assert record["auth_status"] == "PENDING"


def test_agent_session_ids_are_sequential_and_distinct(client, call, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    client.post("/api/call/start")

    with session_scope() as db:
        ids = [r.agent_session_id for r in db.scalars(select(AgentSession))]

    assert len(set(ids)) == len(ids) == 2
    assert all(value.startswith("AGT-") for value in ids)


def test_authentication_is_recorded_only_after_the_pin_is_checked(client, call):
    session_id = call["session_id"]

    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    # Claiming an identity is not having one.
    assert only_row()["customer_id"] is None

    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_pin",
            "arguments": {"spoken_pin": PINS["DEMO001"]},
        },
    )

    record = only_row()
    assert record["customer_id"] == "DEMO001"
    assert record["authenticated"] is True
    assert record["auth_status"] == "VERIFIED"


def test_a_failed_pin_never_attaches_the_customer(client, call):
    session_id = call["session_id"]
    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_pin",
            "arguments": {"spoken_pin": "0000"},
        },
    )

    record = only_row()
    assert record["customer_id"] is None
    assert record["authenticated"] is False
    assert record["auth_status"] == "FAILED"


def test_tool_calls_are_counted_and_named_without_their_arguments(client, call):
    session_id = call["session_id"]
    for name, arguments in (
        ("submit_customer_id", {"spoken_customer_id": "DEMO001"}),
        ("submit_pin", {"spoken_pin": PINS["DEMO001"]}),
        ("get_account_balance", {"account_type": "Savings"}),
    ):
        client.post(
            "/api/call/tool",
            json={"session_id": session_id, "name": name, "arguments": arguments},
        )

    record = only_row()
    assert record["tool_call_count"] == 3

    with session_scope() as db:
        events = db.scalars(select(AgentToolEvent).order_by(AgentToolEvent.id)).all()
        names = [event.tool_name for event in events]
        columns = {column.name for column in AgentToolEvent.__table__.columns}

    assert names == ["submit_customer_id", "submit_pin", "get_account_balance"]
    # There is nowhere for an argument to be stored, which is the point.
    assert "arguments" not in columns
    assert "spoken_pin" not in columns


def test_the_turn_records_domain_and_intent(client, call):
    session_id = call["session_id"]
    client.post(
        "/api/call/scope",
        json={"session_id": session_id, "transcript": "What is my savings balance?"},
    )

    record = only_row()
    assert record["current_domain"] == "ACCOUNT"
    assert record["last_intent"] == "OWN_ACCOUNT_ENQUIRY"
    assert record["capability"] == "Account Services"


def test_a_refused_turn_shows_as_out_of_scope_not_as_banking(client, call):
    client.post(
        "/api/call/scope",
        json={
            "session_id": call["session_id"],
            "transcript": "What is the capital of France?",
        },
    )

    record = only_row()
    assert record["current_domain"] == "GENERAL/SCOPE"
    assert record["last_intent"] == "NON_BANKING_REQUEST"


def test_ending_a_call_freezes_the_record(client, call):
    session_id = call["session_id"]

    client.post(f"/api/call/end?reason=VOICE_END_CALL", json={"session_id": session_id})

    record = only_row()
    assert record["status"] == "COMPLETED"
    assert record["ended_at"] is not None
    assert record["duration_seconds"] is not None and record["duration_seconds"] >= 0
    assert record["disconnect_reason"] == "VOICE_END_CALL"


def test_an_unknown_disconnect_reason_is_not_trusted(client, call):
    client.post(
        "/api/call/end?reason=<script>", json={"session_id": call["session_id"]}
    )

    assert only_row()["disconnect_reason"] == "CUSTOMER_ENDED"


def test_ending_twice_does_not_move_the_end_time(client, call):
    session_id = call["session_id"]
    client.post("/api/call/end", json={"session_id": session_id})
    first = only_row()["ended_at"]

    client.post("/api/call/end", json={"session_id": session_id})

    assert only_row()["ended_at"] == first


def test_a_capacity_rejection_is_recorded_with_no_customer(client, monkeypatch):
    from app.config import settings

    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    monkeypatch.setattr(settings, "realtime_max_active_sessions", 1)

    client.post("/api/call/start")
    refused = client.post("/api/call/start")

    assert refused.status_code == 503
    with session_scope() as db:
        rejected = db.scalars(
            select(AgentSession).where(AgentSession.status == "REJECTED")
        ).all()

    assert len(rejected) == 1
    assert rejected[0].customer_id is None
    assert rejected[0].banking_session_id is None
    assert rejected[0].disconnect_reason == "CAPACITY_REJECTED"


# === tokens, cost and carbon ================================================


def test_token_usage_accumulates_on_the_right_session(call):
    session_id = call["session_id"]

    recorder.record_usage(session_id, input_tokens=100, output_tokens=40)
    recorder.record_usage(session_id, input_tokens=50, output_tokens=10)

    record = only_row()
    assert record["input_tokens"] == 150
    assert record["output_tokens"] == 50
    assert record["total_tokens"] == 200


def test_one_callers_tokens_never_land_on_another(client, call, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    second = client.post("/api/call/start").json()

    recorder.record_usage(call["session_id"], input_tokens=100, output_tokens=10)
    recorder.record_usage(second["session_id"], input_tokens=7, output_tokens=3)

    with session_scope() as db:
        by_banking = {
            r.banking_session_id: r.total_tokens for r in db.scalars(select(AgentSession))
        }

    assert by_banking[call["session_id"]] == 110
    assert by_banking[second["session_id"]] == 10


def test_tokens_stay_unknown_when_the_provider_reports_nothing(call):
    record = only_row()

    assert record["input_tokens"] is None
    assert record["total_tokens"] is None
    assert record["estimated_cost_usd"] is None


def test_cost_is_none_without_configured_pricing():
    assert estimate_cost_usd(1000, 500, price_input_per_mtok=None,
                             price_output_per_mtok=None) is None


def test_cost_is_computed_from_configured_pricing():
    cost = estimate_cost_usd(
        1_000_000, 1_000_000, price_input_per_mtok=5.0, price_output_per_mtok=20.0
    )

    assert cost is not None
    assert float(cost) == pytest.approx(25.0)


def test_carbon_is_none_when_estimation_is_disabled():
    assert estimate_carbon_grams(10_000, enabled=False, grams_per_ktok=1.0) is None


def test_carbon_is_none_without_a_configured_coefficient():
    assert estimate_carbon_grams(10_000, enabled=True, grams_per_ktok=None) is None


def test_carbon_uses_the_configured_assumption_when_enabled():
    grams = estimate_carbon_grams(10_000, enabled=True, grams_per_ktok=0.5)

    assert grams is not None
    assert float(grams) == pytest.approx(5.0)


# === transcript safety ======================================================


@pytest.mark.parametrize(
    "spoken",
    ["4821", "four eight two one", "my pin is 4821", "4 8 2 1", "8 7 3 0"],
)
def test_a_spoken_pin_is_never_stored(client, call, spoken):
    client.post(
        "/api/call/scope",
        json={"session_id": call["session_id"], "transcript": spoken},
    )

    with session_scope() as db:
        stored = [m.safe_content for m in db.scalars(select(ConversationMessage))]

    blob = " ".join(stored)
    assert "4821" not in blob
    assert "8730" not in blob
    for message in stored:
        assert message == "[PIN REDACTED]" or "PIN" in message or message


def test_a_spoken_customer_id_is_stored_as_a_placeholder(client, call):
    client.post(
        "/api/call/scope",
        json={"session_id": call["session_id"], "transcript": "My ID is DEMO001"},
    )

    with session_scope() as db:
        stored = [m.safe_content for m in db.scalars(select(ConversationMessage))]

    assert stored == ["[Customer ID provided]"]


def test_the_assistants_money_answers_stay_readable(client, call):
    client.post(
        "/api/call/transcript",
        json={
            "session_id": call["session_id"],
            "text": "Your Savings account ending 1001 has a balance of 12,450.75 SGD.",
        },
    )

    with session_scope() as db:
        stored = [m.safe_content for m in db.scalars(select(ConversationMessage))]

    assert "12,450.75" in stored[0]


def test_a_credential_in_a_transcript_is_scrubbed():
    line = "the key is sk-proj-AAAABBBBCCCCDDDDEEEE and the bearer token"

    assert "sk-proj" not in redact_transcript(line, role="AGENT")


def test_no_message_table_column_could_hold_reasoning():
    columns = {column.name for column in ConversationMessage.__table__.columns}

    assert "safe_content" in columns
    for forbidden in ("reasoning", "chain_of_thought", "system_prompt", "raw"):
        assert forbidden not in columns


# === admin API ==============================================================


def test_the_listing_puts_live_calls_first(client, call, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    finished = client.post("/api/call/start").json()
    client.post("/api/call/end", json={"session_id": finished["session_id"]})

    rows = client.get("/api/admin/agents").json()["sessions"]

    assert rows[0]["active"] is True
    assert rows[0]["ended_at"] is None
    assert any(row["ended_at"] is not None for row in rows)


def test_the_listing_carries_no_banking_values(client, call):
    client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "submit_pin",
            "arguments": {"spoken_pin": PINS["DEMO001"]},
        },
    )
    client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    )

    body = client.get("/api/admin/agents").text

    # The dashboard is about operations, not about anybody's money.
    assert "12450.75" not in body
    assert "12,450.75" not in body
    assert "XXXX1001" not in body
    assert PINS["DEMO001"] not in body


def test_search_finds_a_session_by_client_id(client, call):
    client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    client.post(
        "/api/call/tool",
        json={
            "session_id": call["session_id"],
            "name": "submit_pin",
            "arguments": {"spoken_pin": PINS["DEMO001"]},
        },
    )

    found = client.get("/api/admin/agents?search=DEMO001").json()

    assert found["total"] == 1
    assert found["sessions"][0]["client_id"] == "DEMO001"


def test_the_status_filter_selects_only_matching_sessions(client, call, monkeypatch):
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)
    finished = client.post("/api/call/start").json()
    client.post("/api/call/end", json={"session_id": finished["session_id"]})

    active = client.get("/api/admin/agents?status=active").json()
    completed = client.get("/api/admin/agents?status=completed").json()

    assert active["total"] == 1
    assert completed["total"] == 1
    assert active["sessions"][0]["ended_at"] is None


def test_the_summary_counts_what_is_happening(client, call):
    summary = client.get("/api/admin/dashboard/summary").json()

    assert summary["active_agents"] == 1
    assert summary["completed_calls"] == 0
    assert "capacity" in summary
    assert summary["capacity"]["state"] in {"NORMAL", "NEAR CAPACITY", "FULL"}
    assert summary["timezone"] == "Asia/Singapore"


def test_the_summary_says_when_estimates_are_unavailable(client):
    estimates = client.get("/api/admin/dashboard/summary").json()["estimates"]

    assert "not a provider-measured value" in estimates["carbon_note"]


def test_history_returns_only_the_selected_session(client, call, monkeypatch):
    """Two calls, same synthetic customer. Their conversations must not merge."""
    import app.routers.call as call_router

    async def fake_mint(**_kwargs):
        return {"value": "ek_test_not_real", "expires_at": 1}

    monkeypatch.setattr(call_router, "mint_client_secret", fake_mint)

    client.post(
        "/api/call/transcript",
        json={"session_id": call["session_id"], "text": "First call answer."},
    )
    second = client.post("/api/call/start").json()
    client.post(
        "/api/call/transcript",
        json={"session_id": second["session_id"], "text": "Second call answer."},
    )

    with session_scope() as db:
        ids = [
            (r.banking_session_id, r.agent_session_id)
            for r in db.scalars(select(AgentSession))
        ]
    first_agent = next(a for b, a in ids if b == call["session_id"])
    second_agent = next(a for b, a in ids if b == second["session_id"])

    first_history = client.get(f"/api/admin/agents/{first_agent}/history").json()
    second_history = client.get(f"/api/admin/agents/{second_agent}/history").json()

    assert [m["content"] for m in first_history["messages"]] == ["First call answer."]
    assert [m["content"] for m in second_history["messages"]] == ["Second call answer."]
    assert "Second call answer." not in str(first_history)


def test_history_for_an_unknown_session_is_a_clean_404(client):
    assert client.get("/api/admin/agents/AGT-999999/history").status_code == 404


def test_details_masks_the_banking_session_id(client, call):
    agent_id = only_row()["agent_session_id"]

    detail = client.get(f"/api/admin/agents/{agent_id}").json()

    assert detail["banking_session_masked"] is not None
    assert call["session_id"] not in str(detail)


def test_the_admin_api_is_not_reachable_from_another_host(client, monkeypatch):
    from app.routers import admin

    class Elsewhere:
        host = "203.0.113.7"

    class Request:
        client = Elsewhere()

    with pytest.raises(Exception) as error:
        admin.operator_only(Request())

    assert "403" in str(error.value) or "Operator" in str(error.value)


# === observability must never break a call ==================================


def test_a_broken_dashboard_does_not_break_the_call(client, call, monkeypatch):
    """The customer still gets their balance when the audit write fails."""
    def explode(*args, **kwargs):
        raise RuntimeError("operational database is gone")

    monkeypatch.setattr("app.observability.recorder.session_scope", explode)

    session_id = call["session_id"]
    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_customer_id",
            "arguments": {"spoken_customer_id": "DEMO001"},
        },
    )
    client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "submit_pin",
            "arguments": {"spoken_pin": PINS["DEMO001"]},
        },
    )
    answer = client.post(
        "/api/call/tool",
        json={
            "session_id": session_id,
            "name": "get_account_balance",
            "arguments": {"account_type": "Savings"},
        },
    )

    assert answer.status_code == 200
    assert answer.json()["result"]["available_balance"] == "12450.75"


def test_every_recorder_entry_point_swallows_its_own_failure(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr("app.observability.recorder.session_scope", explode)

    # None of these may raise, whatever happens underneath.
    assert recorder.start_session("SESSION-x") is None
    recorder.record_authentication("SESSION-x", customer_id="DEMO001",
                                   authenticated=True)
    recorder.record_turn("SESSION-x", domain="ACCOUNT", intent="ACCOUNT_BALANCE")
    recorder.record_tool_call("SESSION-x", "get_account_balance")
    recorder.record_usage("SESSION-x", input_tokens=1, output_tokens=1)
    recorder.record_message("SESSION-x", role="AGENT", content="hello")
    recorder.end_session("SESSION-x")
    assert recorder.record_rejection() is None


def test_a_restart_closes_out_calls_that_cannot_still_be_running(call):
    """In-memory sessions die with the process; their rows must not linger.

    Otherwise the board shows agents nobody is on, and Active Agents never
    comes back down.
    """
    assert only_row()["ended_at"] is None

    closed = recorder.reconcile_active_sessions()

    record = only_row()
    assert closed == 1
    assert record["status"] == "DISCONNECTED"
    assert record["ended_at"] is not None
    assert record["disconnect_reason"] == "FORCED_CLEANUP"
    assert record["duration_seconds"] is not None


def test_reconciling_leaves_finished_and_rejected_rows_alone(client, call):
    client.post("/api/call/end", json={"session_id": call["session_id"]})
    before = only_row()

    recorder.reconcile_active_sessions()

    after = only_row()
    assert after["ended_at"] == before["ended_at"]
    assert after["status"] == "COMPLETED"
    assert after["disconnect_reason"] == "CUSTOMER_ENDED"


def test_the_summary_shows_no_active_agents_after_reconciling(client, call):
    assert client.get("/api/admin/dashboard/summary").json()["active_agents"] == 1

    recorder.reconcile_active_sessions()

    assert client.get("/api/admin/dashboard/summary").json()["active_agents"] == 0


def test_timestamps_are_stored_in_utc(call):
    record = only_row()
    started = record["started_at"]

    assert started is not None
    aware = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - aware).total_seconds()) < 120
    assert aware.utcoffset() == timedelta(0)
