"""Phase 6.12: the per-turn trace, and what it must never contain.

Phase 6.11.1 was diagnosed from production log lines pieced together by hand,
because the operational tables could say *that* `get_account_balance` failed
twice taking four seconds and could not say *why*. This is that missing
context, kept on purpose - and the reason it can be kept is that everything in
it is a decision, not a credential.

Two things are tested here, and they are different in kind:

* **Diagnosis.** For each way a call can go, the trace tells the whole story:
  what was understood, what the gate ruled, what was owed, which tool ran, what
  it returned, how long it took, and how the call ended.
* **Privacy.** Every sensitive shape a caller or a broken backend can produce
  is checked against the stored rows, the API response *and* the logs. The
  assertions are written the hard way round - first proving the raw value is
  really present in what was said, so a test that stopped exercising the path
  would fail rather than pass vacuously.

All customers, PINs and card numbers here are synthetic.
"""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import (
    AgentSession,
    AgentToolEvent,
    CallTraceEvent,
    ConversationMessage,
)
from app.observability import business, recorder, trace
from app.realtime import tools as realtime_tools
from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import RealtimeManager
from app.sessions import SessionManager

PINS = {"DEMO001": "4821", "DEMO002": "7315"}
CALLER = "DEMO001"
OTHER = "DEMO002"

ASK_SAVINGS = "What is my savings balance?"
SPOKEN_ID = "My customer ID is DEMO zero zero one."
SPOKEN_PIN = "Four eight two one."
HEARD_ID = "DEMO zero zero one"
HEARD_PIN = "four eight two one"


@pytest.fixture(autouse=True)
def clean_tables():
    def wipe():
        with session_scope() as db:
            db.execute(delete(CallTraceEvent))
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    wipe()


@pytest.fixture
def traced(monkeypatch):
    """Utterances on, as an operator would set them for a UAT."""
    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "telephony_trace_utterances", True)


@pytest.fixture
def quiet(monkeypatch):
    """The default posture: decisions traced, speech not."""
    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "telephony_trace_utterances", False)


# --- driving one traced call -------------------------------------------------


class _Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


def run(coro):
    return asyncio.run(coro)


async def _invoke(tool, context, **arguments):
    from agents import RunContextWrapper
    from agents.tool_context import ToolContext

    payload = json.dumps(arguments)
    tool_context = ToolContext.from_agent_context(
        RunContextWrapper(context),
        tool_call_id="test-call",
        tool_name=tool.name,
        tool_arguments=payload,
    )
    result = await tool.on_invoke_tool(tool_context, payload)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result


TOOLS = {
    "get_authentication_status": realtime_tools.get_authentication_status,
    "submit_customer_id": realtime_tools.submit_customer_id,
    "submit_pin": realtime_tools.submit_pin,
    "get_account_balance": realtime_tools.get_account_balance,
    "get_recent_transactions": realtime_tools.get_recent_transactions,
    "get_loan_balance": realtime_tools.get_loan_balance,
}


class Call:
    """One telephone call, claimed and traced the way a live one is."""

    _next = 0

    def __init__(self, call_id: str | None = None):
        Call._next += 1
        self.call_id = call_id or f"trace-call-{Call._next}"
        self.manager = SessionManager()
        self.banking = self.manager.create_session()
        self.session_id = self.banking.session_id
        recorder.claim_phone_call(
            self.session_id,
            provider_call_id=self.call_id,
            provider_event_id=f"evt-{self.call_id}",
        )
        self.context = BankingRealtimeContext(
            session_id=self.session_id, manager=self.manager
        )
        self.pump = RealtimeManager(manager=self.manager)
        self._items = 0

    @property
    def session(self):
        return self.manager.get_session(self.session_id)

    def _feed(self, event):
        turn = self.pump._feed_gate(self.session_id, event)
        if turn is not None:
            business.record_turn_decision(*turn)

    def speech_started(self):
        self._feed(
            _Event(
                "raw_model_event",
                data=_Event(
                    "raw_server_event",
                    data={"type": "input_audio_buffer.speech_started"},
                ),
            )
        )

    def transcription_failed(self):
        self._feed(
            _Event(
                "raw_model_event",
                data=_Event(
                    "raw_server_event",
                    data={
                        "type": "conversation.item.input_audio_transcription.failed"
                    },
                ),
            )
        )

    def says(self, text: str, *, item_id: str | None = None):
        self._items += 1
        self.speech_started()
        self._feed(
            _Event(
                "raw_model_event",
                data=_Event(
                    "input_audio_transcription_completed",
                    transcript=text,
                    item_id=item_id or f"item-{self._items}",
                ),
            )
        )

    def tool(self, name: str, **arguments) -> dict:
        return run(_invoke(TOOLS[name], self.context, **arguments))

    def verify(self):
        self.says(SPOKEN_ID)
        self.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        self.says(SPOKEN_PIN)
        self.tool("submit_pin", spoken_pin=HEARD_PIN)

    def ends(self, reason: str = "CALLER_GOODBYE"):
        recorder.close_phone_call(self.call_id, reason=reason)
        trace.record(
            self.session_id,
            trace.TraceEvent(
                kind=trace.KIND_LIFECYCLE,
                speaker=trace.SPEAKER_SYSTEM,
                event_type="call_ended",
                idempotency_key=f"ended:{self.call_id}",
            ),
            session=self.session,
        )

    def replay(self) -> dict:
        return trace.for_call(self.call_id)

    def events(self, kind: str | None = None) -> list[dict]:
        events = self.replay()["events"]
        return [e for e in events if kind is None or e["kind"] == kind]

    def tools_named(self, name: str) -> list[dict]:
        return [e for e in self.events(trace.KIND_TOOL) if e.get("tool_name") == name]

    def close(self):
        self.manager.clear()


def stored_blob() -> str:
    """Every trace row in the database, as one string to search."""
    with session_scope() as db:
        rows = list(db.scalars(select(CallTraceEvent)))
        return json.dumps(
            [
                {
                    column.name: str(getattr(row, column.name))
                    for column in CallTraceEvent.__table__.columns
                }
                for row in rows
            ]
        )


# === 1. the trace tells the story ==========================================


@pytest.mark.trace
def test_a_happy_path_balance_call_reads_end_to_end(traced):
    """The §7 example, asserted rather than illustrated."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.tool("get_authentication_status")
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says("Goodbye.")
        call.ends("CALLER_GOODBYE")

        replay = call.replay()
        summary = replay["call"]
        assert summary["authenticated"] is True
        assert summary["customer_id"] == CALLER
        assert summary["disconnect_reason"] == "CALLER_GOODBYE"

        events = replay["events"]
        # Ordered, and ordered by sequence rather than by clock.
        assert [e["sequence"] for e in events] == sorted(
            e["sequence"] for e in events
        )

        first = events[0]
        assert first["kind"] == trace.KIND_TURN
        assert first["speaker"] == trace.SPEAKER_CUSTOMER
        assert first["utterance"] == ASK_SAVINGS
        assert first["scope_category"] == "OWN_ACCOUNT_ENQUIRY"
        assert first["intent"] == "ACCOUNT_BALANCE"
        # The enquiry the bank now owes the caller.
        assert first["pending_operation"] == "get_account_balance"
        assert first["account_type"] == "Savings"
        assert first["auth_status"] == "PENDING"

        # The two credentials are told apart, and neither is stored.
        utterances = [e.get("utterance") for e in events if e["kind"] == trace.KIND_TURN]
        assert "[Customer ID provided]" in utterances
        assert "[PIN REDACTED]" in utterances

        # The verification, and the balance that follows it.
        balance = call.tools_named("get_account_balance")
        assert len(balance) == 1
        assert balance[0]["tool_status"] == "OK"
        assert balance[0]["auth_status"] == "VERIFIED"
        assert balance[0]["customer_ref"] == CALLER
        assert balance[0]["duration_ms"] >= 0
        assert balance[0]["tool_arguments"] == '{"account_type":"Savings"}'

        assert events[-1]["kind"] == trace.KIND_LIFECYCLE
        assert events[-1]["disconnect_reason"] == "CALLER_GOODBYE"
    finally:
        call.close()


@pytest.mark.trace
def test_the_turn_not_classified_path_is_visible(traced):
    """The Phase 6.11.1 defect, as it would have appeared in a trace.

    This is the shape that took production logs and a hand-built timeline to
    find: a verified caller, an enquiry owed, and a turn nobody could read.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        # A speech onset with nothing behind it.
        call.speech_started()
        call.tool("get_account_balance", account_type="Savings")
        call.ends("PROVIDER_ENDED")

        balance = call.tools_named("get_account_balance")
        assert len(balance) == 1
        # Fixed in 6.11.1, and the trace now says so in one line.
        assert balance[0]["tool_status"] == "OK"
        assert balance[0].get("failure_reason") is None
        assert balance[0]["auth_status"] == "VERIFIED"
    finally:
        call.close()


@pytest.mark.trace
def test_a_cross_customer_refusal_names_itself(traced):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says(f"What is {OTHER}'s savings balance?")
        refused = call.tool("get_account_balance", account_type="Savings")
        call.ends()

        assert refused["success"] is False
        turns = [
            e
            for e in call.events(trace.KIND_TURN)
            if e.get("scope_category") == "CROSS_CUSTOMER_REQUEST"
        ]
        assert turns and turns[0]["scope_allowed"] is False

        failures = [
            e for e in call.tools_named("get_account_balance")
            if e["tool_status"] == "FAILED"
        ]
        assert len(failures) == 1
        assert failures[0]["failure_reason"] == "CROSS_CUSTOMER_REQUEST"
        # The refusal did not change who is on the call.
        assert failures[0]["customer_ref"] == CALLER

        blob = stored_blob()
        assert OTHER not in blob, "another customer's id reached the trace"
    finally:
        call.close()


@pytest.mark.trace
def test_an_authentication_retry_shows_both_attempts(traced):
    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says("Nine nine nine nine.")
        first = call.tool("submit_pin", spoken_pin="9999")
        call.says(SPOKEN_PIN)
        second = call.tool("submit_pin", spoken_pin=HEARD_PIN)
        call.ends()

        assert first["success"] is False
        assert second["success"] is True

        attempts = call.tools_named("submit_pin")
        assert [a["tool_status"] for a in attempts] == ["FAILED", "OK"]
        # The auth events show the transition, in order.
        auth = [e["auth_status"] for e in call.events(trace.KIND_AUTH)]
        assert auth[-1] == "VERIFIED"
        # And no attempt carried the spoken value.
        assert all(a.get("tool_arguments") is None for a in attempts)
    finally:
        call.close()


@pytest.mark.trace
def test_session_exhaustion_and_lockout_are_distinguishable(traced):
    from app.auth import authentication

    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        for _ in range(authentication.MAX_AUTHENTICATION_ATTEMPTS):
            call.says("Nine nine nine nine.")
            call.tool("submit_pin", spoken_pin="9999")
        call.ends("AUTHENTICATION_FAILED")

        attempts = call.tools_named("submit_pin")
        assert all(a["tool_status"] == "FAILED" for a in attempts)
        assert attempts[-1]["failure_reason"] == "AUTHENTICATION_LOCKED"
        assert call.events(trace.KIND_AUTH)[-1]["auth_status"] == "LOCKED"
    finally:
        call.close()


@pytest.mark.trace
def test_an_unsupported_banking_request_is_recorded_as_refused(traced):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        # No relation word: "my friend" would make this cross-customer, which
        # is a different refusal and correctly the stronger one.
        call.says("I want to transfer money.")
        call.ends()

        turns = [
            e
            for e in call.events(trace.KIND_TURN)
            if e.get("scope_category") == "UNSUPPORTED_BANKING_REQUEST"
        ]
        assert turns and turns[0]["scope_allowed"] is False
    finally:
        call.close()


@pytest.mark.trace
@pytest.mark.parametrize(
    "reason", ["CALLER_GOODBYE", "PROVIDER_HANGUP", "CALLER_HANGUP", "IDLE_TIMEOUT"]
)
def test_every_ending_reaches_the_trace(traced, reason):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.ends(reason)

        ending = call.events(trace.KIND_LIFECYCLE)
        assert len(ending) == 1, "a call ended more than once in its own trace"
        assert ending[0]["disconnect_reason"] == reason
    finally:
        call.close()


@pytest.mark.trace
@pytest.mark.parametrize("wordless", ["speech_started", "transcription_failed"])
def test_a_wordless_turn_does_not_invent_an_utterance(traced, wordless):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        before = len(call.events(trace.KIND_TURN))
        getattr(call, wordless)()
        after = call.events(trace.KIND_TURN)

        assert len(after) == before, "a turn nobody could hear became a turn"
        assert all(e.get("utterance") != "" for e in after)
    finally:
        call.close()


@pytest.mark.trace
def test_a_tool_failure_records_its_reason(traced):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says("What is my offshore balance?")
        call.tool("get_account_balance", account_type="Offshore")
        call.ends()

        failed = [
            e for e in call.tools_named("get_account_balance")
            if e["tool_status"] == "FAILED"
        ]
        assert failed and failed[0]["failure_reason"] == "ACCOUNT_NOT_FOUND"
    finally:
        call.close()


# === 2. observability consistency ==========================================


@pytest.mark.trace
def test_the_trace_does_not_inflate_the_existing_counters(traced):
    """One invocation stays one tool event and one count."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.ends()

        with session_scope() as db:
            row = db.scalars(
                select(AgentSession).where(
                    AgentSession.provider_call_id == call.call_id
                )
            ).one()
            tool_events = list(
                db.scalars(
                    select(AgentToolEvent).where(AgentToolEvent.session_pk == row.id)
                )
            )

        # submit_customer_id, submit_pin, get_account_balance.
        assert row.tool_call_count == 3
        assert len(tool_events) == 3
        assert len(call.events(trace.KIND_TOOL)) == 3
    finally:
        call.close()


@pytest.mark.trace
def test_a_repeated_event_representation_is_traced_once(traced):
    """The same utterance arrives more than once. It is one turn."""
    call = Call()
    try:
        call.says(ASK_SAVINGS, item_id="same-item")
        call.says(ASK_SAVINGS, item_id="same-item")
        call.ends()

        turns = [
            e for e in call.events(trace.KIND_TURN)
            if e.get("scope_category") == "OWN_ACCOUNT_ENQUIRY"
        ]
        assert len(turns) == 1
    finally:
        call.close()


@pytest.mark.trace
def test_the_ending_cannot_be_written_twice(traced):
    """Several paths converge on one ending. The trace shows one."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.ends("CALLER_GOODBYE")
        call.ends("CALLER_GOODBYE")
        call.ends("CALLER_GOODBYE")

        assert len(call.events(trace.KIND_LIFECYCLE)) == 1
    finally:
        call.close()


@pytest.mark.trace
def test_a_trace_outage_does_not_break_the_call(traced, monkeypatch):
    """Tracing is an operator's convenience, never the customer's problem."""

    def broken(*args, **kwargs):
        raise RuntimeError("trace store is down")

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        monkeypatch.setattr(trace, "_next_sequence", broken)

        answer = call.tool("get_account_balance", account_type="Savings")
        assert answer["success"] is True, "a tracing outage broke a banking answer"
    finally:
        call.close()


@pytest.mark.trace
def test_tracing_can_be_turned_off_entirely(monkeypatch):
    monkeypatch.setattr(settings, "trace_enabled", False)
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")

        with session_scope() as db:
            assert list(db.scalars(select(CallTraceEvent))) == []
    finally:
        call.close()


# === 3. privacy =============================================================
#
# Written the hard way round on purpose. Each test first asserts that the raw
# value really is present in what the caller said - so a test that stopped
# exercising the path would fail rather than quietly pass - and only then that
# it is absent from the stored rows, the API response and the logs.


SENSITIVE = {
    "pin": ("My PIN is 4821.", "4821"),
    "spoken_pin": ("Four eight two one.", "four eight two one"),
    "card_number": ("My card is 4111 1111 1111 1111.", "4111 1111 1111 1111"),
    "api_key": ("The key is sk-live-abcdefghijklmnop.", "sk-live-abcdefghijklmnop"),
    "bearer_token": (
        "Use Bearer abcdefghijklmnopqrstuvwxyz012345",
        "abcdefghijklmnopqrstuvwxyz012345",
    ),
    "database_url": (
        "postgresql+psycopg://postgres:hunter2secret@10.0.0.1:5432/bank",
        "hunter2secret",
    ),
}


@pytest.mark.trace
@pytest.mark.parametrize("name", sorted(SENSITIVE))
def test_a_sensitive_utterance_never_reaches_the_trace(traced, name, caplog):
    spoken, secret = SENSITIVE[name]
    # The path really does carry the raw value.
    assert secret.lower() in spoken.lower()

    call = Call()
    try:
        with caplog.at_level(logging.DEBUG):
            call.says(spoken)
            call.verify()
            call.ends()

        blob = stored_blob()
        assert secret not in blob, f"{name} reached the trace table"
        assert secret.lower() not in blob.lower(), f"{name} reached the trace table"

        replay = json.dumps(call.replay())
        assert secret.lower() not in replay.lower(), f"{name} reached the API"

        logged = " ".join(record.getMessage() for record in caplog.records)
        assert secret.lower() not in logged.lower(), f"{name} reached the logs"
    finally:
        call.close()


@pytest.mark.trace
def test_a_spoken_pin_is_never_stored_even_as_a_tool_argument(traced):
    """The one argument that must never be written down."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.ends()

        blob = stored_blob()
        for forbidden in (PINS[CALLER], HEARD_PIN, HEARD_ID):
            assert forbidden.lower() not in blob.lower()

        # And the sanitiser refuses it directly, whatever calls it.
        rendered = trace.sanitize_arguments(
            {"spoken_pin": PINS[CALLER], "account_type": "Savings"}
        )
        assert PINS[CALLER] not in rendered
        assert "[redacted]" in rendered
        assert "Savings" in rendered
    finally:
        call.close()


@pytest.mark.trace
def test_a_database_failure_reaches_the_trace_as_a_reason_not_a_statement(
    traced, monkeypatch
):
    """A broken backend must not narrate itself into the trace."""
    from sqlalchemy.exc import OperationalError

    from app.tools import accounts

    def unreachable(*args, **kwargs):
        raise OperationalError(
            "SELECT customers.pin_hash FROM customers WHERE id = %(id)s",
            {"id": CALLER},
            Exception("connection to 10.0.0.1 failed: password authentication failed"),
        )

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        monkeypatch.setattr(accounts, "get_accounts_for_customer", unreachable)
        answer = call.tool("get_account_balance", account_type="Savings")
        call.ends()

        assert answer["reason"] == realtime_tools.DATABASE_UNAVAILABLE

        failed = [
            e for e in call.tools_named("get_account_balance")
            if e["tool_status"] == "FAILED"
        ]
        assert failed and failed[0]["failure_reason"] == "DATABASE_UNAVAILABLE"

        blob = stored_blob()
        for leak in ("pin_hash", "SELECT", "10.0.0.1", "password authentication"):
            assert leak not in blob, f"{leak!r} reached the trace"
    finally:
        call.close()


@pytest.mark.trace
def test_by_default_no_spoken_word_is_stored_at_all(quiet):
    """Q-121, still true unless an operator deliberately turns it off.

    Channel 2 has never kept a spoken word. The trace does not change that by
    default - it records what the backend decided, which is not speech.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.ends()

        events = call.events()
        assert events, "the trace recorded nothing at all"
        assert all(e.get("utterance") is None for e in events)
        assert call.replay()["utterances_recorded"] is False

        # The decisions are all still there.
        assert any(e.get("scope_category") for e in events)
        assert call.tools_named("get_account_balance")[0]["tool_status"] == "OK"

        # And the old transcript table is still untouched by the telephone.
        with session_scope() as db:
            assert list(db.scalars(select(ConversationMessage))) == []
    finally:
        call.close()


@pytest.mark.trace
def test_the_balance_itself_is_never_stored(traced):
    """A trace explains a call. It is not a copy of the customer's money."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        answer = call.tool("get_account_balance", account_type="Savings")
        call.ends()

        balance = answer["available_balance"]
        assert balance, "the test stopped exercising a successful lookup"
        assert balance not in stored_blob()
        assert balance not in json.dumps(call.replay())
    finally:
        call.close()


# === 4. the read path =======================================================


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(
        settings, "telephony_webhook_secret", "trace-test-secret-not-a-credential"
    )
    from app.main import create_app

    return TestClient(create_app())


@pytest.mark.trace
def test_the_endpoint_replays_one_call_in_order(traced, client):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.ends()

        response = client.get(f"/api/telephony/calls/{call.call_id}/trace")
        assert response.status_code == 200
        body = response.json()

        assert body["call"]["provider_call_id"] == call.call_id
        assert body["call"]["customer_id"] == CALLER
        sequences = [e["sequence"] for e in body["events"]]
        assert sequences == sorted(sequences)
        assert any(e["kind"] == trace.KIND_TOOL for e in body["events"])
    finally:
        call.close()


@pytest.mark.trace
def test_an_unknown_call_is_not_confirmed_or_denied_in_detail(client):
    response = client.get("/api/telephony/calls/never-happened/trace")
    assert response.status_code == 404
    assert response.json() == {"detail": "No such call."}


# === 5. retention ===========================================================


@pytest.mark.trace
def test_expired_traces_are_purged(traced, monkeypatch):
    from datetime import datetime, timedelta, timezone

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.ends()
        assert call.events(), "nothing to purge"

        # Nothing is old enough yet.
        assert trace.purge_expired() == 0
        assert call.events()

        # A fortnight later, it is.
        later = datetime.now(timezone.utc) + timedelta(
            days=settings.trace_retention_days + 1
        )
        assert trace.purge_expired(now=later) > 0

        with session_scope() as db:
            assert list(db.scalars(select(CallTraceEvent))) == []
    finally:
        call.close()


@pytest.mark.trace
def test_retention_is_bounded_by_configuration():
    """There is no unlimited setting, and the default is conservative."""
    assert settings.trace_retention_days > 0
    assert settings.trace_retention_days <= 30


# === 6. the switch, in both directions ======================================


@pytest.mark.trace
def test_the_text_switch_is_off_by_default():
    """The default posture is the one Channel 2 has always had.

    Read from a freshly built Settings rather than the live singleton, which
    other tests monkeypatch: the question is what an unconfigured deployment
    does, not what this process currently holds.
    """
    import os

    from app.config import Settings

    for name in ("TELEPHONY_TRACE_UTTERANCES", "TRACE_ENABLED"):
        assert name not in os.environ or os.environ[name] == "", (
            f"{name} is set in this environment; the default cannot be read"
        )

    fresh = Settings()
    assert fresh.telephony_trace_utterances is False
    # Off by default for cost, not privacy: a traced event is a database write,
    # and a diagnostic must not tax the calls it exists to diagnose.
    assert fresh.trace_enabled is False
    assert fresh.trace_retention_days == 14


@pytest.mark.trace
def test_turning_text_off_again_returns_the_next_call_to_metadata_only(monkeypatch):
    """On for one UAT call, off for the next. No residue either way."""
    monkeypatch.setattr(settings, "trace_enabled", True)

    # A call recorded while the operator had text on.
    monkeypatch.setattr(settings, "telephony_trace_utterances", True)
    loud = Call()
    try:
        loud.says(ASK_SAVINGS)
        loud.verify()
        loud.ends()
        assert any(e.get("utterance") for e in loud.events(trace.KIND_TURN))
    finally:
        loud.close()

    # The switch goes off. The next call keeps its decisions and loses its
    # speech, immediately and without a restart.
    monkeypatch.setattr(settings, "telephony_trace_utterances", False)
    silent = Call()
    try:
        silent.says(ASK_SAVINGS)
        silent.verify()
        silent.tool("get_account_balance", account_type="Savings")
        silent.ends()

        events = silent.events()
        assert events, "the trace stopped recording anything at all"
        assert all(e.get("utterance") is None for e in events)
        assert silent.replay()["utterances_recorded"] is False
        # The diagnosis survives the privacy setting.
        assert any(e.get("scope_category") for e in events)
        assert silent.tools_named("get_account_balance")[0]["tool_status"] == "OK"
    finally:
        silent.close()


@pytest.mark.trace
def test_the_existing_channel_2_transcript_contract_still_holds(monkeypatch):
    """Q-121's own test, re-proved here with text switched on.

    Even when an operator opts into utterances, they go to the trace table and
    never to `conversation_messages` - so the row Q-121 protects stays empty
    and the browser's transcript remains the only thing that writes it.
    """
    monkeypatch.setattr(settings, "trace_enabled", True)
    monkeypatch.setattr(settings, "telephony_trace_utterances", True)

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.ends()

        with session_scope() as db:
            assert list(db.scalars(select(ConversationMessage))) == [], (
                "Channel 2 began persisting conversation messages"
            )
    finally:
        call.close()


# === 7. more sensitive shapes ==============================================


MORE_SENSITIVE = {
    "cvv": ("The CVV is 419.", "419"),
    "password": ("My password is hunter2secretvalue.", "hunter2secretvalue"),
    "nric": ("My NRIC is S1234567D.", "S1234567D"),
    "long_card": ("Card 5500 0000 0000 0004 expires soon.", "5500 0000 0000 0004"),
}


@pytest.mark.trace
@pytest.mark.parametrize("name", sorted(MORE_SENSITIVE))
def test_more_sensitive_shapes_never_reach_the_trace(traced, name, caplog):
    spoken, secret = MORE_SENSITIVE[name]
    assert secret.lower() in spoken.lower()

    call = Call()
    try:
        with caplog.at_level(logging.DEBUG):
            call.says(spoken)
            call.verify()
            call.tool("get_account_balance", account_type="Savings")
            call.ends()

        blob = stored_blob()
        assert secret.lower() not in blob.lower(), f"{name} reached the trace table"

        replay = json.dumps(call.replay())
        assert secret.lower() not in replay.lower(), f"{name} reached the API"

        with session_scope() as db:
            events = json.dumps(
                [
                    (e.tool_name, e.status, e.duration_ms)
                    for e in db.scalars(select(AgentToolEvent))
                ]
            )
        assert secret.lower() not in events.lower(), f"{name} reached agent_tool_events"

        logged = " ".join(record.getMessage() for record in caplog.records)
        assert secret.lower() not in logged.lower(), f"{name} reached the logs"
    finally:
        call.close()


@pytest.mark.trace
def test_redaction_happens_before_persistence_not_on_the_way_out(traced):
    """A secret written down and filtered at display time is still written down.

    Asserted against the column itself rather than the API, because the API is
    where a filter would hide the mistake.
    """
    call = Call()
    try:
        call.says("My PIN is 4821 and my card is 4111 1111 1111 1111.")
        call.ends()

        with session_scope() as db:
            stored = [row.utterance for row in db.scalars(select(CallTraceEvent))]

        assert any(stored), "nothing was stored, so nothing was proved"
        for value in stored:
            if value is None:
                continue
            assert "4821" not in value
            assert "4111" not in value
    finally:
        call.close()


# === 8. retention safety ====================================================


@pytest.mark.trace
def test_retention_never_deletes_a_live_call(traced):
    """A call in progress is inside the window by definition."""
    from datetime import datetime, timedelta, timezone

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        before = len(call.events())
        assert before

        # A purge run at the boundary must leave everything newer than it.
        just_inside = datetime.now(timezone.utc) + timedelta(
            days=settings.trace_retention_days
        ) - timedelta(seconds=5)
        trace.purge_expired(now=just_inside)

        assert len(call.events()) == before, "a live call's trace was purged"
        # And the call still works.
        assert call.tool("get_account_balance", account_type="Savings")["success"]
    finally:
        call.close()


@pytest.mark.trace
def test_a_purge_failure_cannot_end_a_call(traced, monkeypatch):
    """Retention is housekeeping. It is not allowed to hang up on anybody."""
    from app.telephony import service

    def broken(*args, **kwargs):
        raise RuntimeError("retention store is down")

    monkeypatch.setattr(trace, "purge_expired", broken)

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()

        # The sweep runs on every new call; a broken purge must not stop it.
        swept = run(service.sweep_idle_calls())
        assert swept == 0

        assert call.tool("get_account_balance", account_type="Savings")["success"]
    finally:
        call.close()


@pytest.mark.trace
def test_the_sweep_decides_before_it_purges(traced, monkeypatch):
    """Retention must not add a scheduling point ahead of the sweep's decision.

    The regression this pins: awaiting anything before the sweep has finished
    counting hands the event loop to whatever else is ending a call, and the
    sweep then reports reclaiming nothing because somebody else got there
    first. Asserted on the order of operations rather than on a timing window.
    """
    from app.telephony import service

    order = []

    real_purge = trace.purge_expired

    def watched_purge(*args, **kwargs):
        order.append("purge")
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(trace, "purge_expired", watched_purge)
    monkeypatch.setattr(settings, "telephony_idle_call_timeout", 60)

    async def scenario():
        order.append("sweep-start")
        swept = await service.sweep_idle_calls()
        order.append("sweep-done")
        return swept

    run(scenario())

    assert order == ["sweep-start", "purge", "sweep-done"], order
    # The purge is the last thing the sweep does, so it cannot change what the
    # sweep already decided.
    assert order.index("purge") == len(order) - 2
