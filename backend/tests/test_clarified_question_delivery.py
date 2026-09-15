"""Phase 7.4C: the bank must ask the question it has decided to ask.

Fail-before-fix evidence for a live acceptance failure at commit 333e7fb.

Three capacity-1 calls, same build, same deployment:

    A  fdc56fe0-29d3-1240-4790-eaa5afddeeef   succeeded
    B  3c1932d1-29d4-1240-4790-eaa5afddeeef   CALLER_SILENT
    C  6a6efe0b-29d4-1240-4790-eaa5afddeeef   CALLER_SILENT

All three asked for a balance generically before identifying themselves, all
three verified correctly, and in all three the deterministic resume ran
`get_account_balance` and got `ACCOUNT_TYPE_REQUIRED` - the bank deciding it
needs to know which account. On A the agent then said "Which account balance
would you like, Savings or Current?" and the call finished with the Savings
balance. On B and C the agent said nothing at all, the silence timer armed, and
the caller was thanked and hung up on while the bank was holding a question it
had already decided to ask. B failed its first PIN and C did not, so
authentication is not the variable.

**This is Phase 7.3's lesson, one step later.** That phase established that once
an enquiry has been classified, authorised and stored, *answering* it must not
depend on the model choosing to act - and `_resume_held_enquiry` says so at
length. Phase 7.4B then made the answer deterministic on both channels. Neither
made *asking* deterministic. The clarification is created correctly and travels
back inside `submit_pin`'s own tool result, where turning it into speech is the
model's decision and nothing notices when it declines.

The backend's own delivery seam, `RealtimeManager._answer_completed_clarification`,
is explicitly gated on `outstanding.complete` - the caller having already
answered. There is no seam at all for the other half: the question. Measured on
this commit, with a model that never speaks and never calls a tool, the backend
sends nothing and the clarification stays owed for the rest of the call.

Every test here drives the real production pump over a scripted session, the
same way `test_clarified_phone_delivery` does. The scripted model has no tool
machinery, so anything spoken is the backend's doing - which is the only way to
tell a deterministic bank from a lucky one.
"""

import asyncio

import pytest

from app import pending_clarification, pending_request
from app.agents import speech
from app.agents.intents import Domain
from app.realtime.realtime_manager import RealtimeConnection, RealtimeManager
from app.realtime.webrtc import execute_tool
from app.sessions import SessionManager, session_manager

PINS = {"DEMO001": "4821", "DEMO003": "2648"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def executions(monkeypatch):
    """What reached the banking tools, and what came back."""
    import app.realtime.tools as tools_module

    original = tools_module._run_tool
    seen: list[tuple[str, str, bool]] = []

    def counting(tool_name, session_id, arguments, mgr):
        result = original(tool_name, session_id, arguments, mgr)
        seen.append(
            (session_id, tool_name, isinstance(result, dict) and bool(result.get("success")))
        )
        return result

    monkeypatch.setattr(tools_module, "_run_tool", counting)

    class Counter:
        def of(self, session_id, tool_name):
            return sum(1 for s, n, _ in seen if s == session_id and n == tool_name)

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


_ITEMS = iter(range(1, 10_000))


def transcription_event(text):
    return Event(
        "raw_model_event",
        data=Raw(
            "input_audio_transcription_completed",
            transcript=text,
            item_id=f"item-{next(_ITEMS)}",
        ),
    )


class SilentModel:
    """A model that produces events but never asks and never calls a tool.

    Faithful to the live failure in the one way that matters. On calls B and C
    the model *was* generating - it had just called `submit_pin` and spoke about
    the result - so events certainly reached the pump. What it never did was ask
    the clarifying question. So this yields a generation-finished event with no
    caller turn behind it, which is exactly the state those calls were in when
    the bank fell silent.

    An empty event stream would be a different and much weaker test: the pump
    body would never run at all, and nothing would be proved about what the
    backend does when it has the chance to act.
    """

    def __init__(self, transcripts=()):
        self._transcripts = list(transcripts)
        self.sent: list[str] = []

    async def __aiter__(self):
        if self._transcripts:
            for text in self._transcripts:
                yield transcription_event(text)
            return
        # The model finished a turn of its own - the verification line - and
        # asked the caller nothing.
        yield Event("audio_end")

    async def send_message(self, text):
        self.sent.append(text)

    async def send_audio(self, audio):
        return None

    async def close(self):
        return None


def pump(manager, session_id, transcripts=()):
    """Drive the real production pump. Returns what the backend sent."""
    realtime = RealtimeManager(manager=manager)
    session = SilentModel(transcripts)
    connection = RealtimeConnection(
        banking_session_id=session_id,
        realtime_session_id="REALTIME-74c",
        session=session,
    )
    run(realtime._pump_events(connection, None))
    return session


def delivered(model):
    return "\n".join(model.sent)


def tool(manager, session_id, name, arguments=None):
    return run(execute_tool(name, session_id, arguments or {}, manager=manager))


def held_enquiry_then_verify(manager, customer_id, question):
    """Exactly the live shape: ask generically, then identify, then PIN."""
    session_id = manager.create_session().session_id

    pump(manager, session_id, [question])
    held = pending_request.recall(manager.get_session(session_id))
    assert held is not None, "the gate did not hold the enquiry"

    tool(manager, session_id, "submit_customer_id",
         {"spoken_customer_id": customer_id})
    verification = tool(manager, session_id, "submit_pin",
                        {"spoken_pin": PINS[customer_id]})
    assert verification["success"] is True
    return session_id, verification


# === Calls B and C ==========================================================


def test_the_bank_asks_which_account_without_another_caller_turn(
    manager, executions
):
    """Calls B and C, offline and deterministic.

    The caller has said everything a caller can be expected to say: what they
    want, who they are, and their PIN. The bank has decided it needs to know
    which account. Whether the caller is ever asked must not depend on the
    model.
    """
    session_id, verification = held_enquiry_then_verify(
        manager, "DEMO001", "I want to know my account balance."
    )

    # The resume ran, exactly once, and produced the bank's question.
    assert executions.of(session_id, "get_account_balance") == 1
    resumed = verification["pending_result"]
    assert resumed["reason"] == "ACCOUNT_TYPE_REQUIRED"
    assert resumed["available_account_types"] == ["Savings", "Current"]

    # The question is authoritative server-side state.
    outstanding = pending_clarification.recall(manager.get_session(session_id))
    assert outstanding is not None, "the bank's question was not recorded"
    assert outstanding.tool == "get_account_balance"
    assert outstanding.domain is Domain.ACCOUNT
    assert outstanding.complete is False
    assert outstanding.intent.name == "ACCOUNT_BALANCE"

    # And now the whole of it: no further caller turn, because on B and C there
    # was none - the caller was waiting to be asked.
    spoken = pump(manager, session_id)

    assert spoken.sent, (
        "the bank decided it needed to know which account and said nothing. "
        "The caller waited, the silence timer armed, and they were hung up on "
        "with the question still owed - live calls B and C."
    )
    payload = delivered(spoken)
    assert "Savings" in payload and "Current" in payload, (
        f"the caller was not offered the choices: {payload!r}"
    )


def test_the_question_is_asked_before_silence_can_close_the_call(manager):
    """The failure mode as the caller experienced it.

    On B and C the only thing that reached the model after verification was the
    silence cue, and the next thing the caller heard was "I do not hear anything
    from you. Thank you." A bank that is holding a question of its own must not
    be the one that runs out of patience.
    """
    session_id, _ = held_enquiry_then_verify(
        manager, "DEMO001", "What is my account balance?"
    )

    spoken = pump(manager, session_id)
    payload = delivered(spoken)

    assert payload, "nothing was said while a question was owed"
    assert speech.SILENCE_CLOSING_CUE not in payload, (
        "the bank moved to close the call while it still owed the caller a "
        "clarifying question"
    )
    assert pending_clarification.recall(
        manager.get_session(session_id)
    ) is not None, "the question was dropped rather than asked"


def test_the_caller_can_answer_the_question_the_bank_asked(manager, executions):
    """The whole exchange, end to end, with a model that does nothing.

    Call A completed only because the model happened to ask. This asserts the
    same outcome without relying on it: the bank asks, the caller answers, the
    bank reads the account once and says the figure.
    """
    session_id, _ = held_enquiry_then_verify(
        manager, "DEMO001", "I want to know my account balance."
    )

    asked = pump(manager, session_id)
    assert "Savings" in delivered(asked), "the bank never asked"

    answered = pump(manager, session_id, ["Savings"])

    assert "12450.75" in delivered(answered), (
        "the caller answered the bank's question and was not told the balance"
    )
    assert executions.of(session_id, "get_account_balance") == 2, (
        "expected one read for the question and one for the answer"
    )
    assert pending_clarification.recall(manager.get_session(session_id)) is None


def test_a_held_loan_enquiry_needing_a_choice_is_also_asked(manager):
    """The same transition, shared machinery, other domain.

    DEMO003 has two loans, so a generic loan enquiry held across authentication
    resolves to `LOAN_TYPE_REQUIRED` exactly as the balance one does.
    """
    session_id, verification = held_enquiry_then_verify(
        manager, "DEMO003", "Tell me about my loan."
    )

    resumed = verification["pending_result"]
    assert resumed["reason"] == "LOAN_TYPE_REQUIRED"

    outstanding = pending_clarification.recall(manager.get_session(session_id))
    assert outstanding is not None and outstanding.domain is Domain.LOAN

    spoken = pump(manager, session_id)
    payload = delivered(spoken)

    assert payload, "the bank owed a loan question and said nothing"
    assert "Car Loan" in payload and "Home Loan" in payload, (
        f"the caller was not offered the loans: {payload!r}"
    )


def test_no_model_tool_call_is_needed_to_ask_or_to_answer(manager, executions):
    """`SilentModel` has no tool machinery at all.

    So both halves of this exchange - the question and the answer - are the
    backend's, and the count proves the bank was read for the answer exactly
    once rather than once per attempt.
    """
    session_id, _ = held_enquiry_then_verify(
        manager, "DEMO001", "What is my account balance?"
    )

    pump(manager, session_id)
    pump(manager, session_id, ["Current"])

    assert executions.of(session_id, "get_account_balance") == 2
    assert pending_clarification.recall(manager.get_session(session_id)) is None
