"""Phase 6: one turn, one answer — and the question the caller already asked.

Three things are specified here.

**One logical caller turn cannot produce two concurrent assistant responses.**
The model can be prompted more than once for a single turn — a retried cue, a
racing trigger, a duplicated SDK callback — and each extra prompt generates
audio at the same time as the first. Played out, that is two assistants talking
over each other down one telephone line.

**A call's state has names and types**, in one place, and reads the banking
session for anything about the customer rather than keeping a second copy that
could disagree with the guards.

**A caller who asked before they were known does not have to ask again.** The
enquiry is held across authentication and answered in the same breath as the
confirmation — tested here through the *telephone* path rather than the
browser's.

All customers and PINs are synthetic seed data.
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.sessions import session_manager
from app.telephony.conversation import ConversationState, Stage

PINS = {"DEMO001": "4821", "DEMO002": "7315"}


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with session_scope() as db:
            db.execute(delete(ConversationMessage))
            db.execute(delete(AgentToolEvent))
            db.execute(delete(AgentSession))

    wipe()
    yield
    session_manager.clear()
    wipe()


def state_for(session_id: str, call_id: str = "call-1") -> ConversationState:
    return ConversationState(
        provider_call_id=call_id,
        banking_session_id=session_id,
        session_manager=session_manager,
    )


# === 1. duplicate-response suppression ======================================


def build_bridge(call_id: str = "call-dup"):
    from app.telephony.bridge import PhoneCallBridge
    from app.telephony.media import LoopbackMediaTransport

    session = session_manager.create_session()
    return PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=LoopbackMediaTransport(),
        realtime_manager=None,
        outbound_max_frames=200,
    )


class Audio:
    """One audio event, as the SDK shapes it."""

    def __init__(self, response_id, data=b"\x00\x10" * 480):
        self.type = "audio"
        self.audio = type("A", (), {"data": data, "response_id": response_id})()


class Plain:
    def __init__(self, kind, **fields):
        self.type = kind
        for name, value in fields.items():
            setattr(self, name, value)


def test_one_response_plays_and_a_second_concurrent_one_is_dropped():
    """Two responses generating at once is two voices on one line."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    for _ in range(3):
        bridge.on_realtime_event(sid, Audio("resp-A"))
    for _ in range(3):
        bridge.on_realtime_event(sid, Audio("resp-B"))

    assert len(bridge.outbound) == 3, "audio from a second response was played"
    assert bridge.conversation.duplicate_responses_suppressed == 3
    assert bridge.conversation.active_response_id == "resp-A"


def test_a_duplicated_callback_for_the_same_response_is_not_dropped():
    """The same answer delivered twice is still one answer.

    Identity comes from the model's response id, so a duplicated SDK callback
    carrying the same id is admitted while a genuinely second response is not.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    event = Audio("resp-A")
    for _ in range(5):
        bridge.on_realtime_event(sid, event)

    assert len(bridge.outbound) == 5
    assert bridge.conversation.duplicate_responses_suppressed == 0


def test_the_next_response_is_allowed_once_the_first_has_ended():
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, Audio("resp-A"))
    bridge.on_realtime_event(sid, Plain("audio_end"))
    bridge.on_realtime_event(sid, Audio("resp-B"))

    assert len(bridge.outbound) == 2
    assert bridge.conversation.duplicate_responses_suppressed == 0
    assert bridge.conversation.active_response_id == "resp-B"


def test_barge_in_does_not_leave_a_response_active_for_ever():
    """Without releasing the id, every later answer would be suppressed."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, Audio("resp-A"))
    bridge.on_realtime_event(sid, Plain("audio_interrupted"))
    assert bridge.conversation.active_response_id is None

    bridge.on_realtime_event(sid, Audio("resp-B"))

    assert len(bridge.outbound) == 1, "the answer after barge-in was suppressed"
    assert bridge.conversation.duplicate_responses_suppressed == 0


def test_audio_with_no_response_id_is_never_dropped():
    """Dropping a stream that cannot be identified would silence real answers."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    for _ in range(4):
        bridge.on_realtime_event(sid, Audio(None))

    assert len(bridge.outbound) == 4
    assert bridge.conversation.duplicate_responses_suppressed == 0


def test_a_cue_is_refused_while_a_response_is_already_speaking():
    """Guards the places we create a response, so the overlap never happens."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    assert bridge.may_request_response() is True
    bridge.on_realtime_event(sid, Audio("resp-A"))
    assert bridge.may_request_response() is False
    bridge.on_realtime_event(sid, Plain("audio_end"))
    assert bridge.may_request_response() is True


def test_the_greeting_is_refused_while_something_is_already_speaking():
    async def scenario():
        bridge = build_bridge("call-greetrace")
        bridge.on_realtime_event(bridge.banking_session_id, Audio("resp-A"))
        return await bridge.greet()

    assert asyncio.run(scenario()) is False


# --- duplicate tools, scoped to a turn --------------------------------------


def tool_event(name="get_account_balance", arguments='{"account_type":"Savings"}'):
    return Plain("tool_start", tool=Plain("t", name=name), arguments=arguments)


def test_the_same_tool_twice_in_one_turn_is_suppressed():
    """A banking read is idempotent; a duplicate is a second disclosure."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    for _ in range(4):
        bridge.on_realtime_event(sid, tool_event())

    assert bridge.conversation.duplicate_tools_suppressed == 3


def test_the_same_question_on_a_later_turn_runs_again():
    """The requirement that stops suppression becoming a refusal.

    A caller may legitimately ask for the same balance twice in one call. The
    second ask is a new turn, so identical text must not be mistaken for a
    duplicate.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id
    turn = Plain("raw_model_event", data=Plain("turn_started"))

    bridge.on_realtime_event(sid, tool_event())
    bridge.on_realtime_event(sid, turn)
    bridge.on_realtime_event(sid, tool_event())
    bridge.on_realtime_event(sid, turn)
    bridge.on_realtime_event(sid, tool_event())

    assert bridge.conversation.duplicate_tools_suppressed == 0
    assert bridge.conversation.turn_counter == 2


def test_a_different_tool_in_the_same_turn_is_not_suppressed():
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, tool_event("get_account_balance"))
    bridge.on_realtime_event(sid, tool_event("get_loan_balance", "{}"))

    assert bridge.conversation.duplicate_tools_suppressed == 0


def test_concurrent_response_attempts_settle_on_one():
    """Many callbacks arriving together must still yield a single answer."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    async def scenario():
        async def deliver(response_id):
            bridge.on_realtime_event(sid, Audio(response_id))

        await asyncio.gather(*(deliver(f"resp-{n}") for n in range(8)))

    asyncio.run(scenario())

    admitted = bridge.conversation.active_response_id
    assert admitted is not None
    assert len(bridge.outbound) == 1, "more than one response was played"
    assert bridge.conversation.duplicate_responses_suppressed == 7


def test_two_calls_suppress_independently():
    first = build_bridge("call-i1")
    second = build_bridge("call-i2")

    first.on_realtime_event(first.banking_session_id, Audio("resp-A"))
    first.on_realtime_event(first.banking_session_id, Audio("resp-B"))
    second.on_realtime_event(second.banking_session_id, Audio("resp-C"))

    assert first.conversation.duplicate_responses_suppressed == 1
    assert second.conversation.duplicate_responses_suppressed == 0
    assert second.conversation.active_response_id == "resp-C"


# === 2. structured per-call state ===========================================


def test_the_state_reports_every_required_field():
    session = session_manager.create_session()
    described = state_for(session.session_id).describe()

    for field in (
        "provider_call_id", "banking_session_id", "authenticated",
        "customer_id_received", "customer_reference", "stage",
        "current_domain", "current_intent", "pending_request",
        "selected_account_type", "selected_loan_type",
        "last_completed_turn_id", "active_response_id", "caller_speaking",
        "assistant_speaking", "closing", "silence_timer_armed", "turn_counter",
    ):
        assert field in described, field
    assert described["has_last_user_turn"] is False
    assert described["has_last_agent_response"] is False
    assert described["has_last_agent_question"] is False


def test_the_state_never_holds_or_reports_a_pin():
    """Not the digits, not a hash, not a masked form."""
    from app.auth.authentication import submit_customer_id, submit_pin

    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO001")
    submit_pin(session.session_id, PINS["DEMO001"])

    state = state_for(session.session_id)
    rendered = str(state.describe()) + str(vars(state))

    assert PINS["DEMO001"] not in rendered
    assert "pin" not in {key.lower() for key in state.describe()}


def test_a_claimed_identity_is_not_a_customer_reference():
    """Returning a claim here would let a stranger's assertion become identity."""
    from app.auth.authentication import submit_customer_id

    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO001")
    state = state_for(session.session_id)

    assert state.customer_id_received is True
    assert state.authenticated is False
    assert state.customer_reference is None


def test_the_customer_reference_appears_only_after_verification():
    from app.auth.authentication import submit_customer_id, submit_pin

    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO001")
    submit_pin(session.session_id, PINS["DEMO001"])
    state = state_for(session.session_id)

    assert state.authenticated is True
    assert state.customer_reference == "DEMO001"


def test_the_state_reads_the_session_rather_than_copying_it():
    """One answer to who is calling — the one the guards use."""
    from app.auth.authentication import submit_customer_id, submit_pin

    session = session_manager.create_session()
    state = state_for(session.session_id)
    assert state.authenticated is False

    submit_customer_id(session.session_id, "DEMO001")
    submit_pin(session.session_id, PINS["DEMO001"])

    # No refresh call: the property reads through.
    assert state.authenticated is True
    assert state.customer_reference == "DEMO001"


def test_the_stage_follows_the_session():
    from app.auth.authentication import submit_customer_id, submit_pin

    session = session_manager.create_session()
    state = state_for(session.session_id)

    state.advance_stage()
    assert state.stage is Stage.OPENING

    state.begin_caller_turn()
    state.advance_stage()
    assert state.stage is Stage.IDENTIFYING

    submit_customer_id(session.session_id, "DEMO001")
    state.advance_stage()
    assert state.stage is Stage.VERIFYING

    submit_pin(session.session_id, PINS["DEMO001"])
    state.advance_stage()
    assert state.stage is Stage.SERVING

    state.closing = True
    state.advance_stage()
    assert state.stage is Stage.CLOSING


def test_two_calls_hold_separate_state():
    first_session = session_manager.create_session()
    second_session = session_manager.create_session()
    first = state_for(first_session.session_id, "call-a")
    second = state_for(second_session.session_id, "call-b")

    first.begin_caller_turn()
    first.begin_caller_turn()
    second.begin_caller_turn()

    assert first.turn_counter == 2
    assert second.turn_counter == 1
    assert first.banking_session_id != second.banking_session_id


def test_authenticating_one_call_leaves_another_a_stranger():
    from app.auth.authentication import submit_customer_id, submit_pin

    first_session = session_manager.create_session()
    second_session = session_manager.create_session()
    submit_customer_id(first_session.session_id, "DEMO001")
    submit_pin(first_session.session_id, PINS["DEMO001"])

    first = state_for(first_session.session_id, "call-a")
    second = state_for(second_session.session_id, "call-b")

    assert first.customer_reference == "DEMO001"
    assert second.customer_reference is None
    assert second.authenticated is False


def test_no_module_level_conversation_registry_exists():
    """Per call, not per process, and nothing keyed by customer."""
    import app.telephony.conversation as module

    for name, value in vars(module).items():
        if name.startswith("_") or name.isupper():
            continue
        assert not isinstance(value, (dict, list, set)), f"{name} is shared state"


# === 3. authentication and pending-request continuity, phone path ===========


def test_a_question_asked_before_verification_is_answered_after_it():
    """The scenario, end to end, on the telephone path.

    "What is my savings balance?" before the bank knows who is calling; then an
    id, then a PIN. The enquiry must survive, be answered automatically, and the
    caller must not be asked what they wanted all over again.
    """
    from app import pending_request
    from app.auth.authentication import submit_customer_id, submit_pin
    from app.tools import accounts

    session = session_manager.create_session()
    state = state_for(session.session_id)

    # 1. The caller asks before anyone knows who they are.
    refusal = accounts.get_account_balance(session.session_id, "Savings")
    assert refusal == {"success": False, "reason": "NOT_AUTHENTICATED"}

    # The realtime tool layer is what remembers it; do what it does.
    live = session_manager.get_session(session.session_id)
    pending_request.remember(
        live, tool="get_account_balance", account_type="Savings",
        manager=session_manager,
    )
    assert state.pending_request == {
        "tool": "get_account_balance", "account_type": "Savings",
    }

    # 2. Identity, then PIN.
    assert submit_customer_id(session.session_id, "DEMO001")["success"] is True
    assert state.pending_request is not None, "the enquiry was lost at the id step"

    verified = submit_pin(session.session_id, PINS["DEMO001"])
    assert verified["authenticated"] is True

    # 3. The enquiry survived authentication and is still the held one.
    assert state.authenticated is True
    assert state.pending_request == {
        "tool": "get_account_balance", "account_type": "Savings",
    }

    # 4. It is answered without the caller repeating themselves.
    answer = accounts.get_account_balance(
        session.session_id, state.pending_request["account_type"]
    )
    assert answer["success"] is True
    assert answer["available_balance"]


def test_the_pin_result_carries_the_held_enquiry_to_the_agent():
    """How the agent knows to answer instead of asking "how may I help you?"."""
    from app import pending_request
    from app.realtime import tools as realtime_tools

    session = session_manager.create_session()
    live = session_manager.get_session(session.session_id)
    pending_request.remember(
        live, tool="get_account_balance", account_type="Savings",
        manager=session_manager,
    )

    import inspect

    source = inspect.getsource(realtime_tools)
    assert 'result = {**result, "pending_request": pending.to_dict()}' in source


def test_the_agent_is_told_not_to_ask_again():
    from app.realtime.banking_realtime import INSTRUCTIONS

    assert "do not ask" in INSTRUCTIONS.lower()
    assert "pending_request" in INSTRUCTIONS
    assert "How may I assist you today?" in INSTRUCTIONS


def test_the_held_enquiry_is_released_once_answered():
    """A held enquiry left lying about keeps the gate exemption open."""
    from app import pending_request

    session = session_manager.create_session()
    live = session_manager.get_session(session.session_id)
    pending_request.remember(
        live, tool="get_account_balance", account_type="Savings",
        manager=session_manager,
    )
    state = state_for(session.session_id)
    assert state.pending_request is not None

    pending_request.clear(live, manager=session_manager)

    assert state.pending_request is None


def test_a_verified_call_is_not_asked_for_credentials_again():
    """Later enquiries on the same call must not re-request id or PIN."""
    from app.auth.authentication import submit_customer_id, submit_pin
    from app.tools import accounts, loans

    session = session_manager.create_session()
    submit_customer_id(session.session_id, "DEMO001")
    submit_pin(session.session_id, PINS["DEMO001"])

    first = accounts.get_account_balance(session.session_id, "Savings")
    second = accounts.get_account_details(session.session_id, "Savings")
    third = loans.get_loan_balance(session.session_id, None)

    for result in (first, second, third):
        assert result.get("reason") != "NOT_AUTHENTICATED"
    assert first["success"] is True

    # And re-verifying is refused rather than restarting authentication.
    again = submit_customer_id(session.session_id, "DEMO001")
    assert again["success"] is False
    assert again["reason"] == "ALREADY_AUTHENTICATED"


def test_one_callers_held_enquiry_never_reaches_another():
    """Continuity is per session, so two callers cannot resume each other's."""
    from app import pending_request

    first_session = session_manager.create_session()
    second_session = session_manager.create_session()
    pending_request.remember(
        session_manager.get_session(first_session.session_id),
        tool="get_loan_balance", loan_type="Home Loan", manager=session_manager,
    )

    first = state_for(first_session.session_id, "call-a")
    second = state_for(second_session.session_id, "call-b")

    assert first.pending_request == {
        "tool": "get_loan_balance", "loan_type": "Home Loan",
    }
    assert second.pending_request is None


def test_the_held_enquiry_carries_no_identity():
    """It is a question, not a customer."""
    from app import pending_request

    session = session_manager.create_session()
    pending_request.remember(
        session_manager.get_session(session.session_id),
        tool="get_account_balance", account_type="Savings", manager=session_manager,
    )
    held = state_for(session.session_id).pending_request

    assert set(held) <= {"tool", "account_type", "loan_type"}
    assert "DEMO001" not in str(held)


def test_a_suppressed_response_does_not_resume_when_the_first_one_ends():
    """The defect this guards: without it the caller hears a second answer.

    Two responses generate concurrently. The admitted one ends, releasing
    `active_response_id` — and the suppressed one's remaining audio was then
    adopted as the next answer, so the caller heard the tail of a sentence they
    were never meant to hear the start of.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, Audio("resp-A"))
    bridge.on_realtime_event(sid, Audio("resp-B"))       # suppressed
    bridge.on_realtime_event(sid, Plain("audio_end"))    # resp-A finishes
    bridge.on_realtime_event(sid, Audio("resp-B"))       # its tail arrives
    bridge.on_realtime_event(sid, Audio("resp-B"))

    assert len(bridge.outbound) == 1, "a suppressed response resumed"
    assert bridge.conversation.active_response_id is None


def test_a_rejected_response_id_is_forgotten_on_the_next_caller_turn():
    """Rejection is scoped to the turn, like every other suppression here."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    bridge.on_realtime_event(sid, Audio("resp-A"))
    bridge.on_realtime_event(sid, Audio("resp-B"))
    assert bridge.conversation.rejected_response_ids == {"resp-B"}

    bridge.on_realtime_event(sid, Plain("raw_model_event", data=Plain("turn_started")))

    assert bridge.conversation.rejected_response_ids == set()
