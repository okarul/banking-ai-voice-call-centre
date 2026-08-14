"""Phase 8 agent layer tests.

Three things are pinned here:

    the classifier is deterministic and never guesses a domain
    no turn reaches a banking tool before the session is authenticated
    two callers can never resume or read each other's conversation

Values are checked against the Phase 2 synthetic seed data.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents import handle_turn
from app.agents.intents import Domain, Intent, classify, parse_type_reply
from app.agents.supervisor import (
    DOMAIN_REQUIRED,
    NOT_UNDERSTOOD,
    PENDING_ACTION_KEY,
    PENDING_INTENT_KEY,
)
from app.auth import authentication
from app.main import app
from app.scope import ScopeCategory
from app.sessions import SessionManager

# Synthetic demo PINs from the Phase 2 seed. Not real credentials.
PINS = {
    "DEMO001": "4821",  # two accounts, one Home Loan
    "DEMO002": "7315",  # one account, one Personal Loan
    "DEMO003": "2648",  # two accounts, two loans
}


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def client():
    return TestClient(app)


def _authenticated(manager, customer_id="DEMO001"):
    """Create a session and take it through the real authentication flow."""
    session = manager.create_session()
    assert authentication.verify_customer(
        session.session_id, customer_id, manager=manager
    )["success"]
    assert authentication.verify_pin(
        session.session_id, PINS[customer_id], manager=manager
    )["success"]
    return session


def _turn(manager, session, text):
    return handle_turn(session.session_id, text, manager=manager)


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("what is my savings balance", Intent.ACCOUNT_BALANCE),
        ("tell me about my current account", Intent.ACCOUNT_DETAILS),
        ("show me my recent transactions", Intent.RECENT_TRANSACTIONS),
        ("what have I spent lately", Intent.RECENT_TRANSACTIONS),
        ("how much do I owe on my home loan", Intent.LOAN_BALANCE),
        ("tell me about my mortgage", Intent.LOAN_DETAILS),
        ("what is my loan interest rate", Intent.LOAN_DETAILS),
        ("when is my next instalment due", Intent.NEXT_INSTALMENT),
        ("how much is my next repayment", Intent.NEXT_INSTALMENT),
        ("goodbye", Intent.END_CALL),
        ("that's all, thank you", Intent.END_CALL),
    ],
)
def test_classifier_routes_plain_requests(text, expected):
    assert classify(text).intent is expected


def test_classifier_extracts_the_named_account():
    result = classify("what is the balance on my savings account")

    assert result.intent is Intent.ACCOUNT_BALANCE
    assert result.account_type == "Savings"


def test_classifier_extracts_the_named_loan():
    result = classify("what is my car loan balance")

    assert result.intent is Intent.LOAN_BALANCE
    assert result.loan_type == "Car Loan"


def test_loan_wins_over_account_when_both_words_appear():
    """'my loan account' is a loan question; 'loan' is the specific noun."""
    assert classify("what is the balance on my loan account").intent is (
        Intent.LOAN_BALANCE
    )


def test_bare_balance_question_asks_which_domain():
    result = classify("what is my balance")

    assert result.intent is Intent.UNKNOWN
    assert result.needs_domain is True


def test_bare_balance_question_follows_the_session_domain():
    result = classify("what is my balance", current_domain="LOAN")

    assert result.intent is Intent.LOAN_BALANCE
    assert result.needs_domain is False


def test_stated_domain_beats_the_session_domain():
    """A caller who changes subject is not held to the previous one."""
    result = classify("what is my savings balance", current_domain="LOAN")

    assert result.intent is Intent.ACCOUNT_BALANCE


def test_current_balance_is_not_the_current_account():
    """'current' is a time word here, not an account type."""
    result = classify("what is my current balance")

    assert result.account_type is None
    assert result.needs_domain is True


def test_unanswerable_combination_is_not_bent_into_another_answer():
    """There are no loan transactions, so this is not answered with account ones."""
    result = classify("show me the transactions on my loan")

    assert result.intent is Intent.UNKNOWN
    assert result.domain is Domain.LOAN


def test_unrelated_speech_is_not_classified():
    assert classify("what is the weather like today").intent is Intent.UNKNOWN


def test_type_reply_accepts_a_bare_answer():
    assert parse_type_reply("savings", Domain.ACCOUNT) == "Savings"
    assert parse_type_reply("the current one", Domain.ACCOUNT) == "Current"
    assert parse_type_reply("home", Domain.LOAN) == "Home Loan"
    assert parse_type_reply("car please", Domain.LOAN) == "Car Loan"


def test_type_reply_rejects_a_full_request():
    """Only a bare answer counts, so 'my current balance' is not the Current account."""
    assert parse_type_reply("what is my current balance", Domain.ACCOUNT) is None


# --- authentication comes first ---------------------------------------------


def test_unauthenticated_turn_never_reaches_a_tool(manager):
    session = manager.create_session()

    response = _turn(manager, session, "what is my savings balance")

    assert response.success is False
    assert response.reason == "NOT_AUTHENTICATED"
    assert response.requires_authentication is True
    assert "customer ID" in response.speech
    assert response.data == {"success": False, "reason": "NOT_AUTHENTICATED"}


def test_unauthenticated_turn_returns_no_banking_values(manager):
    session = manager.create_session()

    response = _turn(manager, session, "what is my loan balance")

    assert "outstanding_balance" not in response.data
    assert response.agent == "supervisor"


def test_locked_session_is_refused(manager):
    session = manager.create_session()
    authentication.verify_customer(session.session_id, "DEMO001", manager=manager)
    for _ in range(3):
        authentication.verify_pin(session.session_id, "0000", manager=manager)

    response = _turn(manager, session, "what is my savings balance")

    assert response.reason == "AUTHENTICATION_LOCKED"
    assert "locked" in response.speech.lower()


def test_unknown_session_is_refused(manager):
    response = handle_turn("SESSION-does-not-exist", "hello", manager=manager)

    assert response.reason == "SESSION_NOT_FOUND"
    assert response.requires_authentication is True


def test_saying_goodbye_needs_no_authentication(manager):
    """Ending a call reveals nothing, so it is answered before the guard."""
    session = manager.create_session()

    response = _turn(manager, session, "goodbye")

    assert response.success is True
    assert response.intent is Intent.END_CALL
    assert "Goodbye" in response.speech


# --- account routing --------------------------------------------------------


def test_account_balance_is_answered_and_spoken(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "what is my savings balance")

    assert response.success is True
    assert response.agent == "account_services"
    assert response.intent is Intent.ACCOUNT_BALANCE
    assert response.data["available_balance"] == "12450.75"
    assert "12,450.75 SGD" in response.speech
    assert "1001" in response.speech


def test_single_account_customer_needs_no_clarification(manager):
    session = _authenticated(manager, "DEMO002")

    response = _turn(manager, session, "what is my account balance")

    assert response.success is True
    assert response.data["masked_account"] == "XXXX1002"


def test_account_details_are_answered(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "tell me about my current account")

    assert response.success is True
    assert response.intent is Intent.ACCOUNT_DETAILS
    assert response.data["masked_account"] == "XXXX2001"


def test_recent_transactions_are_read_back(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "show me recent transactions on my savings")

    assert response.success is True
    assert response.intent is Intent.RECENT_TRANSACTIONS
    assert len(response.data["transactions"]) == 3
    assert "FAST Transfer" in response.speech
    assert "out" in response.speech


# --- loan routing -----------------------------------------------------------


def test_loan_balance_is_answered(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "how much do I owe on my home loan")

    assert response.success is True
    assert response.agent == "loan_services"
    assert response.data["outstanding_balance"] == "284500.00"
    assert "284,500.00 SGD" in response.speech


def test_next_instalment_is_answered(manager):
    session = _authenticated(manager, "DEMO002")

    response = _turn(manager, session, "when is my next instalment due")

    assert response.success is True
    assert response.intent is Intent.NEXT_INSTALMENT
    assert "620.00 SGD" in response.speech
    assert "12 September 2026" in response.speech


def test_loan_details_are_answered(manager):
    session = _authenticated(manager, "DEMO002")

    response = _turn(manager, session, "tell me about my personal loan")

    assert response.success is True
    assert response.data["interest_rate"] == "6.500"


# --- clarifying, then resuming ----------------------------------------------


def test_two_accounts_produce_a_question_not_a_guess(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "what is my account balance")

    assert response.success is False
    assert response.reason == "ACCOUNT_TYPE_REQUIRED"
    assert "Savings" in response.speech and "Current" in response.speech
    assert "available_balance" not in response.data


def test_the_pending_question_is_recorded_on_the_session(manager):
    session = _authenticated(manager, "DEMO001")

    _turn(manager, session, "what is my account balance")

    stored = manager.get_session(session.session_id)
    assert stored.conversation_context[PENDING_INTENT_KEY] == "ACCOUNT_BALANCE"


def test_a_bare_answer_resumes_the_original_question(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my account balance")

    response = _turn(manager, session, "savings")

    assert response.success is True
    assert response.intent is Intent.ACCOUNT_BALANCE
    assert response.data["available_balance"] == "12450.75"


def test_resuming_clears_the_pending_question(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my account balance")
    _turn(manager, session, "savings")

    stored = manager.get_session(session.session_id)
    assert PENDING_INTENT_KEY not in stored.conversation_context


def test_a_bare_loan_answer_resumes_the_loan_question(manager):
    session = _authenticated(manager, "DEMO003")
    first = _turn(manager, session, "what is my loan balance")
    assert first.reason == "LOAN_TYPE_REQUIRED"

    response = _turn(manager, session, "home")

    assert response.success is True
    assert response.data["loan_reference"] == "HL-DEMO003"


def test_a_new_question_is_not_swallowed_by_a_pending_one(manager):
    """Changing subject mid-clarification answers the new question."""
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my account balance")

    response = _turn(manager, session, "what is my home loan balance")

    assert response.success is True
    assert response.intent is Intent.LOAN_BALANCE


def test_naming_an_account_the_customer_does_not_hold(manager):
    session = _authenticated(manager, "DEMO002")

    response = _turn(manager, session, "what is my current account balance")

    assert response.success is False
    assert response.reason == "ACCOUNT_NOT_FOUND"
    assert "Savings" in response.speech


# --- carrying the conversation ----------------------------------------------


def test_the_domain_carries_to_the_next_turn(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "tell me about my home loan")

    response = _turn(manager, session, "and what is the balance")

    assert response.success is True
    assert response.intent is Intent.LOAN_BALANCE


def test_the_chosen_loan_carries_to_the_next_question(manager):
    """Having just picked a loan, the caller is not asked which loan again."""
    session = _authenticated(manager, "DEMO003")
    _turn(manager, session, "what is my loan balance")
    assert _turn(manager, session, "home").success is True

    response = _turn(manager, session, "when is the next instalment due")

    assert response.success is True
    assert response.data["loan_type"] == "Home Loan"


def test_the_chosen_account_carries_to_the_next_question(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my savings balance")

    response = _turn(manager, session, "and the account details")

    assert response.success is True
    assert response.data["masked_account"] == "XXXX1001"


def test_naming_another_account_moves_the_conversation(manager):
    """A carried selection never overrides one the caller states."""
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my savings balance")

    response = _turn(manager, session, "what about my current account balance")

    assert response.success is True
    assert response.data["masked_account"] == "XXXX2001"


def test_selection_does_not_carry_across_domains(manager):
    """An account chosen earlier does not silently answer a loan question."""
    session = _authenticated(manager, "DEMO003")
    _turn(manager, session, "what is my savings balance")

    response = _turn(manager, session, "what is my loan balance")

    assert response.success is False
    assert response.reason == "LOAN_TYPE_REQUIRED"


def test_a_carried_selection_stays_on_its_own_session(manager):
    first = _authenticated(manager, "DEMO003")
    second = _authenticated(manager, "DEMO003")
    _turn(manager, first, "what is my loan balance")
    _turn(manager, first, "home")

    response = _turn(manager, second, "what is my loan balance")

    # The second caller chose nothing, so they are still asked.
    assert response.reason == "LOAN_TYPE_REQUIRED"


def test_the_last_intent_is_recorded(manager):
    session = _authenticated(manager, "DEMO001")

    _turn(manager, session, "what is my savings balance")

    assert manager.get_session(session.session_id).previous_intent == "ACCOUNT_BALANCE"


def test_an_ambiguous_opening_question_is_asked_back(manager):
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "what is my balance")

    assert response.success is False
    assert response.reason == DOMAIN_REQUIRED
    assert "account" in response.speech and "loan" in response.speech


def test_answering_the_domain_question_completes_the_request(manager):
    """The caller supplies only the missing half; the verb was already understood."""
    session = _authenticated(manager, "DEMO001")
    assert _turn(manager, session, "what is my balance").reason == DOMAIN_REQUIRED

    response = _turn(manager, session, "loan")

    assert response.success is True
    assert response.intent is Intent.LOAN_BALANCE
    assert response.data["outstanding_balance"] == "284500.00"


def test_the_outstanding_domain_question_is_recorded(manager):
    session = _authenticated(manager, "DEMO001")

    _turn(manager, session, "what is my balance")

    stored = manager.get_session(session.session_id)
    assert stored.conversation_context[PENDING_ACTION_KEY] == "BALANCE"


def test_answering_the_domain_question_clears_it(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my balance")
    _turn(manager, session, "loan")

    stored = manager.get_session(session.session_id)
    assert PENDING_ACTION_KEY not in stored.conversation_context


def test_domain_answer_can_name_the_account_at_the_same_time(manager):
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my balance")

    response = _turn(manager, session, "my savings account")

    assert response.success is True
    assert response.intent is Intent.ACCOUNT_BALANCE
    assert response.data["masked_account"] == "XXXX1001"


def test_domain_answer_then_account_answer_completes_the_request(manager):
    """Both clarifications in sequence: which domain, then which account."""
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my balance")
    second = _turn(manager, session, "account")
    assert second.reason == "ACCOUNT_TYPE_REQUIRED"

    response = _turn(manager, session, "current")

    assert response.success is True
    assert response.data["masked_account"] == "XXXX2001"


def test_an_unanswerable_domain_answer_is_not_forced(manager):
    """The weather cannot complete a balance request, so it is not pretended to.

    An outstanding clarifying question does not pull an unrelated utterance
    into banking: the scope gate sees it first and answers out of scope.
    """
    session = _authenticated(manager, "DEMO001")
    _turn(manager, session, "what is my balance")

    response = _turn(manager, session, "the weather")

    assert response.success is False
    assert response.reason == ScopeCategory.NON_BANKING_REQUEST.value
    assert "ABC Demo Bank" in response.speech


def test_unrecognised_speech_offers_what_can_be_done(manager):
    """A question the bank does not answer is refused by scope, not by
    confusion — the caller is told what this line is for."""
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "what is the weather like today")

    assert response.success is False
    assert response.reason == ScopeCategory.NON_BANKING_REQUEST.value
    assert "account and loan enquiries" in response.speech


def test_gibberish_still_gets_a_helpful_reply(manager):
    """Nothing recognisable at all must still produce a usable sentence."""
    session = _authenticated(manager, "DEMO001")

    response = _turn(manager, session, "mmm hrrm blargh")

    assert response.success is False
    assert response.speech


# --- session isolation ------------------------------------------------------


def test_two_callers_get_their_own_answers(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")

    one = _turn(manager, first, "what is my savings balance")
    two = _turn(manager, second, "what is my savings balance")

    assert one.data["available_balance"] == "12450.75"
    assert two.data["available_balance"] == "8730.20"


def test_one_callers_pending_question_does_not_reach_another(manager):
    """A clarification outstanding on one call is invisible to the other."""
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO003")
    _turn(manager, first, "what is my account balance")

    response = _turn(manager, second, "savings")

    # DEMO003 has no question outstanding, so a bare word is just not understood.
    assert response.success is False
    assert response.reason == NOT_UNDERSTOOD


def test_one_callers_domain_does_not_reach_another(manager):
    first = _authenticated(manager, "DEMO001")
    second = _authenticated(manager, "DEMO002")
    _turn(manager, first, "tell me about my home loan")

    response = _turn(manager, second, "what is my balance")

    assert response.reason == DOMAIN_REQUIRED


# --- development endpoints --------------------------------------------------


def _authenticated_via_http(client, customer_id="DEMO001"):
    session_id = client.post("/dev/sessions").json()["session_id"]
    client.post(
        "/dev/auth/customer",
        json={"session_id": session_id, "customer_id": customer_id},
    )
    client.post(
        "/dev/auth/pin", json={"session_id": session_id, "pin": PINS[customer_id]}
    )
    return session_id


def test_tools_endpoint_publishes_the_schemas(client):
    response = client.get("/dev/agents/tools")

    assert response.status_code == 200
    tools = response.json()["tools"]
    assert len(tools) == 6
    for tool in tools:
        assert "session_id" not in tool["parameters"]["properties"]
        assert "customer_id" not in tool["parameters"]["properties"]


def test_turn_endpoint_answers_an_authenticated_caller(client):
    session_id = _authenticated_via_http(client, "DEMO002")

    response = client.post(
        "/dev/agents/turn",
        json={"session_id": session_id, "text": "what is my savings balance"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["agent"] == "account_services"
    assert "8,730.20 SGD" in body["speech"]


def test_turn_endpoint_reports_an_unauthenticated_caller(client):
    session_id = client.post("/dev/sessions").json()["session_id"]

    response = client.post(
        "/dev/agents/turn",
        json={"session_id": session_id, "text": "what is my savings balance"},
    )

    # A turn always produces something to say, so this is a 200 with a reason.
    assert response.status_code == 200
    body = response.json()
    assert body["requires_authentication"] is True
    assert body["reason"] == "NOT_AUTHENTICATED"


def test_turn_endpoint_rejects_an_unknown_session(client):
    response = client.post(
        "/dev/agents/turn",
        json={"session_id": "SESSION-nope", "text": "hello"},
    )

    assert response.status_code == 404


def test_turn_endpoint_takes_no_customer_id(client):
    """There is no parameter by which a caller could name someone else."""
    session_id = _authenticated_via_http(client, "DEMO002")

    response = client.post(
        "/dev/agents/turn",
        json={
            "session_id": session_id,
            "text": "what is my savings balance",
            "customer_id": "DEMO001",
        },
    )

    # The extra field is ignored; the answer is still DEMO002's.
    assert response.status_code == 200
    assert response.json()["data"]["masked_account"] == "XXXX1002"
