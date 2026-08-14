"""Account Services Agent.

Answers balance, detail and transaction questions by calling the registered
account tools. It holds no customer state of its own: the session id it is
given is passed straight to the registry, and identity is resolved from the
session by the guards behind the tool layer.

    supervisor -> this agent -> registry.dispatch -> tool -> guard -> session
"""

from app.agents import speech
from app.agents.base import AgentResponse
from app.agents.intents import Classification, Domain, Intent
from app.agents.registry import dispatch
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager

AGENT_NAME = "account_services"

TOOL_BY_INTENT = {
    Intent.ACCOUNT_BALANCE: "get_account_balance",
    Intent.ACCOUNT_DETAILS: "get_account_details",
    Intent.RECENT_TRANSACTIONS: "get_recent_transactions",
}

ACCOUNT_TYPE_REQUIRED = "ACCOUNT_TYPE_REQUIRED"


def _balance_speech(data: dict) -> str:
    return (
        f"Your {data['account_type']} account ending "
        f"{speech.last_four(data['masked_account'])} has an available balance "
        f"of {speech.money(data['available_balance'], data['currency'])}."
    )


def _details_speech(data: dict) -> str:
    return (
        f"Your {data['account_type']} account ending "
        f"{speech.last_four(data['masked_account'])} is {data['status'].lower()}, "
        f"with an available balance of "
        f"{speech.money(data['available_balance'], data['currency'])}."
    )


def _transactions_speech(data: dict) -> str:
    """Read the transactions back, using the sign to say in or out."""
    transactions = data["transactions"]
    tail = speech.last_four(data["masked_account"])
    if not transactions:
        return (
            f"I don't have any recent transactions on your "
            f"{data['account_type']} account ending {tail}."
        )

    lines = []
    for entry in transactions:
        amount = str(entry["amount"])
        direction = "out" if amount.startswith("-") else "in"
        lines.append(
            f"{speech.spoken_date(entry['date'])}, {entry['description']}, "
            f"{speech.money(amount.lstrip('-'))} {direction}"
        )

    count = len(lines)
    noun = "transaction" if count == 1 else "transactions"
    return (
        f"Here are the last {count} {noun} on your {data['account_type']} "
        f"account ending {tail}. " + ". ".join(lines) + "."
    )


SPEECH_BY_INTENT = {
    Intent.ACCOUNT_BALANCE: _balance_speech,
    Intent.ACCOUNT_DETAILS: _details_speech,
    Intent.RECENT_TRANSACTIONS: _transactions_speech,
}


def _failure_speech(result: dict) -> str:
    """Phrase a refusal, naming the real choices when the tool offered them."""
    reason = result.get("reason", "")
    available = result.get("available_account_types")

    if reason == ACCOUNT_TYPE_REQUIRED and available:
        return speech.choose_account_question(available)
    if reason == "ACCOUNT_NOT_FOUND" and available:
        return (
            "I couldn't find that account on your profile. "
            + speech.choose_account_question(available)
        )
    return speech.refusal(reason)


class AccountServicesAgent:
    """Handles the ACCOUNT domain."""

    name = AGENT_NAME

    def handle(
        self,
        session_id: str,
        classification: Classification,
        *,
        manager: SessionManager = default_manager,
    ) -> AgentResponse:
        """Answer one account turn. The turn is already authorized."""
        tool_name = TOOL_BY_INTENT.get(classification.intent)
        if tool_name is None:
            return AgentResponse(
                agent=self.name,
                intent=classification.intent,
                domain=Domain.ACCOUNT,
                speech=speech.UNSUPPORTED_SPEECH,
                success=False,
                reason="UNSUPPORTED_INTENT",
            )

        result = dispatch(
            tool_name,
            session_id,
            {"account_type": classification.account_type},
            manager=manager,
        )

        if not result.get("success"):
            return AgentResponse(
                agent=self.name,
                intent=classification.intent,
                domain=Domain.ACCOUNT,
                speech=_failure_speech(result),
                success=False,
                reason=result.get("reason"),
                data=result,
            )

        return AgentResponse(
            agent=self.name,
            intent=classification.intent,
            domain=Domain.ACCOUNT,
            speech=SPEECH_BY_INTENT[classification.intent](result),
            success=True,
            data=result,
        )
