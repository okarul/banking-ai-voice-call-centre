"""Supervisor Agent: the single entry point for a caller turn.

The supervisor never touches banking data. It decides three things and then
steps out of the way:

    1. is this turn allowed at all   -> the authorization guard decides
    2. what is the caller asking     -> the deterministic classifier decides
    3. who should answer it          -> the domain agent decides the wording

Order matters. A turn is authenticated before it is ever handed to a domain
agent, so there is no path from an unauthenticated session to a banking tool.
Ending the call is the one thing handled before that check, because saying
goodbye reveals nothing.

The supervisor also carries the thread of the conversation across turns. When a
tool asks which account or loan was meant, the pending intent is recorded on
that session, so a bare "Savings" on the next turn resumes the original
question instead of being heard as a new one. That state lives on the session
and nowhere else, so two callers can never resume each other's question.
"""

from dataclasses import replace

from app.agents import speech
from app.agents.account_agent import AccountServicesAgent
from app.agents.base import AgentResponse
from app.agents.intents import (
    DOMAIN_BY_INTENT,
    INTENT_BY_DOMAIN_ACTION,
    THANKS,
    Action,
    Classification,
    Domain,
    Intent,
    classify,
    parse_type_reply,
    social_turn,
)
from app.agents.loan_agent import LoanServicesAgent
from app.agents.selection import carry_type
from app import pending_request
from app.authorization import AuthorizationError, require_authenticated_customer
from app.sessions import SessionManager, SessionNotFoundError
from app.sessions import session_manager as default_manager

AGENT_NAME = "supervisor"

# Which tool answers which enquiry. Used to describe a held request in the same
# vocabulary the voice path uses, so both resume the same way.
TOOL_BY_INTENT = {
    Intent.ACCOUNT_BALANCE: "get_account_balance",
    Intent.ACCOUNT_DETAILS: "get_account_details",
    Intent.RECENT_TRANSACTIONS: "get_recent_transactions",
    Intent.LOAN_BALANCE: "get_loan_balance",
    Intent.LOAN_DETAILS: "get_loan_details",
    Intent.NEXT_INSTALMENT: "get_next_instalment",
}

PENDING_INTENT_KEY = "pending_intent"
PENDING_ACTION_KEY = "pending_action"

# Conversational outcomes that are not refusals: the call continues, the
# supervisor simply needs another word from the caller first.
DOMAIN_REQUIRED = "DOMAIN_REQUIRED"
NOT_UNDERSTOOD = "NOT_UNDERSTOOD"

# Reasons that mean "the tool needs to know which one" rather than "no".
CLARIFICATION_REASONS = frozenset({"ACCOUNT_TYPE_REQUIRED", "LOAN_TYPE_REQUIRED"})


class SupervisorAgent:
    """Routes one caller turn to the agent that should answer it."""

    name = AGENT_NAME

    def __init__(self, account_agent=None, loan_agent=None) -> None:
        self._agents = {
            Domain.ACCOUNT: account_agent or AccountServicesAgent(),
            Domain.LOAN: loan_agent or LoanServicesAgent(),
        }

    # --- public API -------------------------------------------------------

    def handle_turn(
        self,
        session_id: str,
        text: str,
        *,
        manager: SessionManager = default_manager,
    ) -> AgentResponse:
        """Classify, authorize and route one utterance. Always returns a reply."""
        session = manager.get_session(session_id)
        current_domain = session.current_domain if session is not None else None
        classification = classify(text, current_domain=current_domain)

        # Handled before authentication: it exposes nothing.
        if classification.intent is Intent.END_CALL:
            return self._simple(
                Intent.END_CALL, Domain.UNKNOWN, speech.GOODBYE_SPEECH, success=True
            )

        # The scope gate runs before anything else looks at the request. A
        # question outside this bank's services never reaches a tool, an
        # agent, or the caller's data — whoever is asking and however it is
        # phrased. Refusing here rather than downstream is what stops a
        # general-knowledge answer from ever being composed.
        #
        # Imported here, not at module scope: `app.scope` reads this package's
        # intent classifier, so a top-level import would close a cycle the
        # moment anything reaches `app.scope` first. Python caches the module,
        # so this costs a dictionary lookup per turn.
        from app.scope import classify_scope

        decision = classify_scope(
            text,
            authenticated=bool(session and session.authenticated),
            customer_id=session.customer_id if session else None,
            current_domain=current_domain,
        )
        if not decision.allowed:
            return AgentResponse(
                agent=self.name,
                intent=Intent.UNKNOWN,
                domain=Domain.UNKNOWN,
                speech=decision.speech,
                success=False,
                reason=decision.category.value,
            )

        # Courtesy is answered as courtesy, before anything asks who is calling.
        # Saying "thank you" is not a request for banking data, and replying to
        # it with "may I have your customer ID" is the mechanical behaviour this
        # whole refinement exists to remove.
        # Only for a call that still exists. A caller whose session has gone
        # needs to be told to start again, not greeted as though nothing
        # happened — being friendly about a dead line is its own kind of rude.
        social = social_turn(text) if session is not None else None
        if social is not None:
            return self._simple(
                Intent.UNKNOWN,
                Domain.UNKNOWN,
                speech.YOU_ARE_WELCOME_SPEECH
                if social == THANKS
                else speech.GREETING_SPEECH,
                success=True,
                reason=social,
            )

        try:
            context = require_authenticated_customer(session_id, manager=manager)
        except AuthorizationError as error:
            # Hold on to what they asked for, so that once they are verified the
            # bank answers that question rather than asking them to repeat it.
            # Only the enquiry and the account or loan type are kept — never who
            # they claimed to be, and never anything they said.
            self._remember_pending(manager, session, classification, error.reason)
            return AgentResponse(
                agent=self.name,
                intent=classification.intent,
                domain=classification.domain,
                speech=speech.refusal(error.reason),
                success=False,
                reason=error.reason,
                data=error.to_dict(),
                requires_authentication=True,
            )

        classification = self._resume_pending(context.session, classification, text)

        if classification.intent is Intent.UNKNOWN:
            response = self._unknown(classification)
            self._record(manager, session_id, classification, response)
            return response

        classification = self._carry_selection(context.session, classification)

        agent = self._agents[DOMAIN_BY_INTENT[classification.intent]]
        response = agent.handle(session_id, classification, manager=manager)
        self._record(manager, session_id, classification, response)
        return response

    # --- internals --------------------------------------------------------

    def _simple(
        self,
        intent: Intent,
        domain: Domain,
        sentence: str,
        *,
        success: bool,
        reason: str | None = None,
    ) -> AgentResponse:
        return AgentResponse(
            agent=self.name,
            intent=intent,
            domain=domain,
            speech=sentence,
            success=success,
            reason=reason,
        )

    def _unknown(self, classification: Classification) -> AgentResponse:
        """Either ask which domain was meant, or say what can be done."""
        if classification.needs_domain:
            return self._simple(
                Intent.UNKNOWN,
                Domain.UNKNOWN,
                speech.DOMAIN_QUESTION,
                success=False,
                reason=DOMAIN_REQUIRED,
            )
        return self._simple(
            Intent.UNKNOWN,
            classification.domain,
            speech.FALLBACK_SPEECH,
            success=False,
            reason=NOT_UNDERSTOOD,
        )

    def _remember_pending(
        self,
        manager: SessionManager,
        session,
        classification: Classification,
        reason: str,
    ) -> None:
        """Keep an enquiry that only failed because nobody was verified yet."""
        if reason not in pending_request.AUTHENTICATION_REASONS:
            return
        if classification.intent is Intent.UNKNOWN:
            return

        tool = TOOL_BY_INTENT.get(classification.intent)
        if tool is None:
            return

        pending_request.remember(
            session,
            tool=tool,
            account_type=classification.account_type,
            loan_type=classification.loan_type,
            manager=manager,
        )

    def _resume_pending(
        self, session, classification: Classification, text: str
    ) -> Classification:
        """Treat a bare word as the answer to the question just asked.

        Two questions can be outstanding, and each has its own answer shape:

            "which account?"        -> "Savings"  -> resume the pending intent
            "account, or loan?"     -> "loan"     -> resume the pending action

        Both apply only when the turn was not understood on its own, so neither
        can redirect a request the caller stated clearly.
        """
        if classification.intent is not Intent.UNKNOWN:
            return classification

        resumed = self._resume_type_question(session, classification, text)
        if resumed is not None:
            return resumed

        resumed = self._resume_domain_question(session, classification)
        if resumed is not None:
            return resumed

        return classification

    def _resume_type_question(
        self, session, classification: Classification, text: str
    ) -> Classification | None:
        """Read "Savings" as the answer to "which account would you like?"."""
        pending = session.conversation_context.get(PENDING_INTENT_KEY)
        if pending is None:
            return None

        try:
            intent = Intent(pending)
        except ValueError:
            return None

        domain = DOMAIN_BY_INTENT[intent]
        value = parse_type_reply(text, domain)
        if value is None:
            return None

        return replace(
            classification,
            intent=intent,
            domain=domain,
            needs_domain=False,
            account_type=value if domain is Domain.ACCOUNT else None,
            loan_type=value if domain is Domain.LOAN else None,
        )

    def _resume_domain_question(
        self, session, classification: Classification
    ) -> Classification | None:
        """Read "loan" as the answer to "is that about your account, or loan?".

        The caller need only supply the missing half: the verb was understood on
        the earlier turn and was kept on the session, so "loan" completes the
        original "what is my balance" rather than starting a new request.
        """
        pending = session.conversation_context.get(PENDING_ACTION_KEY)
        if pending is None or classification.domain is Domain.UNKNOWN:
            return None

        try:
            action = Action(pending)
        except ValueError:
            return None

        intent = INTENT_BY_DOMAIN_ACTION.get((classification.domain, action))
        if intent is None:
            return None

        return replace(classification, intent=intent, needs_domain=False, action=action)

    def _carry_selection(
        self, session, classification: Classification
    ) -> Classification:
        """Stay on the account or loan this call is already discussing.

        The rule lives in `app.agents.selection`, so the realtime voice agent
        carries a selection exactly the same way this text mode does.
        """
        domain = DOMAIN_BY_INTENT[classification.intent]

        if domain is Domain.ACCOUNT:
            carried = carry_type(session, domain, classification.account_type)
            if carried != classification.account_type:
                return replace(classification, account_type=carried)

        if domain is Domain.LOAN:
            carried = carry_type(session, domain, classification.loan_type)
            if carried != classification.loan_type:
                return replace(classification, loan_type=carried)

        return classification

    def _record(
        self,
        manager: SessionManager,
        session_id: str,
        classification: Classification,
        response: AgentResponse,
    ) -> None:
        """Store the turn's intent, and any outstanding question, on the session.

        The session is re-read first: the tool layer may already have updated
        `current_domain` and the selected account or loan during this turn, and
        that must not be overwritten.
        """
        session = manager.get_session(session_id)
        if session is None:
            return

        # At most one question is ever outstanding, so recording either kind
        # clears the other.
        context = dict(session.conversation_context)
        context.pop(PENDING_INTENT_KEY, None)
        context.pop(PENDING_ACTION_KEY, None)

        if response.reason in CLARIFICATION_REASONS:
            context[PENDING_INTENT_KEY] = classification.intent.value
        elif response.reason == DOMAIN_REQUIRED and classification.action is not None:
            context[PENDING_ACTION_KEY] = classification.action.value

        try:
            manager.update_session(
                session_id,
                previous_intent=classification.intent.value,
                conversation_context=context,
            )
        except SessionNotFoundError:
            # The call ended while this turn was being answered. Nothing to keep.
            return


# Shared supervisor for the running process. It holds no customer state — only
# the two domain agents, which are themselves stateless.
supervisor = SupervisorAgent()
