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
import time
import uuid

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
    """Every trace row in the database, as one string to search.

    `ensure_ascii=False` is not cosmetic. With the default, a PIN transcribed
    into Urdu is escaped to `\\uXXXX` sequences and `spoken not in
    stored_blob()` can never match it - so the assertion that D-8 rests on
    would have passed over a raw non-Latin PIN sitting in the table. Phase 6.14
    found exactly that leak by another route.
    """
    with session_scope() as db:
        rows = list(db.scalars(select(CallTraceEvent)))
        return json.dumps(
            [
                {
                    column.name: str(getattr(row, column.name))
                    for column in CallTraceEvent.__table__.columns
                }
                for row in rows
            ],
            ensure_ascii=False,
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


# === Phase 6.12.2: the trace has to read like the call ======================
#
# The first live UAT passed on every banking measure - DEMO001 verified,
# `get_account_balance` OK in 11 ms, the spoken balance correct, CALLER_GOODBYE
# - and the trace of it was hard to read. Three things, none of them banking:
#
#   * one sentence appeared three times, growing, because the model streams it
#     and the snapshot handler is keyed on the text;
#   * the four digits a caller reads out when the bank asks for a PIN were
#     labelled NON_BANKING_REQUEST, which is what the gate ruled and reads like
#     a refusal of something nobody asked for;
#   * "no, that is all, thank you" missed SOCIAL - it is more than one courtesy
#     phrase - and was filed under a banking-refusal category.
#
# None of these tests touches banking behaviour, and none of them changes what
# the gate decides. They pin how a call reads afterwards.


class _AgentItem:
    """One assistant history item, as the SDK delivers it while streaming."""

    def __init__(self, item_id: str, text: str, role: str = "assistant"):
        self.item_id = item_id
        self.role = role
        self.type = "message"
        self.content = [_AgentContent(text)]


class _AgentContent:
    def __init__(self, text: str):
        self.transcript = text
        self.text = None


@pytest.fixture
def bridge_call(traced, monkeypatch):
    """A phone bridge wired to a claimed call, with tracing on."""
    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import LoopbackMediaTransport
    from app.sessions import session_manager as real_manager

    manager = SessionManager()
    banking = manager.create_session()
    call_id = f"norm-{uuid.uuid4()}"
    recorder.claim_phone_call(
        banking.session_id,
        provider_call_id=call_id,
        provider_event_id=f"evt-{call_id}",
    )

    class _Realtime:
        async def send_audio(self, *_a, **_k):
            pass

        async def send_message(self, *_a, **_k):
            pass

    bridge = PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=banking.session_id,
        transport=LoopbackMediaTransport(),
        realtime_manager=_Realtime(),
        outbound_max_frames=200,
    )
    bridge.conversation.session_manager = manager
    return bridge, call_id, manager, banking


def agent_events(call_id: str) -> list[dict]:
    replay = trace.for_call(call_id)
    return [
        e
        for e in replay["events"]
        if e["kind"] == trace.KIND_TURN and e["speaker"] == trace.SPEAKER_AGENT
    ]


# --- A/B/L: streaming collapses to one complete utterance -------------------


@pytest.mark.trace
def test_streaming_agent_chunks_collapse_to_one_final_utterance(bridge_call):
    """The live defect: one sentence, three rows."""
    bridge, call_id, _manager, _banking = bridge_call

    final = (
        "Let me check that for your savings account and then I'll share "
        "what's available."
    )
    partials = ("Let me check", "Let me check that for your", final)

    async def stream():
        for partial in partials:
            bridge._on_history_item("assistant", partial, "item-1")
        # Still nothing written: the turn is still growing.
        assert agent_events(call_id) == []
        bridge._flush_agent_turn()
        await _settle(bridge)

    asyncio.run(stream())

    written = agent_events(call_id)
    assert len(written) == 1, f"one sentence became {len(written)} rows"
    assert written[0]["utterance"] == final, "the stored utterance is a draft"


@pytest.mark.trace
def test_a_new_turn_flushes_the_previous_one(bridge_call):
    """Two sentences, no generation boundary between them, two rows."""
    bridge, call_id, _manager, _banking = bridge_call

    drive(
        bridge,
        [
            ("assistant", "Alright, I'll confirm", "item-1"),
            ("assistant", "Alright, I'll confirm that now.", "item-1"),
            ("assistant", "Your balance is available.", "item-2"),
        ],
    )

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["Alright, I'll confirm that now.", "Your balance is available."]


@pytest.mark.trace
def test_an_out_of_order_snapshot_cannot_truncate_the_turn(bridge_call):
    """Snapshots carry the whole conversation; their order is not guaranteed."""
    bridge, call_id, _manager, _banking = bridge_call

    drive(
        bridge,
        [
            ("assistant", "The full sentence, complete.", "item-1"),
            ("assistant", "The full", "item-1"),
        ],
    )

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == "The full sentence, complete."


@pytest.mark.trace
def test_flushing_twice_writes_one_row(bridge_call):
    """Generation end and call close both flush. That is one turn, not two."""
    bridge, call_id, _manager, _banking = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "Thank you for calling.", "item-1")
        bridge._flush_agent_turn()
        bridge._flush_agent_turn()
        await _settle(bridge)

    asyncio.run(scenario())

    assert len(agent_events(call_id)) == 1


@pytest.mark.trace
def test_the_greeting_cue_is_never_traced_as_speech(bridge_call):
    """The cue is a synthetic user turn this module injects, not a caller."""
    from app.telephony.bridge import GREETING_CUE

    bridge, call_id, _manager, _banking = bridge_call

    drive(bridge, [("assistant", GREETING_CUE, "item-1")])

    assert agent_events(call_id) == []


async def _settle(bridge) -> None:
    """Let the scheduled trace writes finish."""
    for _ in range(200):
        if not bridge._transitions:
            return
        await asyncio.sleep(0.01)


def drive(bridge, steps) -> None:
    """Feed history items to the bridge inside one event loop.

    `_schedule` needs a running loop - it is called from the event pump, which
    has one - so a test that drives the handler synchronously schedules nothing
    and proves nothing.
    """

    async def scenario():
        for role, text, item_id in steps:
            bridge._on_history_item(role, text, item_id)
        bridge._flush_agent_turn()
        await _settle(bridge)

    asyncio.run(scenario())


# --- C/D/E: authentication inputs read as authentication --------------------


@pytest.mark.trace
def test_a_customer_id_turn_is_recorded_as_an_auth_input(traced):
    call = Call()
    try:
        call.says(SPOKEN_ID)
        turn = call.events(trace.KIND_TURN)[-1]

        assert turn["event_type"] == trace.EVENT_AUTH_INPUT
        assert turn["intent"] == trace.INTENT_CUSTOMER_ID_INPUT
        assert turn["domain"] == trace.DOMAIN_AUTHENTICATION
        assert turn["utterance"] == "[Customer ID provided]"
    finally:
        call.close()


@pytest.mark.trace
def test_a_pin_turn_is_recorded_as_an_auth_input_and_stays_redacted(traced):
    """The turn is named. The digits are not."""
    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(SPOKEN_PIN)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["event_type"] == trace.EVENT_AUTH_INPUT
        assert turn["intent"] == trace.INTENT_PIN_INPUT
        assert turn["domain"] == trace.DOMAIN_AUTHENTICATION
        assert turn["utterance"] == "[PIN REDACTED]"

        blob = stored_blob()
        assert PINS[CALLER] not in blob
        assert "four eight two one" not in blob.lower()
    finally:
        call.close()


@pytest.mark.trace
def test_the_gate_ruling_is_still_recorded_beside_the_description(traced):
    """Describing a turn must not erase what the gate decided about it.

    The whole of Phase 6.11 was diagnosed by reading these two fields. A PIN
    turn really is ruled NON_BANKING_REQUEST, and a trace that hid that would
    have hidden the defect.
    """
    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(SPOKEN_PIN)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["scope_category"] == "NON_BANKING_REQUEST"
        assert turn["scope_allowed"] is False
        # And the description says what it was.
        assert turn["intent"] == trace.INTENT_PIN_INPUT
    finally:
        call.close()


@pytest.mark.trace
def test_a_banking_question_is_not_mistaken_for_an_auth_input(traced):
    """Being mid-authentication does not make every turn a credential."""
    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(ASK_SAVINGS)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["event_type"] == trace.EVENT_CALLER_TURN
        assert turn["intent"] == "ACCOUNT_BALANCE"
    finally:
        call.close()


# --- F/G: closing and social ------------------------------------------------


@pytest.mark.trace
@pytest.mark.parametrize(
    "goodbye",
    [
        "Goodbye.",
        "No, that is all. Thank you.",
        "That is all, thanks. Goodbye.",
        "Nothing else, thanks.",
    ],
)
def test_a_closing_turn_is_not_recorded_as_unsupported_banking(traced, goodbye):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says(goodbye)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["event_type"] == trace.EVENT_CLOSING
        assert turn["intent"] == "END_CALL"
        assert turn["domain"] == trace.DOMAIN_CLOSING
    finally:
        call.close()


@pytest.mark.trace
@pytest.mark.parametrize(
    "utterance, expected", [("Hello.", "GREETING"), ("Thank you.", "THANKS")]
)
def test_social_turns_say_which_courtesy_they_were(traced, utterance, expected):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says(utterance)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["event_type"] == trace.EVENT_SOCIAL
        assert turn["intent"] == expected
        assert turn["domain"] == trace.DOMAIN_SOCIAL
    finally:
        call.close()


# --- H/I/J: order, tools and counters unchanged -----------------------------


@pytest.mark.trace
def test_normalisation_leaves_order_tools_and_counters_alone(traced):
    """Presentation changed. Nothing else did."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says("Goodbye.")
        call.ends("CALLER_GOODBYE")

        events = call.replay()["events"]
        sequences = [e["sequence"] for e in events]
        assert sequences == sorted(sequences), "replay order is not deterministic"

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

        assert row.tool_call_count == 3
        assert len(tool_events) == 3
        assert len(call.events(trace.KIND_TOOL)) == 3
        assert len(call.tools_named("get_account_balance")) == 1
    finally:
        call.close()


@pytest.mark.trace
def test_the_call_summary_is_derived_not_invented(traced):
    """The outcome comes from the events, and the session fields stay as they were.

    `current_domain` and `last_intent` are deliberately the *last turn* - a
    refused turn shows as GENERAL/SCOPE so an operator sees it was turned away.
    That is proven behaviour and is not rewritten to look tidier.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says("Goodbye.")
        call.ends("CALLER_GOODBYE")

        replay = call.replay()
        summary = replay["summary"]

        assert summary["verified"] is True
        assert summary["answered"] == 1
        assert summary["refused"] == 0
        assert summary["operations"] == ["get_account_balance"]
        assert summary["ended"] == "CALLER_GOODBYE"
        assert summary["turns"] >= 4

        # And the session's own fields are untouched by any of it.
        assert replay["call"]["authenticated"] is True
        assert replay["call"]["customer_id"] == CALLER
    finally:
        call.close()


@pytest.mark.trace
def test_the_summary_reports_a_refusal_without_hiding_it(traced):
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says(f"What is {OTHER}'s savings balance?")
        call.tool("get_account_balance", account_type="Savings")
        call.ends("CALLER_GOODBYE")

        summary = call.replay()["summary"]
        assert summary["answered"] == 1
        assert summary["refused"] == 1
        assert summary["refusal_reasons"] == ["CROSS_CUSTOMER_REQUEST"]
    finally:
        call.close()


# === Phase 6.12.3: a credential is what the bank is waiting for ============
#
# Live call `7d67837b-1c7f-1240-4790-eaa5afddeeef` passed on every banking
# measure - DEMO001 verified, `get_account_balance` OK in 11 ms, SGD 12,450.75
# spoken correctly, CALLER_GOODBYE - and its trace contained the caller's PIN
# in clear:
#
#     customer utterance: "فور ایٹ ٹو ون"      (four eight two one, Urdu script)
#     submit_pin        : OK, on the same turn
#
# The authentication path understood it. `looks_like_pin` did not: it knows
# Latin digits and English number words, saw neither, and `redact_transcript`
# returned the line verbatim. No amount of extra number words fixes that - a
# PIN can arrive in any language, any script, mis-transcribed, or as digits.
#
# The only thing that reliably identifies a credential is that the bank asked
# for one and has not had it yet. That is what these tests pin.

PIN_UTTERANCES = {
    "urdu_script": "فور ایٹ ٹو ون",
    "hindi_script": "चार आठ दो एक",
    "tamil_script": "நான்கு எட்டு இரண்டு ஒன்று",
    "arabic_digits": "٤٨٢١",
    "latin_digits": "4821",
    "spoken_english": "four eight two one",
    "punctuated": "4-8-2-1.",
    "garbled_asr": "for ate to won",
    "unexpected_language": "vier acht zwei eins",
    "spaced_digits": "4 8 2 1",
}


@pytest.mark.trace
@pytest.mark.parametrize("shape", sorted(PIN_UTTERANCES))
def test_a_pin_is_redacted_whatever_language_it_arrives_in(traced, shape, caplog):
    """The fail-before-fix case, in ten shapes.

    Before the fix only the Latin ones were caught. The live call arrived in
    the first of these and was stored word for word.
    """
    spoken = PIN_UTTERANCES[shape]

    call = Call()
    try:
        with caplog.at_level(logging.DEBUG):
            call.says(ASK_SAVINGS)
            call.says(SPOKEN_ID)
            call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
            # The bank is now waiting for a PIN. Whatever arrives is a PIN.
            call.says(spoken)

            turn = call.events(trace.KIND_TURN)[-1]
            assert turn["utterance"] == "[PIN REDACTED]", (
                f"{shape} was stored as {turn['utterance']!r}"
            )
            assert turn["intent"] == trace.INTENT_PIN_INPUT
            assert turn["event_type"] == trace.EVENT_AUTH_INPUT
            assert turn["domain"] == trace.DOMAIN_AUTHENTICATION

        # Nowhere at all: not the rows, not the replay, not the logs.
        assert spoken not in stored_blob(), f"{shape} reached the trace table"
        assert spoken not in json.dumps(call.replay(), ensure_ascii=False), (
            f"{shape} reached the API"
        )
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert spoken not in logged, f"{shape} reached the logs"

        with session_scope() as db:
            events = json.dumps(
                [(e.tool_name, e.status) for e in db.scalars(select(AgentToolEvent))]
            )
        assert spoken not in events
    finally:
        call.close()


@pytest.mark.trace
def test_the_credential_rule_reads_state_not_words(traced):
    """Stated directly, because it is the whole of the fix."""
    from app.observability import trace as trace_module

    call = Call()
    try:
        session = call.session
        # Nobody has identified themselves: no credential is expected.
        assert trace_module.expected_credential(session, None) is None

        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        # Now one is, and it stays expected until the PIN is accepted.
        assert (
            trace_module.expected_credential(call.session, None)
            == trace_module.CREDENTIAL_PIN
        )

        call.says(SPOKEN_PIN)
        call.tool("submit_pin", spoken_pin=HEARD_PIN)
        assert call.session.authenticated is True
        assert trace_module.expected_credential(call.session, None) is None
    finally:
        call.close()


@pytest.mark.trace
def test_a_banking_question_asked_mid_verification_is_not_swallowed(traced):
    """Over-redaction has a limit: a question the caller plainly asked.

    Four digits cannot become a balance enquiry - a supported intent needs an
    action word and a domain - so nothing credential-shaped leaves by this
    door.
    """
    call = Call()
    try:
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(ASK_SAVINGS)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["utterance"] == ASK_SAVINGS
        assert turn["intent"] == "ACCOUNT_BALANCE"
        assert turn["event_type"] == trace.EVENT_CALLER_TURN
    finally:
        call.close()


@pytest.mark.trace
def test_a_customer_id_turn_is_masked_from_state_too(traced):
    """The bank is holding an enquiry it cannot answer: this turn is the id."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        # The bank has asked who is calling. Whatever comes back is the answer,
        # in whatever script it was transcribed into.
        call.says("ڈیمو زیرو زیرو ون")

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["utterance"] == "[Customer ID provided]"
        assert turn["intent"] == trace.INTENT_CUSTOMER_ID_INPUT
        assert turn["event_type"] == trace.EVENT_AUTH_INPUT
        assert turn["domain"] == trace.DOMAIN_AUTHENTICATION
    finally:
        call.close()


# --- E/F: the caller comes before the tool they caused ----------------------


@pytest.mark.trace
def test_an_auth_turn_is_recorded_before_the_tool_it_causes(traced):
    """The live trace read backwards: tool, auth, then the words that caused them.

    A caller turn is ruled synchronously in the event pump, before the model can
    reach for anything; its row is written later, on a worker thread. The replay
    position is now taken when the turn is ruled, so the order is the order it
    happened in.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(SPOKEN_PIN)
        call.tool("submit_pin", spoken_pin=HEARD_PIN)
        call.ends()

        story = [
            (e["kind"], e.get("intent") or e.get("tool_name") or e.get("auth_status"))
            for e in call.replay()["events"]
        ]

        def position(kind, name):
            return next(
                i for i, (k, n) in enumerate(story) if k == kind and n == name
            )

        # customer id spoken -> submit_customer_id -> auth transition
        assert (
            position(trace.KIND_TURN, trace.INTENT_CUSTOMER_ID_INPUT)
            < position(trace.KIND_TOOL, "submit_customer_id")
        ), story
        assert (
            position(trace.KIND_TOOL, "submit_customer_id")
            < position(trace.KIND_AUTH, "PENDING")
        ), story

        # PIN spoken -> submit_pin -> VERIFIED
        assert (
            position(trace.KIND_TURN, trace.INTENT_PIN_INPUT)
            < position(trace.KIND_TOOL, "submit_pin")
        ), story
        assert (
            position(trace.KIND_TOOL, "submit_pin")
            < position(trace.KIND_AUTH, "VERIFIED")
        ), story
    finally:
        call.close()


@pytest.mark.trace
def test_replay_order_is_still_strictly_increasing(traced):
    """Reserving a position early must not collide with anything written later."""
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.verify()
        call.tool("get_account_balance", account_type="Savings")
        call.says("Goodbye.")
        call.ends()

        sequences = [e["sequence"] for e in call.replay()["events"]]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences)), "two events share a position"
    finally:
        call.close()


# --- G/H/I: one completed sentence per response -----------------------------


@pytest.mark.trace
def test_a_completed_message_is_written_as_soon_as_it_is_complete(bridge_call):
    """The provider says when a message is finished. That is the signal.

    Generation end is not: it arrives while the transcript is still being
    filled in, which is how the live trace ended up holding
    "Thank you for calling ABC".
    """
    bridge, call_id, _manager, _banking = bridge_call
    final = "Let's look at that together and I'll share what's available."

    drive_with_status(
        bridge,
        [
            ("Let's look at that together", "in_progress"),
            (final, "completed"),
        ],
    )

    written = agent_events(call_id)
    assert len(written) == 1, f"one response became {len(written)} rows"
    assert written[0]["utterance"] == final


@pytest.mark.trace
def test_the_closing_sentence_is_never_truncated(bridge_call):
    """The live goodbye was stored as "Thank you for calling ABC"."""
    bridge, call_id, _manager, _banking = bridge_call
    final = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."

    drive_with_status(
        bridge,
        [
            ("Thank you for calling ABC", "in_progress"),
            ("Thank you for calling ABC Demo Bank. Have a", "in_progress"),
            (final, "completed"),
        ],
    )

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == final


@pytest.mark.trace
def test_generation_end_does_not_write_an_unfinished_message(bridge_call):
    """The old flush point, proved harmless: an in-progress message waits."""
    bridge, call_id, _manager, _banking = bridge_call

    async def scenario():
        bridge._on_history_item(
            "assistant", "Thanks, I'll confirm that and then share", "i1", "in_progress"
        )
        # Generation end arrives while the transcript is still filling in.
        await bridge._generation_finished()
        assert agent_events(call_id) == [], "an unfinished message was written"

        finished = "Thanks, I'll confirm that and then share your balance."
        bridge._on_history_item("assistant", finished, "i1", "completed")
        # Phase 6.14: the item being finished is not the sentence being
        # finished. The provider says the second thing separately.
        final_transcript(bridge, "i1", finished)
        await _settle(bridge)

    asyncio.run(scenario())

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"].endswith("your balance.")


@pytest.mark.trace
def test_a_message_that_never_reports_a_status_is_still_written(bridge_call):
    """A transport that says nothing must not lose the turn entirely."""
    bridge, call_id, _manager, _banking = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "A complete sentence.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == "A complete sentence."


@pytest.mark.trace
def test_two_completed_responses_are_two_rows_and_no_more(bridge_call):
    bridge, call_id, _manager, _banking = bridge_call

    drive_with_status(
        bridge,
        [
            ("First answer", "in_progress"),
            ("First answer, complete.", "completed"),
        ],
    )
    drive_with_status(
        bridge,
        [
            ("Second answer", "in_progress"),
            ("Second answer, complete.", "completed"),
        ],
        item_id="i2",
    )

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["First answer, complete.", "Second answer, complete."]


def drive_with_status(bridge, steps, item_id: str = "i1") -> None:
    """Feed assistant snapshots with their completion status, in one loop.

    A `completed` step also emits the provider's final transcript for that
    item, because that is the order a real audio response arrives in:
    `response.output_audio_transcript.done` carries the finished sentence, and
    `response.output_item.done` marks the item finished. Phase 6.14 made the
    first of those the signal, so a harness that sent only the second would be
    modelling a provider that does not exist.
    """

    async def scenario():
        for text, status in steps:
            bridge._on_history_item("assistant", text, item_id, status)
            if status in ("completed", "incomplete"):
                final_transcript(bridge, item_id, text)
        await _settle(bridge)

    asyncio.run(scenario())


@pytest.mark.trace
def test_the_turn_keeps_its_place_when_the_tool_runs_first(traced):
    """The live ordering defect, reproduced the way it actually happens.

    The pump rules a caller turn synchronously and then hands the *write* to a
    worker thread. The model, meanwhile, is already calling the tool that turn
    asked for. So the write can land after the tool - which is why the live
    replay read

        submit_customer_id  TOOL
        AUTH
        CUSTOMER_ID_INPUT   TURN

    `Call.says` writes synchronously and cannot show this; here the two are
    deliberately interleaved the way the event loop interleaves them.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)

        # The pump rules the turn...
        ruled = call.pump._feed_gate(
            call.session_id,
            _Event(
                "raw_model_event",
                data=_Event(
                    "input_audio_transcription_completed",
                    transcript=SPOKEN_ID,
                    item_id="id-turn",
                ),
            ),
        )
        assert ruled is not None

        # ...the model calls the tool before the write reaches the database...
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)

        # ...and only then is the turn written down.
        business.record_turn_decision(*ruled)

        story = [
            (e["kind"], e.get("intent") or e.get("tool_name"))
            for e in call.replay()["events"]
        ]
        turn_at = next(
            i
            for i, (k, n) in enumerate(story)
            if k == trace.KIND_TURN and n == trace.INTENT_CUSTOMER_ID_INPUT
        )
        tool_at = next(
            i
            for i, (k, n) in enumerate(story)
            if k == trace.KIND_TOOL and n == "submit_customer_id"
        )
        assert turn_at < tool_at, (
            f"the caller's words were recorded after the tool they caused: {story}"
        )
    finally:
        call.close()


@pytest.mark.trace
def test_a_pin_written_down_after_it_was_accepted_is_still_redacted(traced):
    """The subtlest form of the live leak, and the one a fix can reintroduce.

    The pump rules the PIN turn, the model calls `submit_pin`, the PIN is
    accepted, the session becomes authenticated - and only then does the write
    reach the database. A redaction rule that asks "is a credential expected?"
    at *write* time answers no, because the PIN has just been accepted, and
    stores the caller's PIN in clear. The expectation is frozen when the turn
    is ruled, which is the only moment it is true.
    """
    spoken = "فور ایٹ ٹو ون"

    call = Call()
    try:
        call.says(ASK_SAVINGS)
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)

        # The pump rules the PIN turn...
        ruled = call.pump._feed_gate(
            call.session_id,
            _Event(
                "raw_model_event",
                data=_Event(
                    "input_audio_transcription_completed",
                    transcript=spoken,
                    item_id="pin-turn",
                ),
            ),
        )
        assert ruled is not None

        # ...the PIN is accepted, and the caller becomes verified...
        assert call.tool("submit_pin", spoken_pin=HEARD_PIN)["success"] is True
        assert call.session.authenticated is True

        # ...and only now is the turn written down.
        business.record_turn_decision(*ruled)

        pin_turns = [
            e
            for e in call.events(trace.KIND_TURN)
            if e.get("intent") == trace.INTENT_PIN_INPUT
        ]
        assert pin_turns, "the PIN turn was not recorded as one"
        assert pin_turns[-1]["utterance"] == "[PIN REDACTED]"
        assert spoken not in stored_blob(), "the PIN was stored in clear"
        assert spoken not in json.dumps(call.replay(), ensure_ascii=False)
    finally:
        call.close()


# === Phase 6.13: a finished assistant turn is finished for good ============
#
# Live call `0989e07a-1c91-1240-4790-eaa5afddeeef` stored its goodbye three
# times over:
#
#   1. "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
#   2. "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodb"
#   3. "Welcome to ABC Demo Bank. Thank you for calling. How may I assist..."
#
# One root cause behind all three: **completion was not terminal.**
# `_flush_agent_turn` cleared the buffer and remembered nothing, so a later
# snapshot for an item that had already been written simply re-created the
# buffer - and the "longest text wins" guard only ever applied *within* one
# held entry, so a shorter, later snapshot won by default. Teardown then
# flushed whatever happened to be held, which is how a truncated duplicate and
# a greeting from the top of the call arrived after the goodbye.


@pytest.mark.trace
def test_one_message_streamed_in_pieces_is_one_row(bridge_call):
    bridge, call_id, _m, _b = bridge_call
    final = "Let me check that for your savings account."

    drive_with_status(
        bridge,
        [("Let me check", "in_progress"),
         ("Let me check that for your", "in_progress"),
         (final, "completed")],
    )

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == final


@pytest.mark.trace
def test_two_messages_in_sequence_are_two_rows(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    drive_with_status(bridge, [("First, complete.", "completed")], item_id="i1")
    drive_with_status(bridge, [("Second, complete.", "completed")], item_id="i2")

    assert [e["utterance"] for e in agent_events(call_id)] == [
        "First, complete.",
        "Second, complete.",
    ]


@pytest.mark.trace
def test_a_completed_item_is_not_written_again_by_generation_end(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "All done.", "i1", "completed")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    assert len(agent_events(call_id)) == 1


@pytest.mark.trace
def test_a_completed_item_is_not_written_again_by_teardown(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "All done.", "i1", "completed")
        await _settle(bridge)
        # The same item offered once more as the call winds down.
        bridge._on_history_item("assistant", "All done.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    assert len(agent_events(call_id)) == 1


@pytest.mark.trace
def test_a_completed_goodbye_survives_generation_end_intact(bridge_call):
    """Live row 1 kept, live row 2 prevented."""
    bridge, call_id, _m, _b = bridge_call
    goodbye = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."

    async def scenario():
        bridge._on_history_item("assistant", goodbye, "goodbye-item", "completed")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == goodbye


@pytest.mark.trace
def test_a_truncated_snapshot_after_completion_is_ignored(bridge_call):
    """Live row 2, exactly: the same item, arriving again and shorter."""
    bridge, call_id, _m, _b = bridge_call
    goodbye = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
    truncated = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodb"

    async def scenario():
        bridge._on_history_item("assistant", goodbye, "goodbye-item", "completed")
        await _settle(bridge)
        # Out of order, shorter, and carrying no status - which is what a
        # `history_added` item looks like, and what reaches the fallback.
        bridge._on_history_item("assistant", truncated, "goodbye-item", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [goodbye], f"a finished turn was rewritten: {written}"


@pytest.mark.trace
def test_a_stale_greeting_is_never_replayed(bridge_call):
    """Live row 3, exactly: the top of the call arriving after the goodbye."""
    bridge, call_id, _m, _b = bridge_call
    greeting = "Welcome to ABC Demo Bank. Thank you for calling. How may I assist you today?"
    goodbye = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."

    async def scenario():
        bridge._on_history_item("assistant", greeting, "greeting-item", "completed")
        await _settle(bridge)
        bridge._on_history_item("assistant", goodbye, "goodbye-item", "completed")
        await _settle(bridge)
        # A late snapshot re-offers the greeting, with no status, and the
        # model finishes generating.
        bridge._on_history_item("assistant", greeting, "greeting-item", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [greeting, goodbye], f"the call replayed itself: {written}"


@pytest.mark.trace
def test_only_the_current_item_is_flushed_at_teardown(bridge_call):
    """A finished item and an unfinished one. Teardown owes only the unfinished."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "Finished answer.", "i1", "completed")
        await _settle(bridge)
        bridge._on_history_item("assistant", "Still speaking", "i2", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    assert [e["utterance"] for e in agent_events(call_id)] == [
        "Finished answer.",
        "Still speaking",
    ]


@pytest.mark.trace
def test_a_transport_that_never_reports_completion_still_writes_once(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "A whole sentence.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)
        # And once more, after the fallback already owed it.
        bridge._on_history_item("assistant", "A whole sentence.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == "A whole sentence."


@pytest.mark.trace
def test_a_completion_arriving_after_the_fallback_wrote_it_is_ignored(bridge_call):
    """The fallback already owed this item. The provider agreeing changes nothing."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "A whole sentence.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)
        bridge._on_history_item("assistant", "A whole sentence.", "i1", "completed")
        await _settle(bridge)

    asyncio.run(scenario())

    assert len(agent_events(call_id)) == 1


@pytest.mark.trace
def test_many_responses_with_distinct_ids_are_each_stored_once(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        for n in range(5):
            bridge._on_history_item("assistant", f"Answer {n}.", f"i{n}", "completed")
            await _settle(bridge)
        # Every one of them offered again, at the end of the call.
        for n in range(5):
            bridge._on_history_item("assistant", f"Answer {n}.", f"i{n}", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [f"Answer {n}." for n in range(5)], written


@pytest.mark.trace
def test_an_interrupted_response_is_written_once_and_not_replayed(bridge_call):
    """Barge-in: the turn stops mid-sentence. It is still one turn."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "Your balance is", "i1", "in_progress")
        # The caller talks over it; the model abandons the response.
        bridge._on_history_item("assistant", "Your balance is", "i1", "incomplete")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["Your balance is"], written


@pytest.mark.trace
def test_the_longest_text_still_wins_before_the_turn_is_written(bridge_call):
    """Out-of-order snapshots before completion must not truncate the row."""
    bridge, call_id, _m, _b = bridge_call

    drive_with_status(
        bridge,
        [("The complete sentence, all of it.", "in_progress"),
         ("The complete", "in_progress"),
         ("The complete sentence, all of it.", "completed")],
    )

    written = agent_events(call_id)
    assert len(written) == 1
    assert written[0]["utterance"] == "The complete sentence, all of it."


# === Phase 6.14: the caller's words come before the tool they cause =========
#
# Live call `9cf4e328-1cac-1240-4790-eaa5afddeeef` passed on banking, on PIN
# privacy, on non-Latin scope and on stale replay - and read like this:
#
#     9   TOOL     submit_customer_id  OK
#     10  AUTH     PENDING
#     11  CUSTOMER "[PIN REDACTED]"   intent=PIN_INPUT
#
# The caller turn at 11 is the one that *supplied the customer id*. It should
# have been `[Customer ID provided]` / `CUSTOMER_ID_INPUT`, and it should have
# come first.
#
# Phase 6.12.3 froze the credential expectation at the moment the turn is
# ruled, which fixed the deferred write. What remains is that on a voice call
# **the ruling itself is late**. The model is given the caller's audio
# directly and can call `submit_customer_id` as soon as it has heard enough;
# the transcript arrives from a separate ASR pass afterwards. By the time
# `reserve_turn` asks "what credential is expected?", `submit_customer_id` has
# already set `candidate_customer_id` - and that window *is* "waiting for a
# PIN".
#
# The two turns of that same call prove it is a race and not a second code
# path: on the PIN turn the transcript won, so the PIN was redacted correctly;
# on the customer-id turn the tool won, so the id turn was relabelled a PIN
# turn and filed behind the tool it caused. Same code, same call.
#
# `Call.says` fires the onset and the transcript together, which is only ever
# the ordering the transcript wins. These tests drive the other one.


class RacingCall(Call):
    """A call where the model's tool call and the transcript race."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._deferred = []

    def starts_speaking(self) -> None:
        """VAD onset: the caller has begun. No transcript exists yet."""
        self.speech_started()

    def transcript_arrives(self, text: str, *, item_id=None, defer=False) -> None:
        """The ASR result for speech that began earlier."""
        self._items += 1
        event = _Event(
            "raw_model_event",
            data=_Event(
                "input_audio_transcription_completed",
                transcript=text,
                item_id=item_id or f"item-{self._items}",
            ),
        )
        turn = self.pump._feed_gate(self.session_id, event)
        if turn is None:
            return
        if defer:
            self._deferred.append(turn)
        else:
            business.record_turn_decision(*turn)

    def worker_catches_up(self) -> None:
        """The off-loop write, which lands whenever it lands."""
        while self._deferred:
            business.record_turn_decision(*self._deferred.pop(0))


def _story(call):
    return [
        (e["kind"], e.get("intent") or e.get("tool_name") or e.get("auth_status"))
        for e in call.replay()["events"]
    ]


def _position(story, kind, name):
    return next(i for i, (k, n) in enumerate(story) if k == kind and n == name)


@pytest.mark.trace
def test_the_id_turn_is_reserved_before_the_tool_mutates_auth_state(traced):
    """The live ordering, exactly: onset, tool, then the transcript."""
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)

        call.starts_speaking()
        # The model heard the audio and acted on it before ASR came back.
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        assert call.session.candidate_customer_id == "DEMO001"
        call.transcript_arrives(SPOKEN_ID)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["intent"] == trace.INTENT_CUSTOMER_ID_INPUT, (
            "the id turn was relabelled a PIN turn by state it preceded"
        )
        assert turn["utterance"] == "[Customer ID provided]"
        assert turn["event_type"] == trace.EVENT_AUTH_INPUT
        assert turn["domain"] == trace.DOMAIN_AUTHENTICATION
    finally:
        call.close()


@pytest.mark.trace
def test_the_id_turn_is_replayed_before_the_tool_event(traced):
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.transcript_arrives(SPOKEN_ID)
        call.ends()

        story = _story(call)
        assert (
            _position(story, trace.KIND_TURN, trace.INTENT_CUSTOMER_ID_INPUT)
            < _position(story, trace.KIND_TOOL, "submit_customer_id")
        ), story
    finally:
        call.close()


@pytest.mark.trace
def test_the_id_turn_is_replayed_before_the_auth_transition(traced):
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.transcript_arrives(SPOKEN_ID)
        call.ends()

        story = _story(call)
        assert (
            _position(story, trace.KIND_TURN, trace.INTENT_CUSTOMER_ID_INPUT)
            < _position(story, trace.KIND_AUTH, "PENDING")
        ), story
    finally:
        call.close()


@pytest.mark.trace
def test_the_id_turn_survives_a_worker_write_that_lands_after_the_tool(traced):
    """Ruling late is one problem; writing late must not add another."""
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.transcript_arrives(SPOKEN_ID, defer=True)
        # The tool runs, and the auth state moves on, before the row is written.
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.worker_catches_up()

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["intent"] == trace.INTENT_CUSTOMER_ID_INPUT
        assert turn["utterance"] == "[Customer ID provided]"
    finally:
        call.close()


@pytest.mark.trace
def test_the_id_turn_cannot_become_a_pin_turn_once_the_caller_is_verified(traced):
    """The furthest the state can travel before the write lands."""
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.transcript_arrives(SPOKEN_ID, defer=True)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.says(SPOKEN_PIN)
        call.tool("submit_pin", spoken_pin=HEARD_PIN)
        assert call.session.authenticated is True
        call.worker_catches_up()

        intents = [e["intent"] for e in call.events(trace.KIND_TURN)]
        assert trace.INTENT_CUSTOMER_ID_INPUT in intents, intents
        ids = [
            e for e in call.events(trace.KIND_TURN)
            if e["intent"] == trace.INTENT_CUSTOMER_ID_INPUT
        ]
        assert ids[-1]["utterance"] == "[Customer ID provided]"
    finally:
        call.close()


@pytest.mark.trace
def test_the_pin_turn_is_still_a_pin_turn_when_the_tool_wins_the_race(traced):
    """The other half: fixing the id turn must not unfix the PIN turn."""
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.says(SPOKEN_ID)
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)

        call.starts_speaking()
        call.tool("submit_pin", spoken_pin=HEARD_PIN)
        call.transcript_arrives(SPOKEN_PIN)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["intent"] == trace.INTENT_PIN_INPUT
        assert turn["utterance"] == "[PIN REDACTED]"
    finally:
        call.close()


@pytest.mark.trace
def test_both_credential_turns_stay_redacted_when_the_tools_win(traced, caplog):
    call = RacingCall()
    try:
        with caplog.at_level(logging.DEBUG):
            call.says(ASK_SAVINGS)
            call.starts_speaking()
            call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
            call.transcript_arrives(SPOKEN_ID)
            call.starts_speaking()
            call.tool("submit_pin", spoken_pin=HEARD_PIN)
            call.transcript_arrives(SPOKEN_PIN)

        blob = stored_blob()
        replay = json.dumps(call.replay(), ensure_ascii=False)
        logged = " ".join(record.getMessage() for record in caplog.records)
        for secret in (SPOKEN_PIN, HEARD_PIN, SPOKEN_ID):
            assert secret not in blob, secret
            assert secret not in replay, secret
            assert secret not in logged, secret
    finally:
        call.close()


@pytest.mark.trace
def test_replay_order_stays_strictly_increasing_when_the_tool_wins(traced):
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.transcript_arrives(SPOKEN_ID)
        call.starts_speaking()
        call.tool("submit_pin", spoken_pin=HEARD_PIN)
        call.transcript_arrives(SPOKEN_PIN)
        call.tool("get_account_balance", account_type="Savings")
        call.ends()

        sequences = [e["sequence"] for e in call.replay()["events"]]
        assert sequences == sorted(sequences), sequences
        assert len(sequences) == len(set(sequences)), "two events share a position"
    finally:
        call.close()


@pytest.mark.trace
def test_no_tool_is_delayed_to_achieve_the_ordering(traced):
    """Ordering comes from reservation, never from making the bank wait."""
    call = RacingCall()
    try:
        call.says(ASK_SAVINGS)
        call.starts_speaking()

        started = time.monotonic()
        result = call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        elapsed = time.monotonic() - started

        assert result["success"] is True
        assert elapsed < 0.25, f"the tool waited {elapsed:.3f}s for a trace row"
        call.transcript_arrives(SPOKEN_ID)
    finally:
        call.close()


# === Phase 6.14: terminal is not the same as final =========================
#
# The same live call closed like this:
#
#     CUSTOMER  "Okay, thank you. Goodbye."
#     AGENT     "Thank you for calling ABC Demo"      <- all that was stored
#     lifecycle CALLER_GOODBYE
#
# The bank said the whole sentence. Only that much was persisted.
#
# `agents/realtime/session.py` merges a fuller accumulated transcript into an
# updated item **only when the incoming transcript is falsy**:
#
#     entry_transcript = entry.transcript
#     if not entry_transcript:
#         preserved = existing.transcript or self._item_transcripts.get(item_id)
#
# A `response.output_item.done` carrying a *partial but non-empty* transcript
# therefore skips that merge, and `openai_realtime.py` stamps the same item
# `status="completed"`. Phase 6.13 then did exactly what it was built to do:
# treat completion as terminal, write the row, and ignore everything that
# arrived afterwards - including the rest of the sentence.
#
# Phase 6.13 was right about the *item* and wrong to infer the *text* from it.
# The provider publishes the text separately and says so:
# `response.output_audio_transcript.done` carries "the final transcript of the
# audio", and is emitted for interrupted and cancelled responses too. The SDK
# maps only `.delta`, so it is read raw - the same way Phase 6.11.1 had to read
# `input_audio_transcription.failed`.

GOODBYE = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
GOODBYE_PARTIAL = "Thank you for calling ABC Demo"


def final_transcript(bridge, item_id: str, transcript: str) -> None:
    """The provider's authoritative final text for one assistant item."""
    bridge._on_raw(
        _Event(
            "raw_server_event",
            data={
                "type": "response.output_audio_transcript.done",
                "item_id": item_id,
                "transcript": transcript,
                "response_id": "resp-1",
                "content_index": 0,
                "output_index": 0,
            },
        )
    )


@pytest.mark.trace
def test_the_live_goodbye_is_stored_complete(bridge_call):
    """The live sequence, end to end."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "Thank you for", "g1", "in_progress")
        # Terminal item, partial text - the shape that truncated the live row.
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", "completed")
        final_transcript(bridge, "g1", GOODBYE)
        await bridge._generation_finished()
        await _settle(bridge)
        await bridge.close()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [GOODBYE], f"the closing sentence was cut: {written}"


@pytest.mark.trace
def test_a_terminal_status_does_not_freeze_partial_text(bridge_call):
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", "completed")
        await _settle(bridge)
        final_transcript(bridge, "g1", GOODBYE)
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [GOODBYE], written


@pytest.mark.trace
def test_the_authoritative_text_is_what_gets_persisted(bridge_call):
    """One item, one row, and the row holds the provider's final transcript."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        for piece in ("Let me", "Let me check that", "Let me check that for"):
            bridge._on_history_item("assistant", piece, "a1", "in_progress")
        bridge._on_history_item("assistant", "Let me check that for", "a1", "completed")
        final_transcript(bridge, "a1", "Let me check that for your savings account.")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = agent_events(call_id)
    assert len(written) == 1, [e["utterance"] for e in written]
    assert written[0]["utterance"] == "Let me check that for your savings account."


@pytest.mark.trace
def test_a_stale_snapshot_cannot_overwrite_the_authoritative_text(bridge_call):
    """Phase 6.13's guarantee, kept: once written, an item is done with."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", "completed")
        final_transcript(bridge, "g1", GOODBYE)
        await _settle(bridge)
        # Late, shorter, and for an item that has had its final say.
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", None)
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [GOODBYE], written


@pytest.mark.trace
def test_teardown_preserves_the_full_final_sentence(bridge_call):
    """The goodbye disconnect must not cut the goodbye."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", "completed")
        final_transcript(bridge, "g1", GOODBYE)
        await bridge.close()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [GOODBYE], written


@pytest.mark.trace
def test_no_stale_greeting_is_replayed_after_the_final_text(bridge_call):
    bridge, call_id, _m, _b = bridge_call
    greeting = "Welcome to ABC Demo Bank. How may I assist you today?"

    async def scenario():
        bridge._on_history_item("assistant", greeting, "hello", "completed")
        final_transcript(bridge, "hello", greeting)
        await _settle(bridge)
        bridge._on_history_item("assistant", GOODBYE_PARTIAL, "g1", "completed")
        final_transcript(bridge, "g1", GOODBYE)
        await _settle(bridge)
        bridge._on_history_item("assistant", greeting, "hello", None)
        await bridge._generation_finished()
        await bridge.close()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == [greeting, GOODBYE], written


@pytest.mark.trace
def test_an_interrupted_response_keeps_its_existing_semantics(bridge_call):
    """Barge-in: one row, the text the provider settled on."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "Your balance is", "i1", "in_progress")
        bridge._on_history_item("assistant", "Your balance is", "i1", "incomplete")
        # The provider emits the final transcript for cancelled responses too.
        final_transcript(bridge, "i1", "Your balance is")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["Your balance is"], written


@pytest.mark.trace
def test_the_fallback_still_writes_when_no_final_transcript_arrives(bridge_call):
    """A transport that never sends the authoritative event must not lose the turn."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "A whole sentence.", "i1", None)
        await bridge._generation_finished()
        await _settle(bridge)
        await bridge.close()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["A whole sentence."], written


@pytest.mark.trace
def test_a_completed_item_with_no_final_transcript_is_still_written_once(bridge_call):
    """Completion remains a valid last resort when no final text ever comes."""
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        bridge._on_history_item("assistant", "All done.", "i1", "completed")
        await bridge._generation_finished()
        await _settle(bridge)
        await bridge.close()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["All done."], written


@pytest.mark.trace
def test_a_final_transcript_with_no_prior_snapshot_is_still_written(bridge_call):
    """The authoritative text stands on its own.

    Written rather than ignored: this event *is* the bank's words, and a turn
    whose snapshots never arrived is still a turn that was spoken. Recording it
    once is the safe direction to be wrong in; dropping it is not.
    """
    bridge, call_id, _m, _b = bridge_call

    async def scenario():
        final_transcript(bridge, "unannounced", "Words with no snapshot.")
        await bridge._generation_finished()
        await _settle(bridge)

    asyncio.run(scenario())

    written = [e["utterance"] for e in agent_events(call_id)]
    assert written == ["Words with no snapshot."], written


# --- Phase 6.14: the race leaks the PIN, not just its label ----------------
#
# Found while writing the ordering tests above, and worse than the defect they
# were written for.
#
# On the PIN turn the expectation is asked for *after* `submit_pin` has already
# succeeded, so `session.authenticated` is True and `expected_credential`
# answers None - no credential is expected, because it has just been accepted.
# A Latin PIN survives that because `looks_like_pin` still recognises the
# words. A PIN transcribed into another script does not, and D-8 is open again:
#
#     call_trace_events.utterance = "فور ایٹ ٹو ون"
#
# It held on live call `9cf4e328-...` only because the transcript won that
# particular race. Nothing guaranteed it would.

URDU_PIN = "فور ایٹ ٹو ون"


@pytest.mark.trace
def test_a_non_latin_pin_is_redacted_even_when_the_tool_wins_the_race(traced, caplog):
    """The state-driven rule must not depend on who wins."""
    call = RacingCall()
    try:
        with caplog.at_level(logging.DEBUG):
            call.says(ASK_SAVINGS)
            call.says(SPOKEN_ID)
            call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)

            call.starts_speaking()
            call.tool("submit_pin", spoken_pin=HEARD_PIN)
            call.transcript_arrives(URDU_PIN)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["utterance"] == "[PIN REDACTED]", (
            f"the caller's PIN was stored as {turn['utterance']!r}"
        )
        assert turn["intent"] == trace.INTENT_PIN_INPUT
        assert turn["event_type"] == trace.EVENT_AUTH_INPUT

        assert URDU_PIN not in stored_blob(), "the PIN reached the trace table"
        assert URDU_PIN not in json.dumps(call.replay(), ensure_ascii=False), (
            "the PIN reached the replay API"
        )
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert URDU_PIN not in logged, "the PIN reached the logs"
    finally:
        call.close()


@pytest.mark.trace
def test_a_non_latin_customer_id_is_masked_even_when_the_tool_wins(traced):
    """The same anchor has to carry the id turn too."""
    call = RacingCall()
    try:
        spoken = "ڈیمو زیرو زیرو ون"
        call.says(ASK_SAVINGS)
        call.starts_speaking()
        call.tool("submit_customer_id", spoken_customer_id=HEARD_ID)
        call.transcript_arrives(spoken)

        turn = call.events(trace.KIND_TURN)[-1]
        assert turn["utterance"] == "[Customer ID provided]"
        assert turn["intent"] == trace.INTENT_CUSTOMER_ID_INPUT
        assert spoken not in stored_blob()
    finally:
        call.close()


@pytest.mark.trace
def test_the_stored_blob_helper_can_actually_see_non_latin_text(traced):
    """The guard that guards the guard.

    If this fails, every `not in stored_blob()` assertion above is vacuous for
    any script but Latin.
    """
    call = Call()
    try:
        call.says(ASK_SAVINGS)
        blob = stored_blob()
        assert ASK_SAVINGS in blob
        assert "\\u" not in blob, "non-ASCII is being escaped; the search is blind"
    finally:
        call.close()
