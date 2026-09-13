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

# Where the answer the backend already read for the held enquiry is kept, so
# the same operation is never run against the bank twice. It lives beside the
# enquiry deliberately: an answer to a question nobody is holding any more is
# not an answer to anything, so the two are created, replaced and forgotten
# together and cannot drift apart.
RESUMED_ANSWER_KEY = "resumed_answer"

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

    # A new enquiry replaces the old one, so any answer read for the old one is
    # no longer an answer to what is being held.
    forget_answer(session, manager=manager)

    _store(
        session,
        manager,
        {
            "tool": tool,
            "account_type": account_type or None,
            "loan_type": loan_type or None,
        },
    )


def supply_missing_type(
    session: Session | None,
    domain,
    value: str,
    *,
    manager: SessionManager | None = None,
) -> PendingRequest | None:
    """Fill in the account or loan a held enquiry never named.

    The other half of a clarification that happens *before* anybody is
    verified. "What is my account balance?" is held here the moment it is
    classified, with no account on it, because the caller had not said one. If
    they then answer the bank's clarifying question — or simply volunteer
    "Savings" — while still unverified, that answer belongs to the enquiry
    already being held for them.

    Without this the hold stayed empty, Phase 7.3's post-verification resume ran
    the enquiry with no account, and a caller who had said which account twice
    was asked a third time by the bank's own deterministic machinery. There is
    no model anywhere in that sequence, which is what made it the clearest case
    in the phase.

    Deliberately narrow, and every bound matters:

    * only while unverified — once the caller is verified,
      `app.pending_clarification` owns the outstanding question, and mutating a
      hold whose answer may already be cached would let the two disagree;
    * only when the held enquiry is in the same domain as the answer, so a
      "Savings" can never land on a loan enquiry;
    * only when the slot is genuinely empty, so this can correct nothing the
      caller already said and can never redirect a request that was complete.
    """
    if session is None or not value or session.authenticated:
        return None

    held = recall(session)
    if held is None:
        return None

    from app.pending_clarification import ARGUMENT_BY_DOMAIN

    field = ARGUMENT_BY_DOMAIN.get(domain)
    if field is None:
        return None
    # The held enquiry must be asking for this kind of thing at all.
    if field == "account_type" and not held.tool.startswith("get_account"):
        if held.tool != "get_recent_transactions":
            return None
    if field == "loan_type" and not held.tool.startswith("get_loan"):
        if held.tool != "get_next_instalment":
            return None
    if getattr(held, field) is not None:
        return None

    # A different answer is a different question; the answer read for the empty
    # one is no longer an answer to what is held.
    forget_answer(session, manager=manager)
    _store(
        session,
        manager,
        {
            "tool": held.tool,
            "account_type": value if field == "account_type" else held.account_type,
            "loan_type": value if field == "loan_type" else held.loan_type,
        },
    )
    return recall(session)


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


def _apply(
    session: Session, manager: SessionManager | None, context: dict
) -> None:
    """Write a whole conversation context back, through the manager if there is one."""
    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return


def _canonical(value):
    """One argument, in the form the tool layer already compares it in.

    `_resolve_account` and `_resolve_loan` both select on `strip().lower()`, so
    "Savings", "savings" and " savings " name the same account and must not
    look like three different questions. Anything that is not text - the
    transaction `limit` - is compared as it stands.

    This can only ever merge two spellings the bank itself treats as one, so it
    widens what counts as the same question without widening what counts as the
    same answer.
    """
    return value.strip().lower() if isinstance(value, str) else value


def _answer_key(tool: str, arguments: dict | None) -> list:
    """What makes one resumed answer the answer to one exact question.

    The tool alone is not enough, and assuming it was produced a wrong-data
    defect: a resumed Savings balance was handed back to a follow-up asking
    about a different account, so a caller would have been told the balance of
    an account that does not exist. `test_a_refusal_says_which_refusal_it_was`
    caught it.

    Only validated arguments go in, in the tool layer's own canonical form (see
    `_canonical`). Identity never does - the tools resolve the customer from the
    authenticated session, so a cache key that mentioned a customer would be
    describing something the caller cannot choose anyway.

    An argument that was not given is absent from the key rather than present
    and null, so "my balance" and "my Savings balance" stay two questions.
    """
    normalised = {
        name: _canonical(value)
        for name, value in (arguments or {}).items()
        if value is not None
    }
    return [tool, sorted(normalised.items())]


def remember_answer(
    session: Session | None,
    *,
    tool: str,
    arguments: dict | None,
    result,
    manager: SessionManager | None = None,
) -> None:
    """Keep what the bank just said, against the exact question that was asked.

    Written only by the deterministic post-verification resume, and only for a
    result that succeeded. A failure is not an answer: leaving one here would
    let a later attempt replay an outage as though it were the caller's balance.
    """
    if session is None or tool not in RESUMABLE_TOOLS:
        return
    context = dict(session.conversation_context)
    context[RESUMED_ANSWER_KEY] = {
        "key": _answer_key(tool, arguments),
        "result": result,
    }
    _apply(session, manager, context)


def answer_for(session: Session | None, tool: str, arguments: dict | None):
    """The answer already read for this exact question, or None.

    This is the exactly-once seam, and it is deliberately not an authorisation:
    callers reach it only after `_check_scope` has already allowed the tool.
    """
    if session is None:
        return None
    stored = session.conversation_context.get(RESUMED_ANSWER_KEY)
    if not isinstance(stored, dict):
        return None
    if stored.get("key") != _answer_key(tool, arguments):
        return None
    return stored.get("result")


def forget_answer(
    session: Session | None, *, manager: SessionManager | None = None
) -> None:
    """Drop the cached answer. Called wherever the held enquiry itself changes."""
    if session is None:
        return
    if RESUMED_ANSWER_KEY not in session.conversation_context:
        return
    context = dict(session.conversation_context)
    context.pop(RESUMED_ANSWER_KEY, None)
    _apply(session, manager, context)


def clear(session: Session | None, *, manager: SessionManager | None = None) -> None:
    """Forget the held enquiry, and the answer that belonged to it."""
    if session is None:
        return
    forget_answer(session, manager=manager)
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
