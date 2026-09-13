"""The banking question a caller has half-finished, and the bank is owed.

Phase 7.3 gave this system one deterministic continuation: the enquiry a caller
made *before* they were verified is held in `app.pending_request` and answered
once they are. This module is the other half of the same idea, for the other way
a caller finishes a sentence — across a clarifying question:

    "What is my account balance?"   -> the bank asks: Savings or Current?
    "Savings"                       -> the enquiry is now complete

Between those two turns the backend already knows everything it needs. The
enquiry is `get_account_balance`, the turn was classified in scope, the caller
is verified (or about to be), and `parse_type_reply` resolves "Savings" to the
canonical `"Savings"` the tool layer itself uses.

Before Phase 7.4B none of that was written down. The ruling for the slot turn
carried `intent=UNKNOWN` and no account type, `pending_request` refuses to hold
anything once `session.authenticated` is true, and `remember_context` — the only
writer of `account_type` to the session — runs only when a tool *succeeds*,
which a clarification by definition did not. So whether the caller was answered
came down to whether the model happened to repeat a word they had already said.

**Three rules shape everything here.**

*One owner.* A clarification is opened in exactly one place — the tool layer,
when a banking tool reports a missing selection — and completed in exactly one
place: the scope ruling for the next caller turn. Nothing else writes it.

*A bare slot answer has no authority of its own.* "Savings" cannot start an
enquiry, name a tool, or widen what this turn may reach. It can only supply the
missing argument of a clarification **this session already had open**, and only
one whose domain matches. With nothing pending it means nothing at all, which is
what stops a clarification becoming a way past the scope gate.

*The completed request is still only a request.* Completing one produces
arguments, not an answer. What happens next goes through the ordinary path —
authentication, the scope gate, authorization, dispatch — exactly as it would
had the caller said the whole sentence at once. That is the invariant the phase
exists to establish: two ways of saying the same thing converge on the same
authorization decision and the same banking operation.

What is stored is deliberately tiny and carries no identity: a tool name, a
domain, the intent it serves, the choices the caller was offered, whatever
arguments were already validated, and — once given — the canonical slot value.
Never a customer id, never a PIN, never anything the caller said verbatim. It
lives on the banking `Session`, so it dies with the call and no other caller can
reach it.
"""

from dataclasses import dataclass, field

from app.agents.intents import TOOL_BY_INTENT, Domain
from app.sessions import Session, SessionManager, SessionNotFoundError

PENDING_CLARIFICATION_KEY = "pending_clarification"

# What a banking tool says when it needs the caller to choose. These are the
# only reasons that open a clarification: every other failure is a refusal, an
# outage or a not-found, and none of those is a question awaiting an answer.
ACCOUNT_TYPE_REQUIRED = "ACCOUNT_TYPE_REQUIRED"
LOAN_TYPE_REQUIRED = "LOAN_TYPE_REQUIRED"

DOMAIN_BY_REASON = {
    ACCOUNT_TYPE_REQUIRED: Domain.ACCOUNT,
    LOAN_TYPE_REQUIRED: Domain.LOAN,
}

CLARIFYING_REASONS = frozenset(DOMAIN_BY_REASON)

# Where the chosen value goes when the request is finally made.
ARGUMENT_BY_DOMAIN = {
    Domain.ACCOUNT: "account_type",
    Domain.LOAN: "loan_type",
}

# Which enquiry each tool serves. Derived from the one mapping that already
# exists rather than written out again, so a tool added to `TOOL_BY_INTENT` is
# covered here the moment it appears.
INTENT_BY_TOOL = {tool: intent for intent, tool in TOOL_BY_INTENT.items()}

# The enquiries a clarification may be opened for: the six that read customer
# banking data. Authentication tools are absent for the same reason they are
# absent from `pending_request.RESUMABLE_TOOLS` — a clarification must never be
# able to re-run an identity check.
CLARIFIABLE_TOOLS = frozenset(INTENT_BY_TOOL)


@dataclass(frozen=True)
class PendingClarification:
    """One enquiry waiting on one missing selection."""

    tool: str
    domain: Domain
    # The choices the caller was offered, so the bank can put them again
    # without asking the database a second time.
    choices: tuple[str, ...] = ()
    # Arguments already validated on the original request — the transaction
    # `limit`, say. Carried so a clarified request is the *same* request.
    arguments: dict = field(default_factory=dict)
    # The caller's answer, once they have given one. None while still owed.
    answer: str | None = None

    @property
    def complete(self) -> bool:
        """Whether the caller has supplied the missing selection."""
        return self.answer is not None

    @property
    def intent(self):
        """The enquiry this clarification serves."""
        return INTENT_BY_TOOL.get(self.tool)

    def completed_arguments(self) -> dict:
        """The arguments to make the original request with, now complete."""
        arguments = dict(self.arguments)
        if self.answer is not None:
            arguments[ARGUMENT_BY_DOMAIN[self.domain]] = self.answer
        return arguments

    def to_dict(self) -> dict:
        """Safe view. Carries no identity, so it is safe to log and to return."""
        payload: dict = {
            "tool": self.tool,
            "domain": self.domain.value,
            "awaiting": ARGUMENT_BY_DOMAIN[self.domain],
        }
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.answer:
            payload["answer"] = self.answer
        return payload


def _store(session: Session, manager: SessionManager | None, value) -> None:
    """Write the clarification slot, through the manager when there is one."""
    context = dict(session.conversation_context)
    if value is None:
        context.pop(PENDING_CLARIFICATION_KEY, None)
    else:
        context[PENDING_CLARIFICATION_KEY] = value

    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return


def open_for(
    session: Session | None,
    *,
    tool: str,
    reason: str,
    arguments: dict | None = None,
    choices=(),
    manager: SessionManager | None = None,
) -> None:
    """Record that the bank has asked this caller to choose, and why.

    Called from the tool layer at the one moment the question is actually put:
    a banking tool has reported that it cannot proceed without a selection. The
    caller has not been asked anything yet by the model, but the *bank* has
    decided to ask, and that decision is what is written down here.

    A new clarification replaces any earlier one. A caller who abandons "which
    account?" by asking about a loan instead is owed the loan question, not
    both.
    """
    if session is None or tool not in CLARIFIABLE_TOOLS:
        return
    domain = DOMAIN_BY_REASON.get(reason)
    if domain is None:
        return

    # Only arguments that are not the missing one, so completing it cannot be
    # overwritten by a stale value from the request that failed.
    carried = {
        name: value
        for name, value in (arguments or {}).items()
        if value is not None and name != ARGUMENT_BY_DOMAIN[domain]
    }

    _store(
        session,
        manager,
        {
            "tool": tool,
            "domain": domain.value,
            "choices": list(choices or ()),
            "arguments": carried,
            "answer": None,
        },
    )


def recall(session: Session | None) -> PendingClarification | None:
    """The clarification this call is waiting on, or None. Does not clear it."""
    if session is None:
        return None
    raw = session.conversation_context.get(PENDING_CLARIFICATION_KEY)
    if not isinstance(raw, dict):
        return None

    tool = raw.get("tool")
    if tool not in CLARIFIABLE_TOOLS:
        return None
    try:
        domain = Domain(raw.get("domain"))
    except ValueError:
        return None
    if domain not in ARGUMENT_BY_DOMAIN:
        return None

    arguments = raw.get("arguments")
    return PendingClarification(
        tool=tool,
        domain=domain,
        choices=tuple(raw.get("choices") or ()),
        arguments=dict(arguments) if isinstance(arguments, dict) else {},
        answer=raw.get("answer") or None,
    )


def awaiting_domain(session: Session | None) -> Domain | None:
    """Which selection this call is waiting for, if any.

    Read by the scope classifier so that a reply which only makes sense as an
    answer — "both", "what are my options" — is understood as one instead of
    being turned away as a change of subject. It reports the *shape* of the
    outstanding question and nothing about the caller.
    """
    pending = recall(session)
    if pending is None or pending.complete:
        return None
    return pending.domain


def complete_with(
    session: Session | None,
    domain: Domain,
    answer: str,
    *,
    manager: SessionManager | None = None,
) -> PendingClarification | None:
    """Supply the missing selection, if one was genuinely outstanding.

    This is the whole of a bare slot answer's authority, and it is deliberately
    narrow. The value is applied only when

    * a clarification is open on this session, and
    * it is waiting for a selection in **this** domain.

    Otherwise nothing happens and None is returned: "Savings" said out of the
    blue names no tool, starts no enquiry and reaches no data. The enquiry that
    gets completed is the one the bank itself asked about, never one the words
    could be read as suggesting.

    Returns the completed clarification, so the caller can act on it.
    """
    if session is None or not answer:
        return None

    pending = recall(session)
    if pending is None or pending.domain is not domain:
        return None

    completed = {
        "tool": pending.tool,
        "domain": pending.domain.value,
        "choices": list(pending.choices),
        "arguments": dict(pending.arguments),
        "answer": answer,
    }
    _store(session, manager, completed)
    return recall(session)


def resolve(session: Session | None, *, manager: SessionManager | None = None):
    """Answer a completed clarification now, without waiting to be asked.

    The deterministic half, and the reason this module exists rather than a
    tidier `_carried` lookup. Supplying the slot when the model happens to call
    the tool makes a clarified enquiry *correct*; it does not make it
    *certain*. Phase 7.3 established what the difference costs: two live calls
    verified perfectly, held an enquiry the backend had already classified and
    authorised, and then sat in silence because the model never made the call
    that would have answered it.

    So once the caller has answered the bank's question, the bank answers
    theirs. Returns the banking result, or None when there is nothing to do.

    **This is not a way past anything.** The request goes through
    `app.realtime.webrtc.execute_tool`, which is the same entry point both
    channels use for a model-initiated call: the scope gate, the authentication
    check, the ownership guards and the observability all run exactly as they
    would have. What is removed is the model's opportunity to forget, not any
    check.

    Unverified callers are left alone. Their enquiry belongs to
    `app.pending_request`, which holds it across the identity checks and hands
    the answer back with the verification - and running it here would only
    produce the `NOT_AUTHENTICATED` refusal that machinery exists to avoid.
    """
    if session is None or not session.authenticated:
        return None

    pending = recall(session)
    if pending is None or not pending.complete:
        return None

    import asyncio

    from app.realtime.webrtc import execute_tool

    try:
        return asyncio.run(
            execute_tool(
                pending.tool,
                session.session_id,
                pending.completed_arguments(),
                manager=manager,
            )
        )
    except Exception:
        # A clarified enquiry that could not be run is not a reason to break
        # the turn it arrived on. The clarification stays as it is, so the
        # model's own call still completes it through `_carried`.
        return None


def clear(session: Session | None, *, manager: SessionManager | None = None) -> None:
    """Forget the outstanding question. Safe when there is none."""
    if session is None:
        return
    if PENDING_CLARIFICATION_KEY not in session.conversation_context:
        return
    _store(session, manager, None)


def take(
    session: Session | None, *, manager: SessionManager | None = None
) -> PendingClarification | None:
    """Return the clarification and forget it in one step.

    Taking rather than reading is the safer default once it has been acted on:
    a completed clarification left in place would answer a later turn the caller
    never made.
    """
    pending = recall(session)
    if pending is not None:
        clear(session, manager=manager)
    return pending
