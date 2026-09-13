"""Phase 7.4B Stage 1: the enquiry a caller finished across two turns.

Fail-before-fix evidence. No production code is changed by this commit.

Phase 7.3 proved that an enquiry made *before* verification is answered after
it - `pending_request` holds it, and `submit_pin` hands the answer back with the
verification. This file is about the other way a caller finishes a sentence:
across a clarification.

    "What is my account balance?"     -> which account? Savings or Current
    "Savings"                         -> ...

At that moment the backend holds everything it needs. The caller is verified (or
about to be), the turn was classified in scope, the enquiry is
`get_account_balance`, and `parse_type_reply("Savings", ACCOUNT)` returns the
canonical value `"Savings"`. Nothing is missing and nothing is ambiguous.

It is thrown away. `app/scope.py:476` calls the parser, uses the result as a
truthiness test, and returns a bare `Domain` - so the ruling for the slot turn
carries `intent=UNKNOWN` and `account_type=None`. Nothing else holds it either:
`pending_request.remember` and `turn_gate._hold_unverified_enquiry` both begin
`if session.authenticated: return`, and `remember_context` - the only writer of
`account_type` to the session - runs only when a tool *succeeds*, which a
clarification by definition did not.

What is left is a system whose answer depends on whether the model happens to
repeat a word the caller already said. `test_..._when_the_model_repeats_the_slot`
passes today. `test_..._from_server_state` does not, and they differ in nothing
but that.

The strongest case here is AB-008, because no model is involved in it at all:
the caller says "What is my account balance?", then "Savings", then verifies,
and the *backend's own deterministic resume* - the Phase 7.3 mechanism, entirely
server-owned - hands back `ACCOUNT_TYPE_REQUIRED`. A verified caller who has
already said which account is told the bank needs to know which account.

Every test here drives the real orchestration path used by both channels:

    record_turn(...)  ->  execute_tool(...)  ->  @function_tool
                                              ->  _check_scope
                                              ->  _carried
                                              ->  _dispatch
                                              ->  registered banking tool
                                              ->  PostgreSQL

Fixture facts these tests depend on, read from the seeded database and not
changed by anything here:

    DEMO001  pin 4821  Savings 12450.75, Current 3820.10   one loan
    DEMO003  pin 2648  Savings, Current                     Car Loan 46200.00,
                                                            Home Loan

DEMO001 has two accounts, so an account clarification is genuine. It has only
one loan, which resolves without asking - so the loan cases use DEMO003, which
has two.
"""

import asyncio

import pytest

from app import pending_request
from app.agents.intents import Domain, Intent, parse_type_reply
from app.realtime.turn_gate import record_turn
from app.realtime.webrtc import execute_tool
from app.scope import classify_scope
from app.sessions import SessionManager, session_manager

PINS = {"DEMO001": "4821", "DEMO003": "2648"}

# Seeded values. Asserted rather than recomputed, so a test that starts passing
# because the fixture moved is a test that fails.
DEMO001_SAVINGS = "12450.75"
DEMO001_CURRENT = "3820.10"
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
    """Counts what actually reached the banking tools, per session.

    Measured at `_run_tool`, the one door every registered tool goes through.
    These tests drive the tool layer directly and claim no phone call, so there
    is no `agent_tool_events` row to count; the real tool still runs and this
    only watches. Same seam as `test_held_enquiry_resume.runs`.
    """
    import app.realtime.tools as tools_module

    original = tools_module._run_tool
    seen: list[tuple[str, str, bool]] = []

    def counting(tool_name, session_id, arguments, manager):
        result = original(tool_name, session_id, arguments, manager)
        answered = isinstance(result, dict) and bool(result.get("success"))
        seen.append((session_id, tool_name, answered))
        return result

    monkeypatch.setattr(tools_module, "_run_tool", counting)

    class Counter:
        def of(self, session_id, tool_name):
            """Every time this tool was entered, answered or not."""
            return sum(
                1 for sid, name, _ in seen if sid == session_id and name == tool_name
            )

        def answered(self, session_id, tool_name):
            """Only the times it came back with the customer's money.

            The distinction matters for the clarification cases. Asking "which
            account?" *is* a trip into the tool - it has to look at what the
            customer holds before it can name the choices - but it reads no
            balance and hands the caller nothing. What C10 and C11 require is
            that nothing is *answered* while the question is still open, and in
            particular that "both" never answers twice.
            """
            return sum(
                1
                for sid, name, ok in seen
                if sid == session_id and name == tool_name and ok
            )

    return Counter()


# --- the conversation, driven the way production drives it -------------------


def says(manager, session_id, words):
    """One caller turn: classify it, exactly as the realtime pump does."""
    return record_turn(manager.get_session(session_id), words)


def tool(manager, session_id, name, arguments=None):
    """One model tool call, through the real orchestration entry point."""
    return run(execute_tool(name, session_id, arguments or {}, manager=manager))


def verified(manager, customer_id):
    """A session taken through the real deterministic authentication."""
    session = manager.create_session()
    tool(
        manager,
        session.session_id,
        "submit_customer_id",
        {"spoken_customer_id": customer_id},
    )
    result = tool(
        manager, session.session_id, "submit_pin", {"spoken_pin": PINS[customer_id]}
    )
    assert result["success"] is True, "the harness failed to authenticate"
    return session.session_id


def asked_which_account(result):
    """Whether the bank has just asked the caller which account they mean."""
    return result.get("reason") == "ACCOUNT_TYPE_REQUIRED"


def asked_which_loan(result):
    return result.get("reason") == "LOAN_TYPE_REQUIRED"


# === the first divergence, pinned exactly ===================================


def test_a_slot_answer_carries_its_value_but_no_authority_of_its_own():
    """The divergence anchor, and the shape of the correction.

    Stage 1 asserted this differently, and the difference is the architecture
    rather than a detail. The original assertion required the ruling for a bare
    "Savings" to carry `intent=ACCOUNT_BALANCE` - the enquiry it completes.
    That would make a single spoken word name a banking tool on its own, which
    is precisely the authority a slot answer must **not** have: said out of the
    blue it would then arrive at the gate looking like a balance enquiry.

    So the value travels and the authority does not. The ruling reports what
    the caller chose; the enquiry it belongs to comes from the clarification
    this bank itself opened, on this session, in this domain. With nothing
    outstanding the word completes nothing and reaches nothing.

    The defect Stage 1 found is unchanged and is asserted here as it always
    was: the parser resolves the answer, and before Phase 7.4B the ruling threw
    it away one line later.
    """
    # What the deterministic parser makes of the caller's answer.
    assert parse_type_reply("Savings", Domain.ACCOUNT) == "Savings"

    ruling = classify_scope(
        "Savings",
        authenticated=True,
        customer_id="DEMO001",
        current_domain="ACCOUNT",
    )

    assert ruling.allowed is True, "the slot turn is in scope, and that is right"
    assert ruling.account_type == "Savings", (
        "the ruling for a slot answer must carry the canonical value the parser "
        "already produced, or nothing downstream can use it"
    )
    assert ruling.intent is Intent.UNKNOWN, (
        "a bare slot answer must name no enquiry by itself; the enquiry comes "
        "from the clarification the bank opened"
    )
    assert ruling.domain is Domain.ACCOUNT


# === AB-002: verified caller, ambiguous request, then the slot ===============


def test_ab002_clarified_balance_is_answered_when_the_model_repeats_the_slot(
    manager, executions
):
    """AB-002 control. Passes today, and is the reason the defect is invisible.

    Identical to the test below in every respect except that the model repeats
    the word the caller already said. That one difference decides whether the
    caller is answered, which is precisely what must stop being true.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    first = tool(manager, session_id, "get_account_balance", {})
    assert asked_which_account(first), "the bank should ask which account"

    says(manager, session_id, "Savings")
    answer = tool(manager, session_id, "get_account_balance", {"account_type": "Savings"})

    assert answer["success"] is True
    assert answer["available_balance"] == DEMO001_SAVINGS
    assert executions.of(session_id, "get_account_balance") == 2


def test_ab002_clarified_balance_is_answered_from_server_state(manager, executions):
    """AB-002. The caller said which account. The bank must not ask again.

    The model omits `account_type` on the follow-up, which is the reasonable
    thing for it to do and what `_carried` exists to absorb - its own docstring
    says so: "The server heard them; they do not need to be asked twice."

    On this commit the server did not keep what it heard, so the caller is asked
    the identical question a second time and no balance is ever produced. That
    is the caller-visible failure: a verified customer who has answered the
    bank's own clarifying question is asked it again.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    first = tool(manager, session_id, "get_account_balance", {})
    assert asked_which_account(first), "the bank should ask which account"

    # The caller answers. This is the whole of what they are required to do.
    says(manager, session_id, "Savings")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert not asked_which_account(answer), (
        "the bank asked which account twice, having already been told 'Savings'"
    )
    assert answer["success"] is True, (
        f"the completed enquiry did not execute: {answer}"
    )
    assert answer["available_balance"] == DEMO001_SAVINGS
    assert answer["account_type"] == "Savings"


def test_ab003_the_slot_answer_current_selects_the_current_account(manager):
    """AB-003. The same flow, and it must not quietly resolve to the default."""
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    assert asked_which_account(tool(manager, session_id, "get_account_balance", {}))

    says(manager, session_id, "Current")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer["success"] is True, (
        f"the completed enquiry did not execute: {answer}"
    )
    assert answer["account_type"] == "Current"
    assert answer["available_balance"] == DEMO001_CURRENT


def test_ab007_answering_the_slot_twice_reads_the_account_once(manager, executions):
    """AB-007. A caller who repeats themselves is not two enquiries.

    Runs after the flow above works, and is here now so that the fix is held to
    exactly-once from the start rather than having it retrofitted.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})
    before = executions.of(session_id, "get_account_balance")

    says(manager, session_id, "Savings")
    first = tool(manager, session_id, "get_account_balance", {})
    says(manager, session_id, "Savings")
    second = tool(manager, session_id, "get_account_balance", {})

    assert first["success"] is True, f"the completed enquiry did not execute: {first}"
    assert second["success"] is True
    assert second["available_balance"] == first["available_balance"]
    assert executions.of(session_id, "get_account_balance") - before == 1, (
        "the same completed enquiry was read from the bank twice"
    )


# === AB-008: the slot arrives before verification ===========================


def test_ab008_a_clarified_enquiry_survives_authentication(manager):
    """AB-008. The strongest case here, because no model is involved.

    The caller says what they want and which account, and only then verifies.
    Answering them is entirely the backend's own job from that point: Phase 7.3
    made `submit_pin` carry the answer back in `pending_result`, deterministically,
    with no tool call from the model at all.

    On this commit the hold records `account_type=None` - the ambiguous turn had
    none - and the "Savings" turn that follows is ruled `intent=UNKNOWN`, so it
    never reaches the hold. The resume then runs the enquiry with no account and
    the bank tells its own verified customer that it needs to know which account,
    having been told twice.
    """
    session_id = manager.create_session().session_id

    says(manager, session_id, "What is my account balance?")
    held = pending_request.recall(manager.get_session(session_id))
    assert held is not None and held.tool == "get_account_balance"

    # The caller answers the clarification before identifying themselves.
    says(manager, session_id, "Savings")

    held = pending_request.recall(manager.get_session(session_id))
    assert held.account_type == "Savings", (
        "the held enquiry did not record the account the caller named; it will "
        "be resumed with no account at all"
    )

    tool(manager, session_id, "submit_customer_id", {"spoken_customer_id": "DEMO001"})
    verification = tool(manager, session_id, "submit_pin", {"spoken_pin": "4821"})

    assert verification["success"] is True
    resumed = verification.get("pending_result")
    assert resumed is not None, "nothing in this system answered the held enquiry"
    assert resumed["success"] is True, (
        f"the backend resumed the enquiry and failed it: {resumed}"
    )
    assert resumed["available_balance"] == DEMO001_SAVINGS


def test_ab009_the_slot_arrives_after_verification(manager):
    """AB-009. Ambiguous before verifying, answered after.

    The hold is created with no account, verification resumes it and correctly
    asks which one, and then the caller says. From there this is AB-002.
    """
    session_id = manager.create_session().session_id

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "submit_customer_id", {"spoken_customer_id": "DEMO001"})
    verification = tool(manager, session_id, "submit_pin", {"spoken_pin": "4821"})

    assert verification["success"] is True
    # Asking here is correct: the caller genuinely had not said which account.
    assert asked_which_account(verification.get("pending_result") or {})

    says(manager, session_id, "Savings")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert not asked_which_account(answer), (
        "the bank asked which account twice, having already been told 'Savings'"
    )
    assert answer["success"] is True, (
        f"the completed enquiry did not execute: {answer}"
    )
    assert answer["available_balance"] == DEMO001_SAVINGS


# === the same defect on a loan, using a real supported loan tool =============


def test_a_clarified_loan_enquiry_completes_from_server_state(manager):
    """DEMO003 has two loans, so "Tell me about my loan" is genuinely ambiguous.

    Same shape, different domain and different tool, to show this is one defect
    in shared machinery rather than something specific to accounts.
    """
    session_id = verified(manager, "DEMO003")

    says(manager, session_id, "Tell me about my loan")
    first = tool(manager, session_id, "get_loan_details", {})
    assert asked_which_loan(first), "the bank should ask which loan"
    assert first["available_loan_types"] == ["Car Loan", "Home Loan"]

    says(manager, session_id, "Car Loan")
    answer = tool(manager, session_id, "get_loan_details", {})

    assert not asked_which_loan(answer), (
        "the bank asked which loan twice, having already been told 'Car Loan'"
    )
    assert answer["success"] is True, (
        f"the completed enquiry did not execute: {answer}"
    )
    assert answer["loan_type"] == "Car Loan"
    assert answer["outstanding_balance"] == DEMO003_CAR_LOAN


def test_an_invalid_slot_answer_then_a_valid_one_recovers(manager):
    """AB-004 on the loan side. DEMO003 has no Personal Loan.

    A wrong answer must be refused without destroying the enquiry, and the
    correction must then complete it.
    """
    session_id = verified(manager, "DEMO003")

    says(manager, session_id, "Tell me about my loan")
    tool(manager, session_id, "get_loan_details", {})

    says(manager, session_id, "Personal Loan")
    wrong = tool(manager, session_id, "get_loan_details", {"loan_type": "Personal Loan"})
    assert wrong["success"] is False
    assert wrong["reason"] == "LOAN_NOT_FOUND"

    says(manager, session_id, "Car Loan")
    answer = tool(manager, session_id, "get_loan_details", {})

    assert answer["success"] is True, (
        f"the corrected enquiry did not execute: {answer}"
    )
    assert answer["loan_type"] == "Car Loan"


# === a correction before execution must win =================================


def test_ab005_a_correction_before_execution_selects_the_corrected_account(manager):
    """AB-005. "Savings" then "No, Current" before anything has been read.

    `parse_type_reply` already understands "no, Current"; the question is
    whether the enquiry that finally runs uses it.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})

    says(manager, session_id, "Savings")
    says(manager, session_id, "No, Current")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer["success"] is True, (
        f"the completed enquiry did not execute: {answer}"
    )
    assert answer["account_type"] == "Current", (
        "the correction was lost; the caller was given the account they "
        "corrected away from"
    )
    assert answer["available_balance"] == DEMO001_CURRENT


# === C10 / C11: answers that are not a choice but are not off-topic either ===
#
# Both are ruled NON_BANKING_REQUEST today, so the caller who has just been
# asked "Savings or Current?" answers it and is told:
#
#     "I'm here to help with your ABC Demo Bank account and loan enquiries.
#      How can I assist with your banking today?"
#
# The bank asked the question. Refusing the reply to it as off-topic is a
# caller-visible product failure, and it strands the enquiry as well.


def test_c10_answering_both_asks_the_caller_to_choose_one(manager, executions):
    """C10. "both" is an answer to the bank's question, not a new subject.

    The tool contract takes one `account_type`, so "both" cannot be executed as
    it stands - and must not be guessed at either. The required behaviour is to
    keep the enquiry and ask for one of the supported choices.

    Asserted here: not refused as off-topic, nothing read from the bank, and
    the options put to the caller again.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})
    answered_before = executions.answered(session_id, "get_account_balance")

    ruling = says(manager, session_id, "both")
    assert ruling.category.value != "NON_BANKING_REQUEST", (
        "the caller answered the bank's own clarifying question and was told "
        "the bank only handles banking enquiries"
    )

    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer.get("reason") != "OUT_OF_SCOPE", (
        f"the reply to the clarification was refused as off-topic: {answer}"
    )
    assert asked_which_account(answer), (
        "the bank should ask the caller to pick one of the two accounts"
    )
    assert answer["available_account_types"] == ["Savings", "Current"]
    assert executions.answered(session_id, "get_account_balance") == answered_before, (
        "'both' must not answer with an account, and must certainly not "
        "answer with two"
    )


def test_c10_a_choice_after_both_completes_the_same_enquiry(manager):
    """C10 recovery. The enquiry survives the answer that could not be used."""
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})
    says(manager, session_id, "both")
    tool(manager, session_id, "get_account_balance", {})

    says(manager, session_id, "Savings")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer["success"] is True, (
        f"the enquiry did not survive an unusable clarification answer: {answer}"
    )
    assert answer["account_type"] == "Savings"
    assert answer["available_balance"] == DEMO001_SAVINGS


def test_c11_asking_what_the_options_are_restates_them(manager, executions):
    """C11. A caller who did not catch the question asks for it again.

    This is the most ordinary thing a person does on a telephone, and today it
    ends the banking conversation.
    """
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})
    answered_before = executions.answered(session_id, "get_account_balance")

    ruling = says(manager, session_id, "what are my options")
    assert ruling.category.value != "NON_BANKING_REQUEST", (
        "asking which accounts there are, mid-clarification, was treated as an "
        "off-topic request"
    )

    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer.get("reason") != "OUT_OF_SCOPE", (
        f"the caller's request to hear the choices was refused: {answer}"
    )
    assert answer["available_account_types"] == ["Savings", "Current"], (
        "the caller asked what the options were and was not told"
    )
    assert executions.answered(session_id, "get_account_balance") == answered_before, (
        "listing the options must not hand the caller an account"
    )


def test_c11_a_choice_after_asking_the_options_completes_the_same_enquiry(manager):
    """C11 recovery. Same logical enquiry, not a new one."""
    session_id = verified(manager, "DEMO001")

    says(manager, session_id, "What is my account balance?")
    tool(manager, session_id, "get_account_balance", {})
    says(manager, session_id, "what are my options")
    tool(manager, session_id, "get_account_balance", {})

    says(manager, session_id, "Savings")
    answer = tool(manager, session_id, "get_account_balance", {})

    assert answer["success"] is True, (
        f"the enquiry did not survive the caller asking for the options: {answer}"
    )
    assert answer["account_type"] == "Savings"
    assert answer["available_balance"] == DEMO001_SAVINGS


def test_c10_both_on_a_loan_enquiry_asks_the_caller_to_choose_one(manager, executions):
    """The loan-domain equivalent, on DEMO003's two loans."""
    session_id = verified(manager, "DEMO003")

    says(manager, session_id, "Tell me about my loan")
    tool(manager, session_id, "get_loan_details", {})
    answered_before = executions.answered(session_id, "get_loan_details")

    ruling = says(manager, session_id, "both")
    assert ruling.category.value != "NON_BANKING_REQUEST", (
        "the caller answered the bank's own clarifying question and was told "
        "the bank only handles banking enquiries"
    )

    answer = tool(manager, session_id, "get_loan_details", {})

    assert answer.get("reason") != "OUT_OF_SCOPE", (
        f"the reply to the loan clarification was refused as off-topic: {answer}"
    )
    assert asked_which_loan(answer)
    assert answer["available_loan_types"] == ["Car Loan", "Home Loan"]
    assert executions.answered(session_id, "get_loan_details") == answered_before
