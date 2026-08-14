"""Loan Services Agent.

Answers loan balance, detail and instalment questions by calling the registered
loan tools. Like the account agent it holds no customer state: identity is
resolved from the session by the guards behind the tool layer.

    supervisor -> this agent -> registry.dispatch -> tool -> guard -> session
"""

from app.agents import speech
from app.agents.base import AgentResponse
from app.agents.intents import Classification, Domain, Intent
from app.agents.registry import dispatch
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager

AGENT_NAME = "loan_services"

TOOL_BY_INTENT = {
    Intent.LOAN_BALANCE: "get_loan_balance",
    Intent.LOAN_DETAILS: "get_loan_details",
    Intent.NEXT_INSTALMENT: "get_next_instalment",
}

LOAN_TYPE_REQUIRED = "LOAN_TYPE_REQUIRED"


def _balance_speech(data: dict) -> str:
    return (
        f"Your {data['loan_type']}, reference {data['loan_reference']}, has an "
        f"outstanding balance of "
        f"{speech.money(data['outstanding_balance'], data['currency'])}."
    )


def _details_speech(data: dict) -> str:
    return (
        f"Your {data['loan_type']}, reference {data['loan_reference']}, is "
        f"{data['status'].lower()}. The outstanding balance is "
        f"{speech.money(data['outstanding_balance'], data['currency'])}, at an "
        f"interest rate of {data['interest_rate']} percent. The next instalment "
        f"of {speech.money(data['next_instalment_amount'], data['currency'])} is "
        f"due on {speech.spoken_date(data['next_instalment_date'])}, and the "
        f"loan matures on {speech.spoken_date(data['maturity_date'])}."
    )


def _instalment_speech(data: dict) -> str:
    return (
        f"The next instalment on your {data['loan_type']} is "
        f"{speech.money(data['next_instalment_amount'], data['currency'])}, "
        f"due on {speech.spoken_date(data['next_instalment_date'])}."
    )


SPEECH_BY_INTENT = {
    Intent.LOAN_BALANCE: _balance_speech,
    Intent.LOAN_DETAILS: _details_speech,
    Intent.NEXT_INSTALMENT: _instalment_speech,
}


def _failure_speech(result: dict) -> str:
    """Phrase a refusal, naming the real choices when the tool offered them."""
    reason = result.get("reason", "")
    available = result.get("available_loan_types")

    if reason == LOAN_TYPE_REQUIRED and available:
        return speech.choose_loan_question(available)
    if reason == "LOAN_NOT_FOUND" and available:
        return (
            "I couldn't find that loan on your profile. "
            + speech.choose_loan_question(available)
        )
    return speech.refusal(reason)


class LoanServicesAgent:
    """Handles the LOAN domain."""

    name = AGENT_NAME

    def handle(
        self,
        session_id: str,
        classification: Classification,
        *,
        manager: SessionManager = default_manager,
    ) -> AgentResponse:
        """Answer one loan turn. The turn is already authorized."""
        tool_name = TOOL_BY_INTENT.get(classification.intent)
        if tool_name is None:
            return AgentResponse(
                agent=self.name,
                intent=classification.intent,
                domain=Domain.LOAN,
                speech=speech.UNSUPPORTED_SPEECH,
                success=False,
                reason="UNSUPPORTED_INTENT",
            )

        result = dispatch(
            tool_name,
            session_id,
            {"loan_type": classification.loan_type},
            manager=manager,
        )

        if not result.get("success"):
            return AgentResponse(
                agent=self.name,
                intent=classification.intent,
                domain=Domain.LOAN,
                speech=_failure_speech(result),
                success=False,
                reason=result.get("reason"),
                data=result,
            )

        return AgentResponse(
            agent=self.name,
            intent=classification.intent,
            domain=Domain.LOAN,
            speech=SPEECH_BY_INTENT[classification.intent](result),
            success=True,
            data=result,
        )
