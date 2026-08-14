"""The banking question a caller asked before they were verified.

A real contact centre does not make you say why you called twice. You say what
you want, you are verified, and the agent answers the thing you originally
asked. This module is what lets the demo do the same:

    "What is my savings balance?"   -> not verified yet, but remember the ask
    "DEMO001" ... PIN ...           -> verified
    -> answer the savings balance, without asking what they wanted again

What is remembered is deliberately tiny: **which enquiry, and which account or
loan type**. That is all that is needed to resume, and it is all that is safe.

Three things are never stored here:

* **No identity.** A pending request carries no customer id and cannot carry
  one. When it is resumed, the tool reads `session.customer_id` — the identity
  established by the deterministic PIN check, not anything remembered from
  before it. A request remembered while nobody was verified therefore cannot
  become a request for somebody else's money.
* **No PIN, and nothing the caller said.** Only an intent name and an account
  or loan type, both drawn from a fixed vocabulary.
* **Nothing outside this call.** It lives on the banking `Session`, so it dies
  with the session and can never be seen by another caller.

It is also cleared as soon as it is used, so a stale intent cannot answer a
later turn that the caller never made.
"""

from dataclasses import dataclass

from app.sessions import Session, SessionManager, SessionNotFoundError

PENDING_REQUEST_KEY = "pending_request"

# The enquiries worth resuming. Authentication tools are absent: "verify me" is
# not a question anyone needs answered twice, and a pending request must never
# be able to re-run the identity checks themselves.
RESUMABLE_TOOLS = frozenset(
    {
        "get_account_balance",
        "get_account_details",
        "get_recent_transactions",
        "get_loan_balance",
        "get_loan_details",
        "get_next_instalment",
    }
)

# Reasons that mean "ask them who they are, then come back to this" — as
# opposed to a refusal, which must never be resumed.
AUTHENTICATION_REASONS = frozenset({"NOT_AUTHENTICATED", "CUSTOMER_CONTEXT_MISSING"})


@dataclass(frozen=True)
class PendingRequest:
    """One enquiry, held across the authentication step and no longer."""

    tool: str
    account_type: str | None = None
    loan_type: str | None = None

    def to_dict(self) -> dict:
        """Safe view. Carries no identity, so it is safe to log and to return."""
        payload: dict = {"tool": self.tool}
        if self.account_type:
            payload["account_type"] = self.account_type
        if self.loan_type:
            payload["loan_type"] = self.loan_type
        return payload

    def arguments(self) -> dict:
        """The arguments to re-run the enquiry with."""
        if self.account_type:
            return {"account_type": self.account_type}
        if self.loan_type:
            return {"loan_type": self.loan_type}
        return {}


def _store(session: Session, manager: SessionManager | None, value) -> None:
    """Write the pending slot, through the manager when there is one."""
    context = dict(session.conversation_context)
    if value is None:
        context.pop(PENDING_REQUEST_KEY, None)
    else:
        context[PENDING_REQUEST_KEY] = value

    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return


def remember(
    session: Session | None,
    *,
    tool: str,
    account_type: str | None = None,
    loan_type: str | None = None,
    manager: SessionManager | None = None,
) -> None:
    """Hold on to the enquiry the caller made before they were verified.

    Ignored for anything not resumable, and ignored once the caller *is*
    verified — at that point the question is being answered, not deferred.
    """
    if session is None or tool not in RESUMABLE_TOOLS:
        return
    if session.authenticated:
        return

    _store(
        session,
        manager,
        {
            "tool": tool,
            "account_type": account_type or None,
            "loan_type": loan_type or None,
        },
    )


def recall(session: Session | None) -> PendingRequest | None:
    """The held enquiry, or None. Does not clear it."""
    if session is None:
        return None
    raw = session.conversation_context.get(PENDING_REQUEST_KEY)
    if not isinstance(raw, dict):
        return None
    tool = raw.get("tool")
    if tool not in RESUMABLE_TOOLS:
        return None
    return PendingRequest(
        tool=tool,
        account_type=raw.get("account_type"),
        loan_type=raw.get("loan_type"),
    )


def clear(session: Session | None, *, manager: SessionManager | None = None) -> None:
    """Forget the held enquiry."""
    if session is None:
        return
    _store(session, manager, None)


def take(
    session: Session | None, *, manager: SessionManager | None = None
) -> PendingRequest | None:
    """Return the held enquiry and forget it in one step.

    Taking rather than reading is the safer default: an intent that has been
    handed back has been acted on, and leaving it in place would let it answer
    a later turn the caller never made.
    """
    pending = recall(session)
    if pending is not None:
        clear(session, manager=manager)
    return pending
