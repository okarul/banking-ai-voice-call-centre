"""The Phase 11 scope gate, enforced in front of every banking-data tool.

Scope is already decided deterministically in `app.scope`, and the Phase 8 text
agent consults it before answering. The voice paths did not. The model heard the
caller, chose a tool, and the tool ran — so a cross-customer comparison like

    "Who has more in savings, me or DEMO002?"

fetched the caller's *own* balance before the model decided, correctly, to
refuse out loud. The spoken refusal was right; the retrieval behind it was not.
Reading protected data in order to answer a question about somebody else is
still reading protected data, and no prompt can be trusted to prevent it.

This module puts the same Python decision in front of the tools themselves:

    caller turn -> transcript -> classify_scope -> gate -> tool -> guard -> DB

Three things feed the gate, so every interface is covered by one rule:

* the server-side voice path, from `input_audio_transcription_completed`
* text turns, from the user's own history item
* the browser, from `POST /api/call/scope`

The decision lives on the banking `Session`, never at module level, so two
concurrent calls can never share or overwrite each other's gate.

**Fail closed.** A banking-data tool that runs while the current turn is still
unclassified is refused rather than allowed. Measured on the live API, the
caller's transcript arrives about 1.2-1.5s *before* the model's first tool call,
so a turn that is still unclassified when a tool fires is an anomaly — and the
strict reading of the data policy is that an unclassified turn does not get to
read a balance.
"""

import asyncio
import time
from dataclasses import dataclass, field

from app.agents.intents import TOOL_BY_INTENT
from app.scope import ScopeCategory, ScopeDecision, classify_scope
from app.sessions import Session

# The tools that read protected customer money. Authentication tools are not
# here on purpose: checking *whether* the caller is verified reveals nothing
# about anybody's banking and stays available on every turn.
BANKING_DATA_TOOLS = frozenset(
    {
        "get_account_balance",
        "get_account_details",
        "get_recent_transactions",
        "get_loan_balance",
        "get_loan_details",
        "get_next_instalment",
    }
)

# Where the gate lives on the session's own context.
GATE_KEY = "scope_gate"

# How long a tool waits for the current turn to be classified before refusing.
# Comfortably longer than the measured transcript lead, short enough that a
# genuinely stuck turn fails fast rather than holding the call open.
TRANSCRIPT_WAIT_SECONDS = 4.0
_POLL_SECONDS = 0.05

# What a refused tool returns. The model reads `speech` out; it never receives
# a value it could compare, quote or infer from.
REASON_OUT_OF_SCOPE = "OUT_OF_SCOPE"
REASON_UNCLASSIFIED = "TURN_NOT_CLASSIFIED"

UNCLASSIFIED_SPEECH = (
    "I can only help with your own ABC Demo Bank account and loan enquiries. "
    "How can I assist with your banking today?"
)

# Turns that end any goodwill. A held enquiry is dropped when one of these
# arrives, so nothing can be resumed on the back of an attack.
HOSTILE_CATEGORIES = frozenset(
    {
        ScopeCategory.CROSS_CUSTOMER_REQUEST,
        ScopeCategory.SECURITY_OR_PROMPT_ATTACK,
    }
)


@dataclass
class TurnGate:
    """The scope ruling for the caller turn currently being answered.

    `pending` is set when a new caller turn begins and cleared when that turn is
    classified. A tool arriving while it is set waits for the ruling rather than
    racing it.
    """

    category: str | None = None
    allowed: bool = False
    speech: str | None = None
    pending: bool = False
    opened_at: float = field(default_factory=time.monotonic)

    def to_safe_dict(self) -> dict:
        """Category only. Never carries the caller's words."""
        return {
            "category": self.category,
            "allowed": self.allowed,
            "pending": self.pending,
        }


def _gate(session: Session | None) -> TurnGate | None:
    if session is None:
        return None
    gate = session.conversation_context.get(GATE_KEY)
    return gate if isinstance(gate, TurnGate) else None


def open_turn(session: Session | None) -> None:
    """A new caller turn has begun; its scope is not yet known.

    Called when the caller starts speaking. Until the transcript is classified,
    the previous turn's ruling must not authorise this one — an allowed balance
    question followed by a cross-customer question would otherwise be answered
    on the strength of the first.
    """
    if session is None:
        return
    session.conversation_context[GATE_KEY] = TurnGate(pending=True)


def record_decision(session: Session | None, decision: ScopeDecision) -> None:
    """Store an already-made scope ruling for the current turn."""
    if session is None:
        return
    session.conversation_context[GATE_KEY] = TurnGate(
        category=decision.category.value,
        allowed=decision.allowed,
        speech=decision.speech,
        pending=False,
    )

    # A caller who turns to another customer's money, or to attacking the
    # agent, forfeits any enquiry being held for them. Without this, the
    # resume exemption in `refusal_for` would still be open on that turn.
    if decision.category in HOSTILE_CATEGORIES:
        from app import pending_request

        pending_request.clear(session)
    else:
        _hold_unverified_enquiry(session, decision)

    # Deliberately **no database write here.** This function is called from
    # `RealtimeManager._feed_gate`, which runs inside the realtime pump's
    # `async for` — on the same event loop that paces this call's audio. A
    # PostgreSQL write costs 11-26ms there, against a gateway sending one
    # 160-byte frame every 20ms, so persisting inline stalled playback for
    # roughly a frame on every caller turn.
    #
    # The ruling itself stays synchronous, because scope enforcement must be
    # in force before the next tool call is admitted. Only the mirror moves:
    # each channel persists the returned decision from a context where
    # blocking is safe — `/scope` on its threadpool, the telephone through
    # `asyncio.to_thread` in the pump.


def _hold_unverified_enquiry(session: Session, decision: ScopeDecision) -> None:
    """Hold the enquiry an unverified caller just made, from the ruling itself.

    This is the deterministic half of the Phase 11 resume exemption, and the
    reason a live call can no longer answer the same question two different
    ways.

    The enquiry used to be remembered only as a *side effect of the model
    reaching for a tool too early*: a banking tool that failed
    `NOT_AUTHENTICATED` recorded what had been asked on its way out. That works
    when the model calls `get_account_balance` before verifying. It does not
    work when the model does what its own instructions ask and calls
    `get_authentication_status` first — a valid ordering that reaches no banking
    tool at all, so nothing was ever held. Verification then finished on a turn
    whose transcript is a spoken PIN, which classifies as NON_BANKING_REQUEST,
    and the very lookup the caller rang about was refused `OUT_OF_SCOPE` with
    the database never touched. Same caller, same PIN, same question, same
    database — two different answers, decided by which tool the model happened
    to reach for first.

    So the enquiry is taken from the caller's own classified turn instead. The
    caller said what they wanted; the server heard it, classified it in plain
    Python, and now writes it down. Whether the model then calls a tool, calls a
    different tool, or calls none at all cannot change it.

    Nothing here relaxes the gate:

    * It runs only while nobody is verified. `pending_request.remember` re-checks
      that, so a verified caller can never accumulate a held enquiry.
    * It records only a tool name and an account or loan type, both from a fixed
      vocabulary. No identity, no PIN, nothing the caller said.
    * The turn it is drawn from was itself in scope — a hostile or
      cross-customer turn takes the branch above and clears the hold instead.
    * The tool that eventually resumes still reads `session.customer_id` and
      still passes every authorization guard.
    """
    if session.authenticated:
        return

    tool = TOOL_BY_INTENT.get(decision.intent)
    if tool is None:
        # The turn named no specific enquiry — a greeting, a bare id, a PIN, or
        # a balance question with no account or loan named. Nothing to hold, and
        # deliberately nothing cleared: the enquiry held from an earlier turn is
        # what the authentication turns exist to get back to.
        return

    from app import pending_request

    pending_request.remember(
        session,
        tool=tool,
        account_type=decision.account_type,
        loan_type=decision.loan_type,
    )


def record_turn(session: Session | None, transcript: str) -> ScopeDecision | None:
    """Classify one caller utterance and store the ruling.

    The transcript is used and discarded. It is never stored on the session and
    never logged: on the authentication turns it contains the spoken PIN.
    """
    if session is None or not (transcript or "").strip():
        return None

    decision = classify_scope(
        transcript,
        authenticated=session.authenticated,
        customer_id=session.customer_id,
        current_domain=session.current_domain,
    )
    record_decision(session, decision)
    return decision


def refusal_for(session: Session | None, tool_name: str) -> dict | None:
    """The refusal this tool must return instead of running, or None to proceed.

    Synchronous and immediate: use `await wait_for_ruling(...)` first if the
    turn may still be unclassified.
    """
    if tool_name not in BANKING_DATA_TOOLS:
        return None

    gate = _gate(session)
    if gate is None:
        # No gate has ever been set on this session. Nothing has classified a
        # turn, which means no caller utterance reached the gate — the text
        # agent and both voice paths all record one, so this is the offline
        # case (a direct tool call in a test or a developer endpoint).
        return None

    if gate.pending:
        return {
            "success": False,
            "reason": REASON_UNCLASSIFIED,
            "speech": UNCLASSIFIED_SPEECH,
        }

    if gate.allowed:
        return None

    # One narrow exemption: finishing an enquiry the caller already made.
    #
    # A caller who asks for their balance before verifying is asked for their
    # ID and PIN. Those turns are not banking enquiries — a bare "4821" reads as
    # NON_BANKING_REQUEST — so the gate would refuse the very lookup the caller
    # rang about, immediately after verifying them for it.
    #
    # This is not a way past the gate. The held enquiry was itself allowed
    # through the gate on the turn it was made, it names one specific tool, it
    # carries no identity, and it is dropped the moment the caller turns to
    # anything cross-customer or hostile (see `record_decision`). The tool still
    # reads `session.customer_id` and still passes every authorization guard.
    from app import pending_request

    held = pending_request.recall(session)
    if held is not None and held.tool == tool_name:
        return None

    return {
        "success": False,
        "reason": REASON_OUT_OF_SCOPE,
        "category": gate.category,
        "speech": gate.speech or UNCLASSIFIED_SPEECH,
    }


async def wait_for_ruling(
    session: Session | None, *, timeout: float = TRANSCRIPT_WAIT_SECONDS
) -> None:
    """Wait until the current turn has been classified, or the wait runs out.

    Polls rather than waiting on an event because the ruling is written from
    three places — the realtime event pump, the text path and a FastAPI
    endpoint running on a worker thread — and a plain value carries no loop
    affinity. The wait normally ends immediately: the transcript is already in
    by the time the model reaches for a tool.
    """
    gate = _gate(session)
    if gate is None or not gate.pending:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
        gate = _gate(session)
        if gate is None or not gate.pending:
            return


def current_gate(session: Session | None) -> dict | None:
    """Safe view of the current ruling, for development endpoints."""
    gate = _gate(session)
    return gate.to_safe_dict() if gate else None


# Categories that must never reach a banking-data tool, named for tests and for
# anyone reading the policy rather than the code.
BLOCKED_CATEGORIES = frozenset(
    {
        ScopeCategory.CROSS_CUSTOMER_REQUEST,
        ScopeCategory.SECURITY_OR_PROMPT_ATTACK,
        ScopeCategory.NON_BANKING_REQUEST,
        ScopeCategory.UNSUPPORTED_BANKING_REQUEST,
    }
)
