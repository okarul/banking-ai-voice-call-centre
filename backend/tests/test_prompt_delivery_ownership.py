"""Phase 7.4E residual gaps: what "the question was asked" actually means.

Two production-behaviour gaps survived the first 7.4E pass. They are opposite
failures of the same missing distinction, which is why they belong in one file.

**Gap 1 - the bank speaking is not the bank asking.**

`PhoneCallBridge` marks a credential prompt as delivered whenever audio reaches
the line while a credential is awaited and a protected enquiry is held. It has
no way to tell

    "Certainly, I can help you with that."      <- a harmless preface
    "May I have your demo customer ID, please?" <- the actual question

apart, because nothing in the backend records which caller-facing utterance was
the credential request. Unlike the clarification question - which
`app.pending_clarification` genuinely owns through `question_asked` /
`mark_question_asked` / `awaiting_question` - the credential question is
volunteered by the model, and `speech.AUTHENTICATION_REQUEST` and
`speech.PIN_REQUEST` are used only in `SPEECH_BY_REASON` for the browser
channel's refusal wording. Channel 2 never delivers them.

So a preface arms the gate, and the *real* question is then withheld. That is
over-suppression: the caller is asked nothing at all and sits waiting, which is
a worse failure than the duplicate Phase 7.4E set out to fix.

**Gap 2 - the clarification question can still be spoken twice.**

Found by the expanded FLOW 10 work. Once the caller is verified,
`awaiting_credential` is None, so the 7.4E gate cannot arm, and

    clarification question -> audio_end -> tool result -> clarification again

reaches the caller twice - the exact shape of live call
`ca88679d-2c32-1240-4790-eaa5afddeeef`, one invariant over.

**Neither is solved with text.** Nothing here compares or matches utterances.
The fix both gaps require is the same one: the backend must own the fact that a
particular question has been *put to the caller*, and that ownership must name
the response and item that delivered it - because
`pending_clarification.mark_question_asked` fires when the cue is *sent*, before
the model has generated a word, so a gate that merely asked "has it been marked"
would suppress the question's own audio.
"""

import pytest

from app import pending_clarification
from app.auth.authentication import submit_customer_id, submit_pin
from app.sessions import session_manager
from tests.test_auth_turn_ownership import (
    ANSWER,
    ASK_ID,
    ASK_PIN,
    CLARIFY,
    CUSTOMER,
    Heard,
    REAL_PIN,
)

# Harmless things a bank says that are not questions. Never matched on, never
# compared - they are here only so the tests speak something that is plainly
# not a credential request.
PREFACE_ID = "Certainly, I can help you with that."
PREFACE_PIN = "Thank you. One moment while I bring up your record."
SECOND_ANSWER = "Your current account balance is 3,820 dollars and 10 cents."


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


def open_clarification(call):
    """A real clarification, opened the way the tool layer opens one."""
    session = session_manager.get_session(call.sid)
    pending_clarification.open_for(
        session,
        tool="get_account_balance",
        reason=pending_clarification.ACCOUNT_TYPE_REQUIRED,
        choices=("Savings", "Current"),
        manager=session_manager,
    )
    return session


def authenticate(call):
    """Take one call all the way through verification."""
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    call.agent(ASK_PIN)
    call.generation_ended()
    call.caller("my pin is four eight two one")
    result = submit_pin(call.sid, REAL_PIN)
    assert result["authenticated"] is True
    call.generation_ended()
    return call


# === §1 FLOW A - a harmless preface must not consume the ID question ========


def test_flow_a_a_preface_does_not_consume_the_customer_id_question():
    """The bank may clear its throat before it asks.

    Both utterances are legitimate and both must be heard. If the preface marks
    the question as delivered, the caller is never actually asked for anything
    and the call dies in silence.
    """
    call = Heard("pref-a")
    call.caller("I want to know my account balance.")

    assert call.agent(PREFACE_ID) is True, "the acknowledgement was suppressed"
    call.generation_ended()

    assert call.agent(ASK_ID) is True, (
        "the real customer-ID question was withheld because a harmless "
        "acknowledgement had already been counted as the question"
    )

    assert call.heard(PREFACE_ID) == 1
    assert call.heard(ASK_ID) == 1


def test_flow_a_the_id_question_is_still_asked_only_once_after_a_preface():
    """And the duplicate protection must survive the preface being allowed."""
    call = Heard("pref-a2")
    call.caller("I want to know my account balance.")
    call.agent(PREFACE_ID)
    call.generation_ended()
    call.agent(ASK_ID)
    call.generation_ended()

    call.tool("get_authentication_status")
    call.agent(ASK_ID)

    assert call.heard(ASK_ID) == 1, "the question was repeated after a tool result"


# === §1 FLOW B - the same, for the PIN =====================================


def test_flow_b_a_preface_does_not_consume_the_pin_question():
    call = Heard("pref-b")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)

    assert call.agent(PREFACE_PIN) is True, "the transition was suppressed"
    call.generation_ended()

    assert call.agent(ASK_PIN) is True, (
        "the real PIN question was withheld because a harmless transition had "
        "already been counted as the question"
    )

    assert call.heard(PREFACE_PIN) == 1
    assert call.heard(ASK_PIN) == 1


def test_flow_b_the_pin_question_is_still_asked_only_once_after_a_preface():
    call = Heard("pref-b2")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    call.agent(PREFACE_PIN)
    call.generation_ended()
    call.agent(ASK_PIN)
    call.generation_ended()

    call.tool("get_authentication_status")
    call.agent(ASK_PIN)

    assert call.heard(ASK_PIN) == 1


# === §2 the clarification question must be put exactly once ================


def test_the_clarification_question_survives_being_delivered():
    """The question's own audio must reach the caller.

    `mark_question_asked` fires when the cue is *sent*, before the model has
    generated anything, so any guard keyed on it must still let the delivery
    through. This test exists to stop the fix for the duplicate becoming a
    silence.
    """
    call = Heard("clar-first")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    assert call.agent(CLARIFY) is True, "the clarification question was suppressed"
    assert call.heard(CLARIFY) == 1


def test_a_tool_continuation_does_not_repeat_the_clarification_question():
    """The FLOW 10 defect, as its own regression.

    generic balance -> verified -> ACCOUNT_TYPE_REQUIRED -> question once ->
    audio_end -> tool/model continuation -> the question does NOT play again.
    """
    call = Heard("clar-dup")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY)
    call.generation_ended()
    call.tool("get_account_balance")
    call.agent(CLARIFY)

    assert call.heard(CLARIFY) == 1, (
        "the caller was asked to choose twice with no turn of their own in "
        "between"
    )


def test_a_duplicate_response_does_not_repeat_the_clarification_question():
    call = Heard("clar-dup2")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY, response="c1", item="ci1")
    call.generation_ended()
    call.agent(CLARIFY, response="c2", item="ci2")

    assert call.heard(CLARIFY) == 1


def test_a_second_output_item_does_not_repeat_the_clarification_question():
    call = Heard("clar-dup3")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY, response="c1", item="ci1")
    call.agent(CLARIFY, response="c1", item="ci2")

    assert call.heard(CLARIFY) == 1


def test_a_delayed_callback_does_not_repeat_the_clarification_question():
    call = Heard("clar-dup4")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY, response="c1", item="ci1")
    call.generation_ended()
    call.tool("get_account_balance")
    call.tool("get_account_balance")
    call.agent(CLARIFY, response="c-late", item="ci-late")

    assert call.heard(CLARIFY) == 1


def test_the_full_clarified_flow_plays_each_thing_once():
    """The complete §2 regression, end to end, counted at the caller."""
    call = Heard("clar-flow")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY)
    call.generation_ended()
    call.tool("get_account_balance")
    call.agent(CLARIFY)                     # must not reach the caller

    call.caller("Savings")
    pending_clarification.complete_with(
        session_manager.get_session(call.sid),
        pending_clarification.Domain.ACCOUNT,
        "Savings",
        manager=session_manager,
    )

    assert call.agent(ANSWER) is True, "the balance answer was suppressed"

    assert call.heard(CLARIFY) == 1
    assert call.heard(ANSWER) == 1


# === §3 a completed caller turn releases every one of the three waits ======


def test_a_caller_turn_releases_the_clarification_wait():
    """And the next legitimate answer is allowed."""
    call = Heard("clar-release")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)

    call.agent(CLARIFY)
    call.generation_ended()

    call.caller("Savings")
    pending_clarification.complete_with(
        session_manager.get_session(call.sid),
        pending_clarification.Domain.ACCOUNT,
        "Savings",
        manager=session_manager,
    )

    assert call.agent(ANSWER) is True
    assert call.heard(ANSWER) == 1


def test_the_clarification_wait_is_released_by_history_added():
    call = Heard("clar-rel-added")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)
    call.agent(CLARIFY)
    call.generation_ended()

    call.caller_via_history_added("Savings")
    pending_clarification.complete_with(
        session_manager.get_session(call.sid),
        pending_clarification.Domain.ACCOUNT,
        "Savings",
        manager=session_manager,
    )

    assert call.agent(ANSWER) is True, "a history_added answer did not release"


def test_the_clarification_wait_is_released_by_history_updated():
    call = Heard("clar-rel-updated")
    authenticate(call)
    session = open_clarification(call)
    pending_clarification.mark_question_asked(session, manager=session_manager)
    call.agent(CLARIFY)
    call.generation_ended()

    call.caller_via_history_updated("Savings")
    pending_clarification.complete_with(
        session_manager.get_session(call.sid),
        pending_clarification.Domain.ACCOUNT,
        "Savings",
        manager=session_manager,
    )

    assert call.agent(ANSWER) is True, "a history_updated answer did not release"


# === §4 the preface cases added to the over-suppression matrix =============


def test_every_legitimate_utterance_including_prefaces_is_heard():
    """One call, walked through with a preface before each credential."""
    call = Heard("pref-walk")

    call.caller("I want to know my account balance.")
    assert call.agent(PREFACE_ID) is True, "pre-auth preface"
    call.generation_ended()
    assert call.agent(ASK_ID) is True, "first customer-ID question"
    call.generation_ended()

    call.caller("sorry, I don't have it")
    assert call.agent(ASK_ID) is True, "authorised invalid-ID retry"
    call.generation_ended()

    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    assert call.agent(PREFACE_PIN) is True, "pre-PIN preface"
    call.generation_ended()
    assert call.agent(ASK_PIN) is True, "first PIN question"
    call.generation_ended()

    call.caller("my pin is four eight two one")
    submit_pin(call.sid, REAL_PIN)
    assert call.agent(ANSWER) is True, "business answer"

    assert call.heard(ASK_ID) == 2, "one question, one authorised retry"
    assert call.heard(ASK_PIN) == 1
    assert call.heard(PREFACE_ID) == 1
    assert call.heard(PREFACE_PIN) == 1


# === a closing call must still be able to say goodbye ======================
#
# The gap this phase's own fix opened, and the correction to the test that
# first chased it.
#
# Owning the delivered question means a new response is refused until the
# caller answers. A caller who is asked for their demo customer ID and then
# says nothing never answers - so the silence path asked the model for the
# closing line and the boundary withheld it, and the bank signed off
# inaudibly. `lifecycle._wait_for_closing` still ended the call on its bounded
# deadline, logging "closing without a final line", so the line was held open
# rather than lost for ever; the caller simply heard nothing.
#
# The first attempt at this test was wrong in the same way the original bug
# was wrong: it judged by the words. It spoke a sentence that *read like* a
# farewell while the call was not closing and asserted it should be heard -
# but that is exactly the duplicate live call
# `ca88679d-2c32-1240-4790-eaa5afddeeef` was reported for, and refusing it
# there is correct. The two cases are separated by state, never by wording,
# which is why `test_a_call_that_is_not_closing_still_refuses_a_repeat` sits
# immediately below them.

SILENCE_LINE = "I do not hear anything from you. Thank you."


def test_a_closing_call_still_speaks_to_a_caller_who_was_asked_for_their_id():
    """The bank asked, the caller went quiet, and the call is now closing.

    No caller turn ever completes, so the owner taken by the customer-ID
    question is still held. The call is closing anyway - waiting for an answer
    has stopped being the thing this call is doing - and the caller must still
    hear the bank sign off.
    """
    call = Heard("closing-id")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()

    assert call.phase == "CUSTOMER_ID", "the question was not owned as expected"

    # What the silence path does before it speaks: the call has decided to end.
    call.bridge.conversation.closing = True

    assert call.agent(SILENCE_LINE) is True, (
        "a closing call withheld its last words because of a question the "
        "caller was never going to answer"
    )
    assert call.heard(SILENCE_LINE) == 1


def test_a_closing_call_still_speaks_to_a_caller_who_was_asked_for_their_pin():
    """The same, one credential further on."""
    call = Heard("closing-pin")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.caller("my customer id is DEMO001")
    submit_customer_id(call.sid, CUSTOMER)
    call.agent(ASK_PIN)
    call.generation_ended()

    assert call.phase == "PIN"

    call.bridge.conversation.closing = True

    assert call.agent(SILENCE_LINE) is True, (
        "a closing call withheld its last words from a caller asked for a PIN"
    )
    assert call.heard(SILENCE_LINE) == 1


def test_a_call_that_is_not_closing_still_refuses_a_repeat():
    """The guard this phase exists for, and the reason the first probe was wrong.

    Identical to the tests above except that the call is *not* closing. Here the
    new response must be refused, however the sentence happens to read. Kept
    beside them so that any fix for the closing case has to distinguish the two
    by state rather than by wording - which is the whole argument of
    `app.pending_credential`.
    """
    call = Heard("not-closing")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()

    assert call.agent(SILENCE_LINE) is False, (
        "a new response reached a caller who had been asked a question and had "
        "not answered it"
    )
    assert call.heard(ASK_ID) == 1


def test_a_repeat_of_the_question_is_still_refused_while_the_caller_is_silent():
    """The duplicate from the live call must stay suppressed."""
    call = Heard("silent-still-guarded")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()

    call.tool("get_authentication_status")
    call.agent(ASK_ID)

    assert call.heard(ASK_ID) == 1, "the repeat reached a caller who had not answered"
