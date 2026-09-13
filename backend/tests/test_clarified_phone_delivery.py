"""Phase 7.4B Stage 2.1: the telephone caller hears the answer.

Stage 2 made the *value* deterministic on both channels - the account a caller
names is kept server-side and can no longer be lost - and made the *execution*
deterministic on Channel 1 only. `pending_clarification.resolve()` was reached
from `routers/call.py` and from nowhere else, so on the telephone the bank still
waited for the model to reissue the banking tool call before anybody was
answered.

That is the channel that goes to production, and the failure it leaves open is
the one Phase 7.3 already paid for once: a verified caller, an enquiry the
backend has classified, authorised and completed, and silence on the line
because the model did not make a call it was never obliged to make.

These tests drive the **real production pump** - `RealtimeManager._pump_events`
- with a scripted model session, exactly as `test_telephony_business_persistence`
drives it. Nothing here re-implements the orchestration; the fake supplies only
what a provider socket would: caller transcripts in, messages out.

Two things are asserted of every clarified enquiry, and the second is the one
Stage 2 could not make:

1. the banking operation ran, exactly once, with the caller's own selection;
2. the result reached the response path, carrying the actual figure, so that a
   response is generated.

A successful banking operation followed by nothing being sent to the model is a
silent call, and fails.

**On what "caller-visible" can mean offline.** These tests use a scripted
session, so no model generates speech and none is asserted. What is asserted is
the production mechanism that causes speech: the banking result is delivered
into the model session's input - the same path `GREETING_CUE` and
`SILENCE_CLOSING_CUE` use to make the agent speak, and the same path a tool
result takes. The wording stays the model's. Proving the sentence itself needs a
live model and belongs to the live acceptance script, not to an offline suite.
"""

import asyncio
import json

import pytest

from app import pending_clarification
from app.realtime.realtime_manager import RealtimeConnection, RealtimeManager
from app.realtime.webrtc import execute_tool
from app.sessions import SessionManager, session_manager

PINS = {"DEMO001": "4821", "DEMO003": "2648"}

DEMO001_SAVINGS = "12450.75"
DEMO003_CAR_LOAN = "46200.00"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_shared_state():
    yield
    session_manager.clear()


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def executions(monkeypatch):
    """What actually reached the banking tools, and what came back."""
    import app.realtime.tools as tools_module

    original = tools_module._run_tool
    seen: list[tuple[str, str, bool]] = []

    def counting(tool_name, session_id, arguments, mgr):
        result = original(tool_name, session_id, arguments, mgr)
        seen.append(
            (
                session_id,
                tool_name,
                isinstance(result, dict) and bool(result.get("success")),
            )
        )
        return result

    monkeypatch.setattr(tools_module, "_run_tool", counting)

    class Counter:
        def of(self, session_id, tool_name):
            return sum(1 for s, n, _ in seen if s == session_id and n == tool_name)

        def answered(self, session_id, tool_name):
            return sum(
                1 for s, n, ok in seen if s == session_id and n == tool_name and ok
            )

    return Counter()


# --- the provider side, scripted --------------------------------------------


class Raw:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


def transcription_event(text, item_id="item-1"):
    """One caller turn, in the shape the SDK delivers it."""
    return Event(
        "raw_model_event",
        data=Raw(
            "input_audio_transcription_completed", transcript=text, item_id=item_id
        ),
    )


# Item ids must be unique across the whole call, as a provider's are. Reusing
# them between two `pump()` calls made the second turn look like a replay of
# the first, which is a real deduplication path and not one these tests mean to
# exercise here - `test_a_repeated_final_transcript_reads_the_bank_once` covers
# it deliberately, with the same id, which is the correct way to ask for it.
_ITEM_IDS = iter(range(1, 10_000))


class ScriptedSession:
    """A model session that says what it is told and remembers what it is sent.

    Deliberately has no tool-calling behaviour at all. That is the whole point:
    if anything is spoken on these calls, the backend put it there.
    """

    def __init__(self, transcripts):
        self._transcripts = list(transcripts)
        self.sent: list[str] = []
        self.closed = False

    async def __aiter__(self):
        for text in self._transcripts:
            yield transcription_event(text, item_id=f"item-{next(_ITEM_IDS)}")

    async def send_message(self, text):
        self.sent.append(text)

    async def send_audio(self, audio):
        return None

    async def close(self):
        self.closed = True


def pump(manager, session_id, transcripts):
    """Drive the real production pump over a scripted set of caller turns."""
    realtime = RealtimeManager(manager=manager)
    session = ScriptedSession(transcripts)
    connection = RealtimeConnection(
        banking_session_id=session_id,
        realtime_session_id="REALTIME-stage21",
        session=session,
    )
    run(realtime._pump_events(connection, None))
    return session


def delivered(scripted):
    """Everything the backend pushed into the model session, as one string."""
    return "\n".join(scripted.sent)


# --- the conversation, as production drives it ------------------------------


def tool(manager, session_id, name, arguments=None):
    return run(execute_tool(name, session_id, arguments or {}, manager=manager))


def verified(manager, customer_id):
    session = manager.create_session()
    tool(manager, session.session_id, "submit_customer_id",
         {"spoken_customer_id": customer_id})
    result = tool(manager, session.session_id, "submit_pin",
                  {"spoken_pin": PINS[customer_id]})
    assert result["success"] is True, "the harness failed to authenticate"
    return session.session_id


def ask_ambiguously(manager, session_id, tool_name):
    """Put the bank's clarifying question, through the real tool path."""
    result = tool(manager, session_id, tool_name, {})
    assert result["success"] is False
    assert pending_clarification.recall(manager.get_session(session_id)) is not None
    return result


# === A. the authenticated telephone flow ====================================


def test_a_clarified_balance_is_executed_and_delivered_on_the_phone(
    manager, executions
):
    """A. The Stage 2 gap, stated as the caller experiences it.

    The caller has been asked which account and says "Savings". The model in
    this test cannot call tools at all, so everything that happens next is the
    backend's doing - which is exactly the guarantee that was missing.
    """
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    before = executions.answered(session_id, "get_account_balance")

    spoken = pump(manager, session_id, ["Savings"])

    assert executions.answered(session_id, "get_account_balance") - before == 1, (
        "the backend did not run the enquiry the caller had completed"
    )
    assert spoken.sent, (
        "the enquiry was executed and nothing was sent to the caller - a "
        "successful banking operation ended in silence"
    )
    assert DEMO001_SAVINGS in delivered(spoken), (
        "what reached the response path did not carry the balance that was read"
    )
    assert pending_clarification.recall(manager.get_session(session_id)) is None


def test_the_phone_path_needs_no_model_tool_call(manager, executions):
    """3. The model never invokes the banking tool, and the caller is answered.

    `ScriptedSession` has no tool machinery, and `_pump_events` is given no
    handler. The only route from "Savings" to a balance is the backend's own.
    """
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")

    spoken = pump(manager, session_id, ["Savings"])

    assert DEMO001_SAVINGS in delivered(spoken)
    # Exactly one read in total for this session: the clarifying question
    # looked at the account list, and the answer read the balance.
    assert executions.answered(session_id, "get_account_balance") == 1


def test_a_clarified_loan_is_executed_and_delivered_on_the_phone(manager):
    """D. The same, in the other domain, on the customer with two loans."""
    session_id = verified(manager, "DEMO003")
    ask_ambiguously(manager, session_id, "get_loan_details")

    spoken = pump(manager, session_id, ["Car Loan"])

    assert DEMO003_CAR_LOAN in delivered(spoken), (
        "the clarified loan enquiry did not reach the caller"
    )


# === B / C. the authentication orderings ====================================


def test_a_slot_given_before_verification_is_delivered_after_it(manager, executions):
    """B. Slot before auth. The Phase 7.3 resume still owns this one.

    Nothing protected may be read while the caller is unverified, so the pump
    must deliver nothing on those turns - and the enquiry must still be waiting
    afterwards, complete with the account they named.
    """
    session_id = manager.create_session().session_id

    spoken = pump(manager, session_id, ["What is my account balance?", "Savings"])

    assert executions.answered(session_id, "get_account_balance") == 0, (
        "an unverified caller's balance was read"
    )
    assert DEMO001_SAVINGS not in delivered(spoken)

    tool(manager, session_id, "submit_customer_id",
         {"spoken_customer_id": "DEMO001"})
    verification = tool(manager, session_id, "submit_pin", {"spoken_pin": "4821"})

    assert verification["pending_result"]["available_balance"] == DEMO001_SAVINGS
    assert executions.answered(session_id, "get_account_balance") == 1


def test_a_slot_given_after_verification_is_executed_on_the_phone(manager):
    """C. Auth before slot. The generic ask, then identity, then the choice."""
    session_id = manager.create_session().session_id

    pump(manager, session_id, ["What is my account balance?"])
    tool(manager, session_id, "submit_customer_id",
         {"spoken_customer_id": "DEMO001"})
    verification = tool(manager, session_id, "submit_pin", {"spoken_pin": "4821"})

    # Correctly asked which account: the caller genuinely had not said.
    assert verification["pending_result"]["reason"] == "ACCOUNT_TYPE_REQUIRED"

    spoken = pump(manager, session_id, ["Savings"])

    assert DEMO001_SAVINGS in delivered(spoken), (
        "the caller answered after verifying and was not told the balance"
    )


# === 5. duplicate and stale realtime events =================================


def test_a_repeated_final_transcript_reads_the_bank_once(manager, executions):
    """Duplicate final transcript. Both failed live calls in 7.3 had these."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    before = executions.answered(session_id, "get_account_balance")

    spoken = pump(manager, session_id, ["Savings", "Savings"])

    assert executions.answered(session_id, "get_account_balance") - before == 1, (
        "a repeated transcript read the account twice"
    )
    assert DEMO001_SAVINGS in delivered(spoken)


def test_a_stale_slot_event_after_completion_executes_nothing(manager, executions):
    """A slot answer arriving after the enquiry is finished completes nothing."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    pump(manager, session_id, ["Savings"])
    after_first = executions.answered(session_id, "get_account_balance")

    # The same word again, with nothing outstanding.
    pump(manager, session_id, ["Savings"])

    assert executions.answered(session_id, "get_account_balance") == after_first, (
        "a stale slot answer re-ran a finished enquiry"
    )
    assert pending_clarification.recall(manager.get_session(session_id)) is None


def test_a_duplicate_model_tool_call_after_delivery_reads_the_bank_once(
    manager, executions
):
    """The other order: the backend answers, then the model asks anyway.

    Both routes must converge on one business execution. This is the cache
    doing its job, and it is the reason the backend answering early cannot
    double-charge the bank.
    """
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    before = executions.answered(session_id, "get_account_balance")

    pump(manager, session_id, ["Savings"])
    # The model, belatedly, makes the call it was never needed for.
    late = tool(manager, session_id, "get_account_balance", {"account_type": "Savings"})

    assert late["success"] is True
    assert late["available_balance"] == DEMO001_SAVINGS
    assert executions.answered(session_id, "get_account_balance") - before == 1, (
        "the backend and the model each read the account"
    )


def test_a_genuinely_new_enquiry_later_still_executes(manager, executions):
    """Exactly-once must not become never-again."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    pump(manager, session_id, ["Savings"])
    after_first = executions.answered(session_id, "get_account_balance")

    # Later in the same call, a new question about the other account.
    fresh = tool(manager, session_id, "get_account_balance",
                 {"account_type": "Current"})

    assert fresh["success"] is True
    assert executions.answered(session_id, "get_account_balance") > after_first


# === 4. lifecycle ===========================================================


def test_an_identity_change_forfeits_the_outstanding_question(manager, executions):
    """4A. Customer A's unfinished enquiry must not become authority for B."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    assert pending_clarification.recall(manager.get_session(session_id)) is not None

    # The caller identifies as somebody else on the same line.
    tool(manager, session_id, "submit_customer_id",
         {"spoken_customer_id": "DEMO003"})

    assert pending_clarification.recall(manager.get_session(session_id)) is None, (
        "an enquiry opened for one customer survived the identity changing"
    )


def test_a_goodbye_forfeits_the_outstanding_question(manager, executions):
    """4C. Nothing completes after the caller has said goodbye."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")
    before = executions.answered(session_id, "get_account_balance")

    spoken = pump(manager, session_id, ["Goodbye", "Savings"])

    assert pending_clarification.recall(manager.get_session(session_id)) is None, (
        "the question outlived the call it belonged to"
    )
    assert executions.answered(session_id, "get_account_balance") == before, (
        "a banking operation ran after the caller had said goodbye"
    )
    assert DEMO001_SAVINGS not in delivered(spoken)


def test_a_released_session_leaves_no_clarification_behind(manager):
    """4B. Provider/call end, through the ordinary session release."""
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")

    manager.destroy_session(session_id)

    assert manager.get_session(session_id) is None
    assert pending_clarification.recall(manager.get_session(session_id)) is None

    # A new call on the same manager starts with nothing owed.
    fresh = verified(manager, "DEMO001")
    assert pending_clarification.recall(manager.get_session(fresh)) is None
    answer = tool(manager, fresh, "get_account_balance", {})
    assert answer["reason"] == "ACCOUNT_TYPE_REQUIRED"


def test_the_delivery_payload_carries_no_credential(manager):
    """Whatever is pushed into the session is still subject to the rules.

    The result of a balance enquiry is money, not a credential - but this is a
    new path into the model's input and it is worth pinning that it carries
    nothing else.
    """
    session_id = verified(manager, "DEMO001")
    ask_ambiguously(manager, session_id, "get_account_balance")

    spoken = pump(manager, session_id, ["Savings"])
    payload = delivered(spoken)

    assert "4821" not in payload, "the PIN reached the model input"
    assert "DEMO001" not in payload, "the customer id reached the model input"
    # It is, however, genuinely the banking result.
    assert json.dumps(DEMO001_SAVINGS).strip('"') in payload
