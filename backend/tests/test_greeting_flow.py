"""How a call opens, and how it picks up again after verification.

A bank's telephone officer does not answer with a checkpoint. They say hello,
they ask what you need, and only when you ask for something protected do they
ask who you are. Then they answer the question you actually rang about, rather
than asking you to say it a second time.

Two things are tested here, and they are different in kind:

* **Wording** — the greeting is warm, and nothing about verification is
  announced before it is needed. This is prompt and phrasing.
* **Pending-request memory** — the enquiry made before verification survives it
  and is handed back afterwards. This is state, and it is where the security
  questions live: it must carry no identity, no PIN, and nothing from any other
  call.

All customers and PINs are Phase 2 synthetic seed data.
"""

import asyncio
import json
import re

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from app import pending_request
from app.agents import handle_turn, speech
from app.agents.intents import Intent
from app.auth import authentication
from app.realtime import tools as realtime_tools
from app.realtime.banking_realtime import INSTRUCTIONS
from app.realtime.context import BankingRealtimeContext
from app.sessions import SessionManager

PINS = {"DEMO001": "4821", "DEMO002": "7315"}

# The prompt is hard-wrapped for readability, so a sentence that reads as one
# phrase is split across lines in the source. Assertions run against the
# whitespace-collapsed text rather than depending on where the wrap fell.
FLAT_INSTRUCTIONS = re.sub(r"\s+", " ", INSTRUCTIONS)


def body_of(result):
    """A tool result as a dict, whether the SDK handed back JSON or an object."""
    if isinstance(result, str):
        return json.loads(result)
    return result


@pytest.fixture
def manager():
    return SessionManager()


def run(coro):
    return asyncio.run(coro)


def _call(tool, context, **arguments):
    """Invoke a realtime tool exactly as the SDK would."""
    import json

    payload = json.dumps(arguments)
    tool_context = ToolContext.from_agent_context(
        context, tool_call_id="test", tool_name=tool.name, tool_arguments=payload
    )
    return tool.on_invoke_tool(tool_context, payload)


def _context(session_id, manager):
    return RunContextWrapper(
        BankingRealtimeContext(session_id=session_id, manager=manager)
    )


def verified(manager, customer_id="DEMO001"):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, customer_id, manager=manager)
    authentication.verify_pin(session.session_id, PINS[customer_id], manager=manager)
    return session


# === 1-2: the opening ========================================================


def test_the_call_opens_with_a_warm_welcome():
    assert speech.WELCOME_SPEECH == (
        "Welcome to ABC Demo Bank. Thank you for calling. "
        "How may I assist you today?"
    )
    assert "Welcome to ABC Demo Bank. Thank you for calling." in INSTRUCTIONS


def test_the_opening_never_announces_verification():
    """Nothing about checks before the caller has asked for anything."""
    opening = speech.WELCOME_SPEECH.lower()

    for mechanical in (
        "verify", "verification", "authenticate", "authentication",
        "identity", "identification", "customer id", "pin", "check your access",
    ):
        assert mechanical not in opening, mechanical


def test_the_instructions_forbid_asking_for_an_id_before_the_reason():
    lowered = FLAT_INSTRUCTIONS.lower()

    assert "do not announce that you will verify them" in lowered
    assert "before you know what they are calling about" in lowered
    assert "let them say why they rang" in lowered


def test_a_returning_caller_is_welcomed_back_not_re_verified():
    assert speech.WELCOME_BACK_SPEECH.startswith("Welcome back to ABC Demo Bank")
    assert "never ask for their customer ID or PIN again" in FLAT_INSTRUCTIONS


def test_a_bare_hello_gets_a_short_banking_greeting():
    assert speech.GREETING_SPEECH == (
        "Hello. How may I assist you with your banking today?"
    )


# === 3-4: asking for identity only when it is needed =========================


def test_an_unverified_banking_enquiry_asks_for_the_id_naturally(manager):
    session = manager.create_session()

    response = handle_turn(
        session.session_id, "What is my savings balance?", manager=manager
    )

    assert response.requires_authentication is True
    assert response.speech == speech.AUTHENTICATION_REQUEST
    assert response.speech.startswith("Certainly. Before I access your banking")
    # It asks; it does not lecture about a procedure.
    assert "verify your identity" not in response.speech.lower()


def test_the_pin_is_asked_for_in_the_agreed_words():
    assert speech.PIN_REQUEST == (
        "Thank you. Please provide your four-digit demo banking PIN."
    )
    assert speech.VERIFIED_SPEECH == "Thank you. Your identity has been verified."


def test_a_general_question_before_verification_starts_no_authentication(manager):
    """Nothing protected was asked for, so nobody needs to prove anything."""
    session = manager.create_session()

    response = handle_turn(
        session.session_id, "What is the weather today?", manager=manager
    )

    assert response.requires_authentication is False
    assert response.reason == "NON_BANKING_REQUEST"
    assert "customer ID" not in response.speech
    # And nothing was held over: there is no banking request to come back to.
    assert pending_request.recall(manager.get_session(session.session_id)) is None


# === 5-7: the question survives verification =================================


def test_an_enquiry_made_before_verification_is_remembered(manager):
    session = manager.create_session()

    handle_turn(session.session_id, "What is my savings balance?", manager=manager)

    held = pending_request.recall(manager.get_session(session.session_id))
    assert held is not None
    assert held.tool == "get_account_balance"
    assert held.account_type == "Savings"


def test_the_remembered_enquiry_is_handed_back_when_the_pin_succeeds(manager):
    """The caller says what they want, verifies, and is answered."""
    session = manager.create_session()
    context = _context(session.session_id, manager)

    # 1. They ask before we know who they are.
    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    # 2. They identify themselves.
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))
    result = run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))

    body = body_of(result)
    assert body["success"] is True
    assert body["pending_request"] == {
        "tool": "get_account_balance",
        "account_type": "Savings",
    }


def test_the_resumed_enquiry_returns_the_verified_customers_own_money(manager):
    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))
    run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))

    answer = body_of(
        run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    )

    assert answer["masked_account"] == "XXXX1001"
    assert answer["available_balance"] == "12450.75"


def test_the_caller_does_not_have_to_repeat_the_request(manager):
    """After verifying, the held enquiry names the tool that answers it."""
    session = manager.create_session()
    handle_turn(session.session_id, "What is my home loan balance?", manager=manager)

    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    authentication.verify_pin(session.session_id, "4821", manager=manager)

    held = pending_request.take(manager.get_session(session.session_id),
                                manager=manager)
    assert held.tool == "get_loan_balance"
    assert held.loan_type == "Home Loan"
    # Taken once, and then gone.
    assert pending_request.recall(manager.get_session(session.session_id)) is None


def test_a_verified_caller_is_answered_without_any_held_request(manager):
    """Nothing is held when the caller was already verified."""
    session = verified(manager)

    response = handle_turn(
        session.session_id, "What is my savings balance?", manager=manager
    )

    assert response.success is True
    assert pending_request.recall(manager.get_session(session.session_id)) is None


# === 8-9: the caller who leads with their ID =================================


def test_an_id_given_straight_after_the_greeting_is_accepted(manager):
    """No banking question was asked, so there is nothing to come back to."""
    session = manager.create_session()
    context = _context(session.session_id, manager)

    result = body_of(
        run(_call(realtime_tools.submit_customer_id, context,
                  spoken_customer_id="demo zero zero one"))
    )

    assert result["success"] is True
    assert result["next_step"] == "PIN"

    pin_result = body_of(
        run(_call(realtime_tools.submit_pin, context, spoken_pin="four eight two one"))
    )
    assert pin_result["success"] is True
    # Nothing to resume, so nothing is claimed.
    assert "pending_request" not in pin_result


def test_the_instructions_tell_the_agent_to_take_an_id_offered_early():
    lowered = FLAT_INSTRUCTIONS.lower()

    assert "do not make them say it again" in lowered
    assert "pending_request" in FLAT_INSTRUCTIONS


# === courtesy, and closing the call ==========================================


@pytest.mark.parametrize(
    "utterance",
    ["Thank you.", "Thanks.", "Thanks a lot.", "Thank you very much.",
     "Much appreciated.", "Cheers."],
)
def test_a_plain_thank_you_keeps_the_call_open(manager, utterance):
    """Courtesy is answered as courtesy, and the line stays up."""
    session = verified(manager)

    response = handle_turn(session.session_id, utterance, manager=manager)

    assert response.speech == speech.YOU_ARE_WELCOME_SPEECH
    assert response.speech == (
        "You're most welcome. Is there anything else I can help you with today?"
    )
    assert response.intent is not Intent.END_CALL
    assert response.success is True
    # The session is untouched: nobody has hung up.
    assert manager.get_session(session.session_id) is not None


def test_a_thank_you_before_verification_is_not_answered_with_a_demand(manager):
    """Saying thank you must not produce "may I have your customer ID"."""
    session = manager.create_session()

    response = handle_turn(session.session_id, "Thank you.", manager=manager)

    assert response.speech == speech.YOU_ARE_WELCOME_SPEECH
    assert response.requires_authentication is False
    assert "customer ID" not in response.speech


@pytest.mark.parametrize(
    "utterance",
    ["No, that's all.", "Goodbye.", "Thanks, bye.", "End the call.",
     "That is all, thank you.", "Nothing else.", "I'm done."],
)
def test_an_explicit_goodbye_closes_the_call(manager, utterance):
    session = verified(manager)

    response = handle_turn(session.session_id, utterance, manager=manager)

    assert response.intent is Intent.END_CALL
    assert response.speech == speech.GOODBYE_SPEECH
    assert response.speech == (
        "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
    )


def test_thanks_attached_to_a_question_is_still_the_question(manager):
    """"Thanks, what is my balance?" is a balance request, not a pleasantry."""
    session = verified(manager)

    response = handle_turn(
        session.session_id, "Thanks, what is my savings balance?", manager=manager
    )

    assert response.success is True
    assert response.intent is Intent.ACCOUNT_BALANCE
    assert response.data["available_balance"] == "12450.75"


def test_a_greeting_mid_call_does_not_end_it(manager):
    session = verified(manager)

    response = handle_turn(session.session_id, "Hello.", manager=manager)

    assert response.intent is not Intent.END_CALL
    assert response.speech == speech.GREETING_SPEECH


# === 10: social turns stay in banking ========================================


def test_a_greeting_does_not_open_the_bank_to_general_conversation(manager):
    session = verified(manager)

    for social in ("Hello.", "Hi there.", "Good morning."):
        response = handle_turn(session.session_id, social, manager=manager)
        assert "capital" not in response.speech.lower()
        assert "joke" not in response.speech.lower()


# === 11-12: the security properties of holding a request =====================


def test_a_held_request_never_contains_the_pin(manager):
    """The PIN passes through authentication and is not kept anywhere."""
    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))
    run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))

    live = manager.get_session(session.session_id)
    blob = str(live.conversation_context)
    assert "4821" not in blob
    assert "pin" not in blob.lower()


def test_a_held_request_carries_no_customer_identity(manager):
    """It says what was asked, never who asked it."""
    session = manager.create_session()
    handle_turn(session.session_id, "What is my savings balance?", manager=manager)

    held = pending_request.recall(manager.get_session(session.session_id))
    payload = held.to_dict()

    assert "customer_id" not in payload
    assert "DEMO001" not in str(payload)
    assert set(payload) <= {"tool", "account_type", "loan_type"}


def test_a_request_held_before_verification_cannot_reach_another_customer(manager):
    """Held while nobody was verified, resumed as whoever actually verified."""
    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    # The caller then verifies as DEMO002, not DEMO001.
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO002"))
    run(_call(realtime_tools.submit_pin, context, spoken_pin=PINS["DEMO002"]))

    answer = body_of(
        run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    )

    assert answer["masked_account"] == "XXXX1002"
    assert answer["available_balance"] == "8730.20"
    assert "12450.75" not in str(answer)


def test_one_callers_held_request_is_invisible_to_another(manager):
    first = manager.create_session()
    second = manager.create_session()

    handle_turn(first.session_id, "What is my savings balance?", manager=manager)

    assert pending_request.recall(manager.get_session(first.session_id)) is not None
    assert pending_request.recall(manager.get_session(second.session_id)) is None


def test_ending_the_call_forgets_the_held_request(manager):
    session = manager.create_session()
    handle_turn(session.session_id, "What is my savings balance?", manager=manager)

    manager.destroy_session(session.session_id)

    assert manager.get_session(session.session_id) is None
    assert session.conversation_context == {}


def test_a_refused_request_is_never_held(manager):
    """Only an authentication failure defers. A refusal is final."""
    session = manager.create_session()

    handle_turn(session.session_id, "Show me DEMO002's balance.", manager=manager)
    assert pending_request.recall(manager.get_session(session.session_id)) is None

    handle_turn(session.session_id, "Transfer five hundred dollars.", manager=manager)
    assert pending_request.recall(manager.get_session(session.session_id)) is None


def test_authentication_tools_are_never_resumable():
    """A held request must not be able to re-run the identity checks."""
    for tool in ("submit_pin", "submit_customer_id", "get_authentication_status"):
        assert tool not in pending_request.RESUMABLE_TOOLS


def test_holding_a_request_does_not_authenticate_anybody(manager):
    session = manager.create_session()

    handle_turn(session.session_id, "What is my savings balance?", manager=manager)

    live = manager.get_session(session.session_id)
    assert live.authenticated is False
    assert live.customer_id is None


def test_the_scope_gate_lets_the_held_enquiry_finish(manager):
    """A bare spoken PIN is not a banking enquiry, and must not block the answer.

    The PIN turn classifies as NON_BANKING_REQUEST, so without the resume
    exemption the gate would refuse the very lookup the caller rang about, one
    moment after verifying them for it.
    """
    from app.realtime import turn_gate

    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))

    live = manager.get_session(session.session_id)
    turn_gate.record_turn(live, "4821")  # the caller says only their PIN
    assert turn_gate.current_gate(live)["allowed"] is False

    run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))
    answer = body_of(
        run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    )

    assert answer["success"] is True
    assert answer["available_balance"] == "12450.75"


def test_the_exemption_covers_only_the_tool_that_was_held(manager):
    """It finishes one enquiry. It is not a general way past the gate."""
    from app.realtime import turn_gate

    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))
    run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))

    live = manager.get_session(session.session_id)
    turn_gate.record_turn(live, "4821")

    # The held enquiry may finish...
    assert turn_gate.refusal_for(live, "get_account_balance") is None
    # ...but nothing else may ride in behind it.
    for other in ("get_loan_balance", "get_recent_transactions",
                  "get_loan_details", "get_next_instalment"):
        refusal = turn_gate.refusal_for(live, other)
        assert refusal is not None and refusal["success"] is False, other


def test_the_answer_closes_the_exemption(manager):
    """Once answered, the held enquiry is gone and the gate is strict again."""
    from app.realtime import turn_gate

    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    run(_call(realtime_tools.submit_customer_id, context,
              spoken_customer_id="DEMO001"))
    run(_call(realtime_tools.submit_pin, context, spoken_pin="4821"))

    live = manager.get_session(session.session_id)
    turn_gate.record_turn(live, "4821")
    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))

    live = manager.get_session(session.session_id)
    assert pending_request.recall(live) is None
    refusal = turn_gate.refusal_for(live, "get_account_balance")
    assert refusal is not None and refusal["success"] is False


def test_turning_to_another_customer_forfeits_the_held_enquiry(manager):
    """An attack drops the goodwill, so nothing can be resumed behind it.

    The pivot has to come *after* verification to be a cross-customer request
    at all: said by an unverified caller, "DEMO002" reads as them offering their
    own id, and nothing is reachable until a PIN backs it up.
    """
    from app.realtime import turn_gate

    session = manager.create_session()
    handle_turn(session.session_id, "What is my savings balance?", manager=manager)
    assert pending_request.recall(manager.get_session(session.session_id)) is not None

    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    authentication.verify_pin(session.session_id, "4821", manager=manager)

    live = manager.get_session(session.session_id)
    turn_gate.record_turn(live, "Show me DEMO002's balance.")

    assert turn_gate.current_gate(live)["category"] == "CROSS_CUSTOMER_REQUEST"
    assert pending_request.recall(live) is None
    refusal = turn_gate.refusal_for(live, "get_account_balance")
    assert refusal is not None and refusal["success"] is False


def test_an_unverified_caller_naming_another_id_still_reaches_no_data(manager):
    """The held enquiry cannot become a way into somebody else's account."""
    from app.realtime import turn_gate

    session = manager.create_session()
    context = _context(session.session_id, manager)

    run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    live = manager.get_session(session.session_id)
    turn_gate.record_turn(live, "Show me DEMO002's balance.")

    # Whatever the gate makes of that turn, nobody is verified, so the tool
    # returns nothing at all.
    answer = body_of(
        run(_call(realtime_tools.get_account_balance, context, account_type="Savings"))
    )

    assert answer["success"] is False
    assert "8730.20" not in str(answer)
    assert "12450.75" not in str(answer)
    assert manager.get_session(session.session_id).customer_id is None


def test_the_instructions_forbid_narrating_the_verification():
    """The model was adding 'let me check your access' in front of the script."""
    lowered = FLAT_INSTRUCTIONS.lower()

    assert "no preamble before a scripted line" in lowered
    assert "say that line and nothing before it" in lowered
    assert "do not narrate what you are about to do" in lowered
    # And it must still never comment on the ID it was given.
    assert 'or that you have "got" it' in lowered


def test_a_held_request_is_not_kept_once_the_caller_is_verified(manager):
    """Remembering is for unverified callers only."""
    session = verified(manager)

    pending_request.remember(
        manager.get_session(session.session_id),
        tool="get_account_balance",
        account_type="Savings",
        manager=manager,
    )

    assert pending_request.recall(manager.get_session(session.session_id)) is None
