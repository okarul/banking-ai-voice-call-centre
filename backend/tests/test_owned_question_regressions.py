"""Phase 7.4F: the permanent regressions for backend-owned question delivery.

Every live failure this programme has had in the authentication path came from
the same root: a caller-visible action owned by the model rather than by the
bank. This file holds the cases that must never come back, each named for the
call that produced it where there was one.

The architecture they protect. The bank issues its own `response.create` for the
three state-machine questions - demo customer ID, PIN, Savings-or-Current -
carrying a correlation token in the response metadata. The server echoes that
metadata in `response.created`, which reaches the bridge through the raw event
stream, and the bridge adopts the `response.id` only when the token *and* the
banking session *and* the question kind all agree. Audio on that id is the
question. Audio on anything else is not, whatever it says.

Nothing here compares words, and neither does the production code. That is the
whole point: two shapes that were indistinguishable by wording -

    one response, items [preface,  question]
    one response, items [question, question]

- are no longer distinguished by wording either. The first cannot happen on an
owned response with `tool_choice: "none"` and `tools: []`, and if the provider
ever breaks that contract the violation is counted rather than accommodated.
"""

import asyncio

import pytest

from app import pending_clarification, pending_credential
from app.auth.authentication import submit_customer_id, submit_pin
from app.sessions import session_manager
from tests.test_auth_turn_ownership import (
    ANSWER,
    ASK_ID,
    ASK_PIN,
    CLARIFY,
    CUSTOMER,
    Event,
    Heard,
    REAL_PIN,
)

PREFACE = "Certainly, I can help you with that."
FOREIGN_QUESTION = "And your demo customer ID?"


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


# --- the pump, minus the socket ---------------------------------------------


def response_created(call, kind, token, response_id, *, session=None):
    """`response.created`, exactly as the raw server stream delivers it."""
    call.bridge.on_realtime_event(
        call.sid,
        Event(
            "raw_model_event",
            data=Event(
                "raw_server_event",
                data={
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "metadata": {
                            "bank_question": kind,
                            "token": token,
                            "session": session if session is not None else call.sid,
                        },
                    },
                },
            ),
        ),
    )


def own_credential(call, credential, *, response_id=None):
    """Issue, mark and confirm an owned credential question."""
    response_id = response_id or f"owned-{credential.lower()}"
    token = pending_credential.issue_owned_question(
        session_manager.get_session(call.sid), credential, manager=session_manager
    )
    pending_credential.mark_question_asked(
        session_manager.get_session(call.sid), credential, manager=session_manager
    )
    response_created(call, credential, token, response_id)
    return response_id, token


def own_clarification(call, *, response_id="owned-account"):
    """Open a real clarification and own its question response."""
    pending_clarification.open_for(
        session_manager.get_session(call.sid),
        tool="get_account_balance",
        reason=pending_clarification.ACCOUNT_TYPE_REQUIRED,
        choices=("Savings", "Current"),
        manager=session_manager,
    )
    token = pending_clarification.issue_owned_question(
        session_manager.get_session(call.sid), manager=session_manager
    )
    pending_clarification.mark_question_asked(
        session_manager.get_session(call.sid), manager=session_manager
    )
    response_created(call, "ACCOUNT", token, response_id)
    return response_id, token


def ended_generation(call):
    """`audio_end`, actually executed.

    The bridge defers `_generation_finished` through `_schedule`, which closes
    the coroutine when no loop is running - silently. A synchronous test that
    merely emits the event never runs the recovery it is asserting about.
    """

    async def turn():
        call.bridge.on_realtime_event(call.sid, Event("audio_end"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(turn())


def at_pin_stage(call):
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")
    call.agent(ASK_ID, response=owned, item="q-id")
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    return call


def verified(call):
    at_pin_stage(call)
    owned, _ = own_credential(call, "PIN")
    call.agent(ASK_PIN, response=owned, item="q-pin")
    call.generation_ended()
    call.caller("my pin is four eight two one")
    assert submit_pin(call.sid, REAL_PIN)["authenticated"] is True
    call.generation_ended()
    return call


# === A. live duplicate 56f012b6 =============================================


def test_A_56f012b6_second_assistant_item_of_one_response_stays_suppressed():
    """One response, two items, the same request twice, six milliseconds apart.

    The caller heard both and hung up: `authenticated=false`,
    `tool_call_count=0`, `CUSTOMER_ENDED`. Phase 7.4D's item guard is what stops
    it, and no part of Phase 7.4F may relax that.
    """
    call = Heard("reg-a")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")

    assert call.agent(ASK_ID, response=owned, item="i1") is True
    assert call.agent(ASK_ID, response=owned, item="i2") is False

    assert call.heard(ASK_ID) == 1


# === B. live duplicate ca88679d =============================================


def test_B_ca88679d_tool_continuation_cannot_repeat_the_owned_question():
    """The failure this whole phase began with.

    AGENT asks for the customer ID, `get_authentication_status` runs, AGENT asks
    again with no caller turn in between. The caller heard both prompts and hung
    up. Now the question has an id the bank owns, and the continuation response
    the SDK creates after a tool result is simply not it.
    """
    call = Heard("reg-b")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")

    assert call.agent(ASK_ID, response=owned, item="q1") is True
    call.generation_ended()

    call.tool("get_authentication_status")
    assert call.agent(ASK_ID, response="auto-continuation", item="c1") is False

    assert call.heard(ASK_ID) == 1


# === C / D / E / F. foreign preface and foreign duplicate, per question ======


@pytest.mark.parametrize(
    "credential,question",
    [("CUSTOMER_ID", ASK_ID), ("PIN", ASK_PIN)],
)
def test_CE_a_foreign_preface_never_replaces_the_owned_credential_question(
    credential, question
):
    call = Heard(f"reg-c-{credential.lower()}")
    if credential == "PIN":
        at_pin_stage(call)
    else:
        call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, credential, response_id=f"own-{credential}")

    assert call.agent(PREFACE, response="auto-pre", item="p1") is False
    assert call.agent(question, response=owned, item="q1") is True

    assert call.heard(question) == 1
    assert call.heard(PREFACE) == 0


@pytest.mark.parametrize(
    "credential,question",
    [("CUSTOMER_ID", ASK_ID), ("PIN", ASK_PIN)],
)
def test_DE_a_foreign_response_never_asks_the_credential_question_again(
    credential, question
):
    call = Heard(f"reg-d-{credential.lower()}")
    if credential == "PIN":
        at_pin_stage(call)
    else:
        call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, credential, response_id=f"own2-{credential}")

    assert call.agent(question, response=owned, item="q1") is True
    call.generation_ended()
    assert call.agent(FOREIGN_QUESTION, response="auto-again", item="f1") is False

    assert call.heard(question) == 1
    assert call.heard(FOREIGN_QUESTION) == 0


def test_F_a_foreign_preface_never_replaces_the_owned_clarification():
    call = Heard("reg-f1")
    verified(call)
    owned, _ = own_clarification(call)

    assert call.agent(PREFACE, response="auto-pre", item="p1") is False
    assert call.agent(CLARIFY, response=owned, item="q1") is True
    assert call.heard(CLARIFY) == 1


def test_F_a_foreign_response_never_asks_the_clarification_again():
    call = Heard("reg-f2")
    verified(call)
    owned, _ = own_clarification(call, response_id="owned-account-2")

    assert call.agent(CLARIFY, response=owned, item="q1") is True
    call.generation_ended()
    call.tool("get_account_balance")
    assert call.agent(CLARIFY, response="auto-clar", item="c1") is False

    assert call.heard(CLARIFY) == 1


# === G. response.created mapping ============================================


def test_G_correct_metadata_is_adopted():
    call = Heard("reg-g1")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID", response_id="good")

    assert call.bridge.conversation.owned_question_response == "good"
    assert call.bridge.conversation.owned_question_kind == "CUSTOMER_ID"


def test_G_wrong_token_is_ignored():
    call = Heard("reg-g2")
    call.caller("I want to know my account balance.")
    pending_credential.issue_owned_question(
        session_manager.get_session(call.sid), "CUSTOMER_ID", manager=session_manager
    )

    response_created(call, "CUSTOMER_ID", "not-the-token", "intruder")

    assert call.bridge.conversation.owned_question_response is None


def test_G_stale_token_from_an_abandoned_attempt_is_ignored():
    """The first attempt produced no audio and was revoked. Its response is late."""
    call = Heard("reg-g3")
    call.caller("I want to know my account balance.")
    _, stale = own_credential(call, "CUSTOMER_ID", response_id="first")
    ended_generation(call)          # nothing delivered -> attempt revoked

    response_created(call, "CUSTOMER_ID", stale, "late-arrival")

    assert call.bridge.conversation.owned_question_response != "late-arrival"


def test_G_cross_session_metadata_is_ignored():
    call = Heard("reg-g4")
    call.caller("I want to know my account balance.")
    token = pending_credential.issue_owned_question(
        session_manager.get_session(call.sid), "CUSTOMER_ID", manager=session_manager
    )

    response_created(
        call, "CUSTOMER_ID", token, "someone-elses", session="SESSION-not-this-call"
    )

    assert call.bridge.conversation.owned_question_response is None


def test_G_unknown_question_kind_is_ignored():
    call = Heard("reg-g5")
    call.caller("I want to know my account balance.")
    token = pending_credential.issue_owned_question(
        session_manager.get_session(call.sid), "CUSTOMER_ID", manager=session_manager
    )

    response_created(call, "SOMETHING_ELSE", token, "wrong-kind")

    assert call.bridge.conversation.owned_question_response is None


# === H. owned response produced no audio ====================================


def test_H_an_owned_response_with_no_audio_is_retried_once():
    call = Heard("reg-h")
    call.caller("I want to know my account balance.")
    own_credential(call, "CUSTOMER_ID")

    ended_generation(call)          # not a single frame reached the caller

    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) == "CUSTOMER_ID", (
        "an owned question that produced no audio left the caller in silence"
    )
    assert pending_credential.owned_question(session) is None, (
        "the abandoned attempt's correlation was not released"
    )


def test_H_recovery_is_bounded_at_two_attempts():
    call = Heard("reg-h2")
    call.caller("I want to know my account balance.")

    delivered = 0
    for n in range(5):
        session = session_manager.get_session(call.sid)
        if pending_credential.awaiting_question(session) is None:
            break
        own_credential(call, "CUSTOMER_ID", response_id=f"own-{n}")
        ended_generation(call)
        delivered += 1

    assert delivered == 2, f"expected two bounded attempts, made {delivered}"


# === I. owned response interrupted ==========================================


def test_I_an_interrupted_owned_response_releases_ownership():
    call = Heard("reg-i")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")
    call.agent(ASK_ID, response=owned, item="q1")

    call.barge_in()

    conv = call.bridge.conversation
    assert conv.owned_question_response is None
    assert conv.delivered_question_kind is None
    assert conv.active_response_id is None
    assert conv.active_item_id is None


def test_I_the_question_can_be_asked_again_after_an_interruption():
    call = Heard("reg-i2")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")
    call.agent(ASK_ID, response=owned, item="q1")
    call.barge_in()

    call.caller("sorry, what was that")
    second, _ = own_credential(call, "CUSTOMER_ID", response_id="own-retry")
    assert call.agent(ASK_ID, response=second, item="q2") is True


# === J. API contract violation ==============================================


def test_J_a_multi_item_owned_response_is_a_counted_contract_violation():
    """`tool_choice: "none"` and `tools: []` say this cannot happen.

    If it does, the extra item is refused by Phase 7.4D exactly as any second
    item is, the violation is counted, and nothing here starts reading words to
    decide which item mattered.
    """
    call = Heard("reg-j")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")

    assert call.agent(ASK_ID, response=owned, item="q1") is True
    assert call.agent(ASK_ID, response=owned, item="q2") is False

    assert call.heard(ASK_ID) == 1
    assert call.bridge.conversation.owned_question_contract_violations == 1


def test_J_a_contract_violation_does_not_weaken_the_duplicate_guard():
    """The violation counter must not become a way through the item guard."""
    call = Heard("reg-j2")
    call.caller("I want to know my account balance.")
    owned, _ = own_credential(call, "CUSTOMER_ID")

    call.agent(ASK_ID, response=owned, item="q1")
    for n in range(4):
        assert call.agent(ASK_ID, response=owned, item=f"extra-{n}") is False

    assert call.heard(ASK_ID) == 1


# === over-suppression: the owned gate must not silence ordinary speech ======


def test_ordinary_speech_is_untouched_when_no_question_is_owned():
    call = Heard("reg-os")
    assert call.agent("Thank you for calling the bank.") is True
    call.generation_ended()
    call.caller("hello there")
    assert call.agent("How may I help you today?") is True


def test_the_business_answer_speaks_after_verification():
    call = Heard("reg-os2")
    verified(call)
    assert call.agent(ANSWER) is True
    assert call.heard(ANSWER) == 1
