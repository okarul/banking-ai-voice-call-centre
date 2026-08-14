"""Every Version 1 question, and the one tool that must answer it.

This file exists because a caller asked for a balance and was read their recent
transactions. Nothing here involves a model: routing is decided by explicit
Python rules, so every row below is pinned exactly and will stay pinned.

Each case asserts the whole chain, not just the final sentence:

    utterance -> domain -> intent -> specialist -> tool actually dispatched

The tool is captured by spying on `dispatch`, so these tests assert what really
ran rather than what an agent reported about itself.

All customers, accounts, loans and PINs are synthetic Phase 2 seed data.
"""

import pytest

from app.agents import handle_turn
from app.agents.account_agent import TOOL_BY_INTENT as ACCOUNT_TOOLS
from app.agents.intents import Domain, Intent
from app.agents.loan_agent import TOOL_BY_INTENT as LOAN_TOOLS
from app.auth import authentication
from app.sessions import SessionManager

PINS = {"DEMO001": "4821", "DEMO002": "7315", "DEMO005": "6072"}

ACCOUNT_AGENT = "account_services"
LOAN_AGENT = "loan_services"
SUPERVISOR = "supervisor"


@pytest.fixture
def manager():
    return SessionManager()


@pytest.fixture
def calls(monkeypatch):
    """Record every tool dispatched, in order, while still running it."""
    from app.agents import account_agent, loan_agent
    from app.agents.registry import dispatch as real_dispatch

    recorded = []

    def spy(tool_name, session_id, arguments=None, **kwargs):
        recorded.append((tool_name, dict(arguments or {})))
        return real_dispatch(tool_name, session_id, arguments, **kwargs)

    monkeypatch.setattr(account_agent, "dispatch", spy)
    monkeypatch.setattr(loan_agent, "dispatch", spy)
    return recorded


@pytest.fixture
def session(manager):
    """An authenticated DEMO001 call."""
    created = manager.create_session()
    assert authentication.verify_customer(
        created.session_id, "DEMO001", manager=manager
    )["success"]
    assert authentication.verify_pin(
        created.session_id, PINS["DEMO001"], manager=manager
    )["success"]
    return created


def ask(session, manager, text):
    return handle_turn(session.session_id, text, manager=manager)


# === the mapping itself =====================================================


def test_the_account_intent_to_tool_map_is_exactly_the_version_1_set():
    assert ACCOUNT_TOOLS == {
        Intent.ACCOUNT_BALANCE: "get_account_balance",
        Intent.ACCOUNT_DETAILS: "get_account_details",
        Intent.RECENT_TRANSACTIONS: "get_recent_transactions",
    }


def test_the_loan_intent_to_tool_map_is_exactly_the_version_1_set():
    assert LOAN_TOOLS == {
        Intent.LOAN_BALANCE: "get_loan_balance",
        Intent.LOAN_DETAILS: "get_loan_details",
        Intent.NEXT_INSTALMENT: "get_next_instalment",
    }


def test_no_intent_maps_to_a_tool_from_the_other_domain():
    assert not set(ACCOUNT_TOOLS.values()) & set(LOAN_TOOLS.values())


# === the matrix =============================================================

# utterance, domain, intent, specialist, tool
MATRIX = [
    # --- ACCOUNT_BALANCE ---------------------------------------------------
    ("What is my savings account balance?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("What is my current account balance?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("How much money do I have in savings?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("How much is in my savings?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("What's my savings balance?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("How much have I got left in my savings account?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("Savings account balance please.",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    # Terse forms. A caller was once told a savings balance was out of scope,
    # so the short phrasings are pinned explicitly.
    ("Savings balance.",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("Balance in savings.",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("Tell me my savings balance.",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("What's my savings balance?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),
    ("How much is in my current account?",
     Domain.ACCOUNT, Intent.ACCOUNT_BALANCE, ACCOUNT_AGENT, "get_account_balance"),

    # --- ACCOUNT_DETAILS ---------------------------------------------------
    ("Give me my savings account details.",
     Domain.ACCOUNT, Intent.ACCOUNT_DETAILS, ACCOUNT_AGENT, "get_account_details"),
    ("Tell me about my current account.",
     Domain.ACCOUNT, Intent.ACCOUNT_DETAILS, ACCOUNT_AGENT, "get_account_details"),
    ("What is my savings account information?",
     Domain.ACCOUNT, Intent.ACCOUNT_DETAILS, ACCOUNT_AGENT, "get_account_details"),
    ("What is the status of my savings account?",
     Domain.ACCOUNT, Intent.ACCOUNT_DETAILS, ACCOUNT_AGENT, "get_account_details"),

    # --- RECENT_TRANSACTIONS -----------------------------------------------
    ("What are my last three transactions?",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("Tell me my recent savings transactions.",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("Show my recent transactions.",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("What did I spend recently?",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("Show me my recent account activity.",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("Can I get a statement on my savings account?",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),
    ("What is my savings transaction history?",
     Domain.ACCOUNT, Intent.RECENT_TRANSACTIONS, ACCOUNT_AGENT, "get_recent_transactions"),

    # --- LOAN_BALANCE ------------------------------------------------------
    ("What is my home loan balance?",
     Domain.LOAN, Intent.LOAN_BALANCE, LOAN_AGENT, "get_loan_balance"),
    ("What is my outstanding home loan amount?",
     Domain.LOAN, Intent.LOAN_BALANCE, LOAN_AGENT, "get_loan_balance"),
    ("How much do I still owe on my home loan?",
     Domain.LOAN, Intent.LOAN_BALANCE, LOAN_AGENT, "get_loan_balance"),
    ("How much is left on my mortgage?",
     Domain.LOAN, Intent.LOAN_BALANCE, LOAN_AGENT, "get_loan_balance"),
    ("What is the outstanding balance on my home loan?",
     Domain.LOAN, Intent.LOAN_BALANCE, LOAN_AGENT, "get_loan_balance"),

    # --- NEXT_INSTALMENT ---------------------------------------------------
    ("When is my next instalment?",
     Domain.LOAN, Intent.NEXT_INSTALMENT, LOAN_AGENT, "get_next_instalment"),
    ("How much is my next loan payment?",
     Domain.LOAN, Intent.NEXT_INSTALMENT, LOAN_AGENT, "get_next_instalment"),
    ("When is my next payment?",
     Domain.LOAN, Intent.NEXT_INSTALMENT, LOAN_AGENT, "get_next_instalment"),
    ("What is my monthly instalment on the home loan?",
     Domain.LOAN, Intent.NEXT_INSTALMENT, LOAN_AGENT, "get_next_instalment"),
    ("When is my home loan payment due?",
     Domain.LOAN, Intent.NEXT_INSTALMENT, LOAN_AGENT, "get_next_instalment"),

    # --- LOAN_DETAILS ------------------------------------------------------
    ("What is my loan interest rate?",
     Domain.LOAN, Intent.LOAN_DETAILS, LOAN_AGENT, "get_loan_details"),
    ("What interest rate am I paying?",
     Domain.LOAN, Intent.LOAN_DETAILS, LOAN_AGENT, "get_loan_details"),
    ("When does my home loan mature?",
     Domain.LOAN, Intent.LOAN_DETAILS, LOAN_AGENT, "get_loan_details"),
    ("Give me my home loan details.",
     Domain.LOAN, Intent.LOAN_DETAILS, LOAN_AGENT, "get_loan_details"),
    ("Tell me about my home loan.",
     Domain.LOAN, Intent.LOAN_DETAILS, LOAN_AGENT, "get_loan_details"),
]


@pytest.mark.parametrize("text,domain,intent,specialist,tool", MATRIX)
def test_every_version_1_question_reaches_exactly_one_correct_tool(
    session, manager, calls, text, domain, intent, specialist, tool
):
    response = ask(session, manager, text)

    assert response.intent is intent, text
    assert response.domain is domain, text
    assert response.agent == specialist, text
    # Exactly one tool ran, and it was the mapped one.
    assert [name for name, _ in calls] == [tool], text


@pytest.mark.parametrize("text,domain,intent,specialist,tool", MATRIX)
def test_every_answer_is_built_from_its_own_tool_result(
    session, manager, calls, text, domain, intent, specialist, tool
):
    """The spoken answer must express the selected tool's data."""
    response = ask(session, manager, text)

    if not response.success:
        # The only acceptable non-answer is asking which account or loan.
        assert response.reason in ("ACCOUNT_TYPE_REQUIRED", "LOAN_TYPE_REQUIRED"), text
        return

    if intent is Intent.ACCOUNT_BALANCE:
        assert response.data["available_balance"] in response.speech.replace(",", "")
        assert "transaction" not in response.speech.lower()
    if intent is Intent.RECENT_TRANSACTIONS:
        assert "transactions" in response.speech.lower()
        assert response.data["transactions"]
    if intent is Intent.LOAN_BALANCE:
        assert response.data["outstanding_balance"] in response.speech.replace(",", "")
    if intent is Intent.NEXT_INSTALMENT:
        assert response.data["next_instalment_amount"] in response.speech.replace(",", "")
    if intent is Intent.LOAN_DETAILS:
        assert response.data["interest_rate"] in response.speech


# === the specific regression: a balance is never transactions ===============


def test_a_balance_question_never_calls_the_transactions_tool(session, manager, calls):
    ask(session, manager, "What is my savings account balance?")

    assert [name for name, _ in calls] == ["get_account_balance"]
    assert "get_recent_transactions" not in [name for name, _ in calls]


def test_a_balance_answer_carries_a_balance_and_no_transaction_list(session, manager):
    response = ask(session, manager, "What is my savings account balance?")

    assert response.success is True
    assert response.data["available_balance"] == "12450.75"
    assert "transactions" not in response.data
    assert "12,450.75" in response.speech


# === ambiguity is asked about, never guessed ================================


def test_a_bare_balance_question_is_never_guessed(session, manager):
    """DEMO001 holds accounts and loans, so 'my balance' is a question."""
    response = ask(session, manager, "What is my balance?")

    assert response.success is False
    assert response.intent is Intent.UNKNOWN
    assert "account" in response.speech.lower()
    assert "loan" in response.speech.lower()


def test_a_bare_account_balance_question_asks_which_account(session, manager, calls):
    """DEMO001 holds two accounts, so the account type must be asked for."""
    response = ask(session, manager, "What is my account balance?")

    assert response.reason == "ACCOUNT_TYPE_REQUIRED"
    assert "savings" in response.speech.lower()
    assert "current" in response.speech.lower()
    assert [name for name, _ in calls] == ["get_account_balance"]


def test_a_single_account_customer_is_still_asked_account_or_loan(manager, calls):
    """DEMO002 holds one account — but also a loan, so "my balance" is still
    ambiguous. The domain question comes first and nothing is guessed."""
    created = manager.create_session()
    authentication.verify_customer(created.session_id, "DEMO002", manager=manager)
    authentication.verify_pin(created.session_id, PINS["DEMO002"], manager=manager)

    response = handle_turn(created.session_id, "What is my balance?", manager=manager)

    assert response.success is False
    assert response.reason == "DOMAIN_REQUIRED"
    assert calls == []


def test_naming_the_domain_then_resolves_a_single_account_without_asking_again(
    manager, calls
):
    """DEMO002 holds one account, so once the domain is known there is nothing
    left to disambiguate."""
    created = manager.create_session()
    authentication.verify_customer(created.session_id, "DEMO002", manager=manager)
    authentication.verify_pin(created.session_id, PINS["DEMO002"], manager=manager)

    response = handle_turn(
        created.session_id, "What is my account balance?", manager=manager
    )

    assert response.success is True
    assert response.intent is Intent.ACCOUNT_BALANCE
    assert response.data["masked_account"] == "XXXX1002"


# === unsupported ============================================================


@pytest.mark.parametrize(
    "text",
    [
        "Transfer five hundred dollars to John.",
        "I want to block my credit card.",
        "Please change my address.",
        "What is the weather today?",
        "Open a new fixed deposit for me.",
        "Tell me a joke.",
        "Add my wife as a beneficiary.",
    ],
)
def test_unsupported_requests_reach_no_tool(session, manager, calls, text):
    response = ask(session, manager, text)

    assert response.success is False
    assert response.intent is Intent.UNKNOWN
    assert calls == []


# === sequential conversation: a new request always wins =====================

SEQUENCES = [
    ("transactions then balance",
     ["Tell me my recent savings transactions.", "What is my savings account balance?"],
     ["get_recent_transactions", "get_account_balance"]),
    ("balance then transactions",
     ["What is my savings account balance?", "Tell me my recent savings transactions."],
     ["get_account_balance", "get_recent_transactions"]),
    ("account then loan",
     ["What is my savings account balance?", "What is my home loan balance?"],
     ["get_account_balance", "get_loan_balance"]),
    ("loan then account",
     ["What is my home loan balance?", "What is my savings account balance?"],
     ["get_loan_balance", "get_account_balance"]),
    ("loan balance then instalment",
     ["What is my home loan balance?", "When is my next instalment?"],
     ["get_loan_balance", "get_next_instalment"]),
    ("account balance then account details",
     ["What is my savings account balance?", "Give me my savings account details."],
     ["get_account_balance", "get_account_details"]),
    ("transactions then loan balance",
     ["Tell me my recent savings transactions.", "What is my home loan balance?"],
     ["get_recent_transactions", "get_loan_balance"]),
    ("loan details then savings balance",
     ["What is my home loan interest rate?", "What is my savings balance?"],
     ["get_loan_details", "get_account_balance"]),
]


@pytest.mark.parametrize("label,turns,expected", SEQUENCES)
def test_a_clear_new_request_is_never_overridden_by_the_previous_one(
    session, manager, calls, label, turns, expected
):
    for text in turns:
        ask(session, manager, text)

    assert [name for name, _ in calls] == expected, label


@pytest.mark.parametrize(
    "first",
    [
        "What are my last three transactions?",
        "What is my home loan balance?",
        "Transfer five hundred dollars to John.",
        "Tell me a joke.",
    ],
)
def test_no_previous_turn_can_make_a_savings_balance_unsupported(
    session, manager, calls, first
):
    """Whatever came before — including a refused request — a balance question
    is still a balance question."""
    ask(session, manager, first)
    calls.clear()

    response = ask(session, manager, "What is my savings balance?")

    assert response.intent is Intent.ACCOUNT_BALANCE, first
    assert response.domain is Domain.ACCOUNT, first
    assert response.agent == ACCOUNT_AGENT, first
    assert [name for name, _ in calls] == ["get_account_balance"], first
    assert response.data["available_balance"] == "12450.75"


def test_the_reported_failure_does_not_recur(session, manager, calls):
    """Transactions, then a balance question. The exact reported sequence."""
    first = ask(session, manager, "What are my last three transactions?")
    second = ask(session, manager, "What is my savings account balance?")

    assert [name for name, _ in calls] == [
        "get_recent_transactions",
        "get_account_balance",
    ]
    assert second.intent is Intent.ACCOUNT_BALANCE
    assert second.data["available_balance"] == "12450.75"
    assert "transaction" not in second.speech.lower()
    # And the first turn was still answered on its own terms.
    assert first.intent is Intent.RECENT_TRANSACTIONS


# === elliptical follow-ups still work =======================================


def test_an_elliptical_follow_up_still_uses_context(session, manager, calls):
    """'When is the next one due?' after a loan must stay on that loan."""
    ask(session, manager, "What is my home loan balance?")
    response = ask(session, manager, "When is the next one due?")

    assert [name for name, _ in calls] == ["get_loan_balance", "get_next_instalment"]
    assert response.success is True
    assert response.data["loan_type"] == "Home Loan"


def test_a_bare_account_reply_resumes_the_original_question(session, manager, calls):
    """Answering 'which account?' must resume the question that asked it."""
    ask(session, manager, "What is my account balance?")
    response = ask(session, manager, "Savings")

    assert [name for name, _ in calls] == ["get_account_balance", "get_account_balance"]
    assert response.success is True
    assert response.data["masked_account"] == "XXXX1001"
