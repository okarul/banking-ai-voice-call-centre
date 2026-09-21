"""Phase 7.4F: whole calls, counted by what the caller actually heard.

Fourteen end-to-end flows on the backend-owned question path. Every assertion is
about caller-visible speech - how many times a question reached the line - rather
than about internal state, because a call that ends verified having asked for the
PIN twice is a failed call however tidy its state looks.

Helpers come from `test_owned_question_regressions`, which imports the harness
from `test_auth_turn_ownership`. That direction is deliberate and acyclic.
"""

import pytest

from app import pending_clarification, pending_credential
from app.auth.authentication import submit_customer_id, submit_pin
from app.sessions import session_manager
from tests.test_auth_turn_ownership import (
    ANSWER,
    ASK_ID,
    ASK_PIN,
    CLARIFY,
    CURRENT_ANSWER,
    CUSTOMER,
    GOODBYE,
    Heard,
    REAL_PIN,
    RECOVERY,
    WRONG_PIN,
)
from tests.test_owned_question_regressions import (
    PREFACE,
    ended_generation,
    own_clarification,
    own_credential,
)


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


def ask_id(call, call_n=0):
    """The bank asks for the customer ID through a response it owns."""
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id=f"oid-{call_n}")
    heard = call.agent(ASK_ID, response=owned, item=f"iid-{call_n}")
    call.generation_ended()
    return heard


def ask_pin(call, call_n=0):
    owned, _ = own_credential(call, "PIN", response_id=f"opin-{call_n}")
    heard = call.agent(ASK_PIN, response=owned, item=f"ipin-{call_n}")
    call.generation_ended()
    return heard


def ask_account(call, call_n=0):
    owned, _ = own_clarification(call, response_id=f"oacc-{call_n}")
    heard = call.agent(CLARIFY, response=owned, item=f"iacc-{call_n}")
    call.generation_ended()
    return heard


def give_id(call, customer=CUSTOMER):
    call.caller(f"my customer id is {customer}")
    return submit_customer_id(call.sid, customer)


def give_pin(call, pin=REAL_PIN):
    call.caller("my pin is " + " ".join(pin))
    return submit_pin(call.sid, pin)


# === 1-3. the ordinary calls =================================================


def test_flow_1_generic_balance_with_clarification():
    call = Heard("f1")
    call.caller("I want to know my account balance.")
    assert ask_id(call, 1)
    give_id(call)
    assert ask_pin(call, 1)
    assert give_pin(call)["authenticated"] is True
    assert ask_account(call, 1)
    call.caller("Savings")
    pending_clarification.complete_with(
        session_manager.get_session(call.sid),
        pending_clarification.Domain.ACCOUNT,
        "Savings",
        manager=session_manager,
    )
    call.agent(ANSWER)
    call.generation_ended()
    call.caller("that's all, thank you")
    call.agent(GOODBYE)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(CLARIFY) == 1
    assert call.heard(ANSWER) == 1
    assert call.heard(GOODBYE) == 1


def test_flow_2_direct_savings_balance():
    call = Heard("f2")
    call.caller("what is my savings balance")
    assert ask_id(call, 2)
    give_id(call)
    assert ask_pin(call, 2)
    give_pin(call)
    call.agent(ANSWER)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(ANSWER) == 1


def test_flow_3_current_balance():
    call = Heard("f3")
    call.caller("what is my current account balance")
    assert ask_id(call, 3)
    give_id(call)
    assert ask_pin(call, 3)
    give_pin(call)
    call.agent(CURRENT_ANSWER)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(CURRENT_ANSWER) == 1


# === 4-5. the authorised retries =============================================


def test_flow_4_wrong_pin_then_correct():
    call = Heard("f4")
    call.caller("what is my savings balance")
    assert ask_id(call, 4)
    give_id(call)
    assert ask_pin(call, 4)

    assert give_pin(call, WRONG_PIN)["success"] is False
    assert ask_pin(call, 44), "the authorised PIN retry was withheld"
    assert give_pin(call)["authenticated"] is True
    call.agent(ANSWER)

    assert call.heard(ASK_PIN) == 2, "one question, one authorised retry"
    assert call.heard(ANSWER) == 1


def test_flow_5_invalid_id_then_valid():
    call = Heard("f5")
    call.caller("what is my savings balance")
    assert ask_id(call, 5)

    call.caller("it's, er, one two three")
    assert ask_id(call, 55), "the authorised ID retry was withheld"
    give_id(call)
    assert ask_pin(call, 5)

    assert call.heard(ASK_ID) == 2
    assert call.heard(ASK_PIN) == 1


# === 6-7. the caller taking their time =======================================


def test_flow_6_slow_id_is_never_asked_twice():
    call = Heard("f6")
    call.caller("what is my savings balance")
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id="slow-id")
    call.agent(ASK_ID, response=owned, item="q1")
    call.generation_ended()

    for n in range(3):
        call.speech_starts()
        call.agent(ASK_ID, response=f"foreign-{n}", item=f"f{n}")

    assert call.heard(ASK_ID) == 1


def test_flow_7_slow_pin_is_never_asked_twice():
    call = Heard("f7")
    call.caller("what is my savings balance")
    ask_id(call, 7)
    give_id(call)
    owned, _ = own_credential(call, "PIN", response_id="slow-pin")
    call.agent(ASK_PIN, response=owned, item="q1")
    call.generation_ended()

    for n in range(3):
        call.speech_starts()
        call.agent(ASK_PIN, response=f"foreign-{n}", item=f"f{n}")

    assert call.heard(ASK_PIN) == 1


# === 8-10. the races ========================================================


def test_flow_8_tool_continuation_races_the_owned_id_question():
    call = Heard("f8")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id="race-id")

    call.tool("get_authentication_status")
    call.agent(ASK_ID, response="continuation", item="c1")
    assert call.agent(ASK_ID, response=owned, item="q1") is True

    assert call.heard(ASK_ID) == 1


def test_flow_9_tool_continuation_races_the_owned_clarification():
    call = Heard("f9")
    call.caller("I want to know my account balance.")
    ask_id(call, 9)
    give_id(call)
    ask_pin(call, 9)
    give_pin(call)

    owned, _ = own_clarification(call, response_id="race-acc")
    call.tool("get_account_balance")
    call.agent(CLARIFY, response="continuation", item="c1")
    assert call.agent(CLARIFY, response=owned, item="q1") is True

    assert call.heard(CLARIFY) == 1


def test_flow_10_foreign_acknowledgement_races_the_owned_id_question():
    call = Heard("f10")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id="race-ack")

    assert call.agent(PREFACE, response="auto", item="a1") is False
    assert call.agent(ASK_ID, response=owned, item="q1") is True

    assert call.heard(ASK_ID) == 1
    assert call.heard(PREFACE) == 0


# === 11-12. delivery that did not happen ====================================


def test_flow_11_owned_question_produces_no_audio_then_recovers():
    call = Heard("f11")
    call.caller("I want to know my account balance.")
    own_credential(call, "CUSTOMER_ID", response_id="silent")

    ended_generation(call)

    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) == "CUSTOMER_ID"

    assert ask_id(call, 11), "the bounded retry never reached the caller"
    assert call.heard(ASK_ID) == 1


def test_flow_12_owned_question_interrupted_then_retried():
    call = Heard("f12")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id="cut-off")
    call.agent(ASK_ID, response=owned, item="q1")

    call.barge_in()
    assert call.bridge.conversation.owned_question_response is None

    call.caller("sorry, what was that")
    assert ask_id(call, 12)


# === 13-14. the caller steering ==============================================


def test_flow_13_i_dont_know_earns_one_owned_clarification_re_ask():
    call = Heard("f13")
    call.caller("I want to know my account balance.")
    ask_id(call, 13)
    give_id(call)
    ask_pin(call, 13)
    give_pin(call)
    assert ask_account(call, 13)

    call.caller("I don't know")

    session = session_manager.get_session(call.sid)
    assert pending_clarification.awaiting_question(session) is not None, (
        "an unusable answer left the clarification unaskable"
    )
    assert ask_account(call, 133), "the authorised clarification re-ask was withheld"

    # Two, and two is correct. The caller heard the question, gave an answer the
    # bank could not use, and was asked once more - which is exactly what an
    # authorised re-ask is. `Heard` counts distinct items, so these are two
    # separate utterances rather than one counted twice.
    #
    # Asserting 1 here would have asserted the defect this phase exists to fix
    # from the other direction: a caller who says "I don't know" and is never
    # asked again.
    assert call.heard(CLARIFY) == 2, (
        "an unusable answer must earn exactly one re-ask: the question, then "
        "one more, and no further"
    )


def test_flow_14_goodbye_during_a_pending_question_closes_the_call():
    call = Heard("f14")
    call.caller("I want to know my account balance.")
    assert ask_id(call, 14)

    call.caller("actually, goodbye")

    session = session_manager.get_session(call.sid)
    assert pending_clarification.recall(session) is None
    assert call.agent(GOODBYE) is True, "the caller never heard the bank sign off"
    assert call.heard(ASK_ID) == 1
