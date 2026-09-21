"""Phase 7.4E: requesting a question is not the caller hearing one.

The delivery chain this phase built has four links, and until now only the ends
of it were tested:

    1. a question is *owed*          - `pending_credential.needed`,
                                       `pending_clarification.recall`
    2. a cue is *sent* to the model  - `_ask_for_credential`,
                                       `_answer_completed_clarification`
    3. the backend *marks it asked*  - `mark_question_asked`
    4. caller-visible audio *arrives*- `_admit_response` takes the owner

Link 3 fires on the strength of link 2. `mark_question_asked` is called once
`send_message` returns, which means it records **"a cue was submitted to the
model"** and not **"the caller heard the question"**. That is deliberate in
Phase 7.4C - its docstring says so, and the ordering is what makes a failed send
retry on the next event - but it means the two facts have one name between them,
and everything in this file is about the gap where they disagree.

The hazard, stated so a test can refute it: once the mark is set,
`awaiting_question` returns None, so the realtime pump will not cue again. If no
usable audio ever arrives - the model produces none, or the response is cut off
before a word reaches the line - then nothing has been heard, nothing is owned,
and nothing will ask again. Only a completed caller turn calls `clear()`, and a
caller who was never actually asked anything has no reason to speak. That is a
caller sitting in silence waiting for a question the bank believes it has put.

**No text inspection anywhere.** These tests distinguish the cases by state and
by which response or item carried the audio, never by what any sentence says.
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


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


# --- standing in for the realtime pump --------------------------------------
#
# In production `RealtimeManager._ask_for_credential` and
# `_answer_completed_clarification` send the cue and then mark. These helpers do
# exactly those two steps and nothing else, so a test can send a cue and then
# decline to produce any audio - which is the whole point of this file.


def cue_credential(call):
    """Send the credential cue and mark it, as the pump does. No audio."""
    session = session_manager.get_session(call.sid)
    owed = pending_credential.awaiting_question(session)
    assert owed is not None, "nothing was owed, so this test proves nothing"
    pending_credential.mark_question_asked(session, owed, manager=session_manager)
    return owed


def cue_clarification(call):
    """Open a clarification and mark its question cued. No audio."""
    session = session_manager.get_session(call.sid)
    pending_clarification.open_for(
        session,
        tool="get_account_balance",
        reason=pending_clarification.ACCOUNT_TYPE_REQUIRED,
        choices=("Savings", "Current"),
        manager=session_manager,
    )
    pending_clarification.mark_question_asked(session, manager=session_manager)
    return session


def response_created(call, kind, token, response_id, *, session=None):
    """The server confirming a response exists, as the raw stream delivers it.

    `_handle_ws_event` forwards every server event to listeners before any
    filtering, which is the only reason the backend can learn the id of a
    response it created - `send_event` returns nothing.
    """
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


def own_credential_question(call, credential, *, response_id=None):
    """Everything the pump does to put a credential question, minus the socket.

    Mints the correlation token, marks the cue submitted, and delivers the
    server's `response.created`. Returns the owned response id, which is what
    the caller-facing audio must arrive on.
    """
    response_id = response_id or f"owned-{credential.lower()}"
    session = session_manager.get_session(call.sid)
    token = pending_credential.issue_owned_question(
        session, credential, manager=session_manager
    )
    pending_credential.mark_question_asked(
        session_manager.get_session(call.sid), credential, manager=session_manager
    )
    response_created(call, credential, token, response_id)
    assert call.bridge.conversation.owned_question_response == response_id, (
        "the owned response was not adopted; the rest of this test proves nothing"
    )
    return response_id


def own_clarification_question(call, *, response_id="owned-account"):
    """The same, for the Savings-or-Current question."""
    session = session_manager.get_session(call.sid)
    pending_clarification.open_for(
        session,
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
    assert call.bridge.conversation.owned_question_response == response_id
    return response_id


def reach_pin_stage(call):
    """A call that has supplied a customer id and is owed the PIN question."""
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    return call


def verified(call):
    """A call all the way through verification."""
    reach_pin_stage(call)
    call.agent(ASK_PIN)
    call.generation_ended()
    call.caller("my pin is four eight two one")
    assert submit_pin(call.sid, REAL_PIN)["authenticated"] is True
    call.generation_ended()
    return call


# === §4 the question's own response must survive the mark ===================
#
# The cheapest way to get §2 and §3 wrong is to gate on "has it been marked",
# which would silence the very question the mark was set for. These three run
# first so that any fix below has to keep them.


def test_the_customer_id_question_survives_its_own_mark():
    call = Heard("own-id")
    call.caller("I want to know my account balance.")
    cue_credential(call)

    assert call.agent(ASK_ID) is True, (
        "the customer-ID question's own audio was suppressed because the "
        "backend already recorded that the cue had been sent"
    )
    assert call.heard(ASK_ID) == 1


def test_the_pin_question_survives_its_own_mark():
    call = Heard("own-pin")
    reach_pin_stage(call)
    cue_credential(call)

    assert call.agent(ASK_PIN) is True, "the PIN question's own audio was suppressed"
    assert call.heard(ASK_PIN) == 1


def test_the_clarification_question_survives_its_own_mark():
    call = Heard("own-clar")
    verified(call)
    cue_clarification(call)

    assert call.agent(CLARIFY) is True, "the clarification's own audio was suppressed"
    assert call.heard(CLARIFY) == 1


# === §2 a cue was sent and no audio ever came ==============================
#
# The response ends without producing a caller-audible word. Nothing was heard,
# so nothing is owned - and the backend believes it has asked. There must be a
# bounded way back, and it must not be an immediate unbounded re-ask.


def ended_generation(call):
    """`audio_end`, executed rather than merely emitted.

    The bridge defers `_generation_finished` through `self._schedule(...)`,
    which needs a running event loop - so in a synchronous test the coroutine is
    never awaited and the recovery path never runs. An earlier version of these
    three tests called `Heard.generation_ended()` directly and failed for that
    reason alone, while the production logic was correct: invoking
    `conversation.question_delivery_failed()` by hand reopened the question
    immediately, with no loop involved.

    So drive it the way the rest of this suite's async tests do
    (`tests/test_telephony_call_journey.py`), and prove the whole chain:

        audio_end -> _schedule -> _generation_finished
                  -> conversation.question_delivery_failed
    """

    async def scenario():
        call.bridge.on_realtime_event(
            call.sid, Event("audio_end")
        )
        # Let the scheduled task actually run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_a_credential_cue_with_no_audio_can_be_attempted_again():
    call = Heard("noaudio-id")
    call.caller("I want to know my account balance.")
    cue_credential(call)

    # The model produced nothing at all, and the turn ended.
    ended_generation(call)

    assert call.phase is None, "an owner was taken although nothing was heard"

    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) is not None, (
        "the caller heard no question and the bank will never ask again: "
        "the cue was marked as asked and only a completed caller turn clears "
        "it, so a caller who was never asked anything waits for ever"
    )


def test_a_pin_cue_with_no_audio_can_be_attempted_again():
    call = Heard("noaudio-pin")
    reach_pin_stage(call)
    cue_credential(call)
    ended_generation(call)

    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) is not None, (
        "the PIN question was never heard and cannot be asked again"
    )


def test_a_clarification_cue_with_no_audio_can_be_attempted_again():
    call = Heard("noaudio-clar")
    verified(call)
    cue_clarification(call)
    ended_generation(call)

    session = session_manager.get_session(call.sid)
    assert pending_clarification.awaiting_question(session) is not None, (
        "the clarification was never heard and cannot be asked again"
    )


def test_the_recovery_decision_itself_needs_no_event_loop():
    """The unit-level half, kept beside the three above deliberately.

    Those three prove the chain the bridge actually runs. This one proves the
    decision in isolation, so that a future scheduling change cannot make them
    pass by never reaching the logic at all - which is precisely how they failed
    the first time.
    """
    call = Heard("noaudio-unit")
    call.caller("I want to know my account balance.")
    cue_credential(call)

    assert call.bridge.conversation.outstanding_question() == "CUSTOMER_ID"
    assert call.bridge.conversation.delivered_question_kind is None

    assert call.bridge.conversation.question_delivery_failed() == "CUSTOMER_ID"

    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) == "CUSTOMER_ID"


def test_a_delivered_question_is_not_revoked_when_the_turn_ends():
    """The guard against over-eager recovery.

    Every model turn ends, and the great majority are nothing to do with a
    question. A turn that *did* deliver one must stay owned: revoking it would
    re-ask a question the caller is in the middle of answering, which is the
    original defect of this whole phase.
    """
    call = Heard("delivered-kept")
    call.caller("I want to know my account balance.")
    cue_credential(call)
    call.agent(ASK_ID)
    assert call.phase == "CUSTOMER_ID"

    ended_generation(call)

    assert call.phase == "CUSTOMER_ID", "a delivered question lost its owner"
    session = session_manager.get_session(call.sid)
    assert pending_credential.awaiting_question(session) is None, (
        "a question the caller actually heard became owed again"
    )


def test_recovery_is_bounded_not_an_immediate_loop():
    """A retry must be possible, and must not be unlimited.

    The bank asking once more because nothing was delivered is recovery. The
    bank asking on every model event is a machine talking over a caller, which
    is the failure this whole phase exists to stop.
    """
    call = Heard("bounded")
    call.caller("I want to know my account balance.")

    heard = 0
    for _ in range(6):
        session = session_manager.get_session(call.sid)
        if pending_credential.awaiting_question(session) is not None:
            cue_credential(call)
            if call.agent(ASK_ID, response=f"r{_}", item=f"i{_}"):
                heard += 1
        call.generation_ended()

    assert heard >= 1, "the question was never delivered at all"
    assert heard <= 2, (
        f"the caller was asked {heard} times without ever answering: recovery "
        "must be bounded"
    )


# === §3 the response was interrupted before any question reached the line ===


def test_an_interrupted_credential_response_does_not_leave_ownership_armed():
    call = Heard("interrupt-id")
    call.caller("I want to know my account balance.")
    cue_credential(call)

    # Generation starts and is then abandoned before a word reaches the caller.
    call.barge_in()

    assert call.phase is None, (
        "delivery ownership stayed armed after an interrupted response, so the "
        "next legitimate question would be withheld"
    )
    assert call.agent(ASK_ID) is True, "the retry of the question was withheld"
    assert call.heard(ASK_ID) == 1


def test_an_interrupted_pin_response_does_not_leave_ownership_armed():
    call = Heard("interrupt-pin")
    reach_pin_stage(call)
    cue_credential(call)
    call.barge_in()

    assert call.phase is None
    assert call.agent(ASK_PIN) is True, "the PIN question could not be retried"


def test_an_interrupted_clarification_response_does_not_leave_ownership_armed():
    call = Heard("interrupt-clar")
    verified(call)
    cue_clarification(call)
    call.barge_in()

    assert call.phase is None
    assert call.agent(CLARIFY) is True, "the clarification could not be retried"


def test_an_interruption_after_delivery_still_releases_ownership():
    """Barge-in once the question *has* been heard.

    The caller talked over it, which means they are answering - the one event
    that legitimately releases the wait. What must not happen is the owner
    surviving an abandoned response for ever.
    """
    call = Heard("interrupt-after")
    call.caller("I want to know my account balance.")
    cue_credential(call)
    call.agent(ASK_ID)
    assert call.phase == "CUSTOMER_ID"

    call.barge_in()

    assert call.phase is None, "an abandoned response kept the question owned"


# === §5 one response, a preface item and then the question item =============
#
# The live defect this phase started from proved that a single response can
# carry two assistant items - `56f012b6-2b4e-…` asked for the customer ID twice
# from one response. So the reverse shape is equally possible: item 1 a
# harmless preface, item 2 the actual question.
#
# Phase 7.4D deliberately drops the second output item of a response, because
# that is how it stopped the duplicate. If the model packs preface-then-question
# into one response, 7.4D drops the question and the caller hears only the
# preface.


# --- the ambiguity, now resolved by ownership -------------------------------
#
# These three used to assert a limitation, and they no longer can, because the
# shape they described is no longer the path production takes.
#
# The old failure: the bank *cued* the model and then had to work out which of
# its utterances had been the question. Two shapes were indistinguishable at
# the media boundary -
#
#     one response, items [preface,  question]   item 2 must be heard
#     one response, items [question, question]   item 2 must be dropped
#
# - because in both a question was outstanding, backend state was identical, and
# only the words differed. Comparing words is the text matching this programme
# has refused at every phase.
#
# Phase 7.4F removes the guesswork instead of resolving it. The backend issues
# its own `response.create` for each of the three state-machine questions,
# carrying a correlation token, and learns the authoritative `response.id` from
# `response.created`. A preface can then only arrive on some *other* response -
# and any other response is refused while the owned question is outstanding. The
# question is identified, never inferred.
#
# What has NOT changed, and must not: a second assistant item of one response is
# still dropped. That is live call `56f012b6-2b4e-1240-4790-eaa5afddeeef` - the
# same request twice, six milliseconds apart, the second landing over the caller
# mid-answer - and `test_two_question_items_in_one_response_are_still_only_asked_once`
# below keeps it. With `tool_choice: "none"` and `tools: []` the installed API
# contract says an owned response carries one assistant item, so the two rules
# no longer compete; if the provider ever breaks that contract,
# `owned_question_contract_violations` records it rather than anything silently
# changing meaning.


def test_a_foreign_preface_cannot_take_the_owned_id_question():
    """CASE A, resolved. The caller hears the question, not just a preface."""
    call = Heard("owned-id")
    call.caller("I want to know my account balance.")
    owned = own_credential_question(call, "CUSTOMER_ID")

    # The model's own automatic response says something harmless first.
    assert call.agent(PREFACE, response="auto-1", item="a1") is False, (
        "a foreign response reached the caller while the bank's own question "
        "was outstanding"
    )
    assert call.agent(ASK_ID, response=owned, item="q1") is True, (
        "the bank's own question response was withheld"
    )

    assert call.heard(ASK_ID) == 1
    assert call.heard(PREFACE) == 0


def test_a_foreign_preface_cannot_take_the_owned_pin_question():
    """CASE A, PIN side."""
    call = Heard("owned-pin")
    reach_pin_stage(call)
    owned = own_credential_question(call, "PIN")

    assert call.agent(PREFACE, response="auto-2", item="a1") is False
    assert call.agent(ASK_PIN, response=owned, item="q1") is True
    assert call.heard(ASK_PIN) == 1


def test_a_foreign_preface_cannot_take_the_owned_clarification():
    """CASE A, clarification side."""
    call = Heard("owned-clar")
    verified(call)
    owned = own_clarification_question(call)

    assert call.agent(PREFACE, response="auto-3", item="a1") is False
    assert call.agent(CLARIFY, response=owned, item="q1") is True
    assert call.heard(CLARIFY) == 1


def test_a_foreign_response_cannot_ask_again_after_the_owned_question():
    """CASE D. A second question from elsewhere is refused, not heard."""
    call = Heard("owned-dup")
    call.caller("I want to know my account balance.")
    owned = own_credential_question(call, "CUSTOMER_ID")

    assert call.agent(ASK_ID, response=owned, item="q1") is True
    call.generation_ended()

    # The SDK creates its own response after a tool result. It may exist; it may
    # not reach the caller as a competing question.
    call.tool("get_authentication_status")
    assert call.agent(ASK_ID, response="auto-tool", item="t1") is False

    assert call.heard(ASK_ID) == 1


def test_a_second_item_of_the_owned_response_is_a_contract_violation():
    """CASE J. Refused, counted, and Phase 7.4D left exactly as it was."""
    call = Heard("owned-contract")
    call.caller("I want to know my account balance.")
    owned = own_credential_question(call, "CUSTOMER_ID")

    assert call.agent(ASK_ID, response=owned, item="q1") is True
    assert call.agent(ASK_ID, response=owned, item="q2") is False, (
        "a second assistant item of the owned response reached the caller; "
        "Phase 7.4D must still refuse it"
    )

    assert call.heard(ASK_ID) == 1
    assert call.bridge.conversation.owned_question_contract_violations == 1, (
        "the API-contract violation was not recorded"
    )


def test_two_question_items_in_one_response_are_still_only_asked_once():
    """The live defect itself must stay fixed whatever §5 requires.

    `56f012b6-2b4e-…`: one response, two items, the same request twice, six
    milliseconds apart, the second arriving over the caller mid-answer. Any
    change that lets a second item through for the preface case must not let
    this one through.
    """
    call = Heard("multi-dup")
    call.caller("I want to know my account balance.")
    cue_credential(call)

    call.agent(ASK_ID, response="r1", item="i1")
    call.agent(ASK_ID, response="r1", item="i2")

    assert call.heard(ASK_ID) == 1, "the caller was asked twice from one response"
