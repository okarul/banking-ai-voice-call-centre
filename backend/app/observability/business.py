"""One place where a business event becomes a database row, for both channels.

Until Phase 6.9 this lived in `app/routers/call.py`. That worked for exactly as
long as every call was a browser call, because the browser round-trips each
tool through `POST /api/call/tool` and the endpoint mirrored the outcome on the
way past. The telephone does not: the Agents SDK runs the same tool objects
inside this process, so the router is never visited and nothing was written
down. A live call therefore came back `authenticated = false`,
`auth_status = PENDING`, `tool_call_count = 0` — for a call whose in-memory
session had authenticated perfectly well.

The mistake was where the mirror was attached, not what it did. Observability
was bolted to a *transport* when the thing worth observing is a *business
operation*. So it moves down to the boundary both channels genuinely share:

    browser  -> POST /api/call/tool -> webrtc.execute_tool ─┐
                                                            ├─> the same
    phone    -> Agents SDK ─────────────────────────────────┘   @function_tool
                                                                objects
                                                                     |
                                                                     v
                                                         these functions
                                                                     |
                                                                     v
                                                            one recorder call

`webrtc.TOOLS_BY_NAME` is built from `BANKING_TOOLS`, the very list handed to
the telephone agent, so both channels invoke the identical tool objects. That
is what makes a single mirror possible and what makes a second one a
duplicate: the browser route's own recording was removed in the same change
that added this, because leaving it would have counted every browser tool
twice.

Nothing here decides anything. It reads the verdict the banking session
already reached and writes it down. In particular `record_identity` reads
`session.authenticated` rather than a tool's return value, so a caller who
claimed an identity but failed the PIN can never be persisted as that customer.

**What is never written.** No tool arguments — one of them is a PIN. No tool
results — one of them is a balance. No transcript. Only the name of the tool,
whether it worked, and how long it took.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.observability import recorder, trace
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager

logger = logging.getLogger("app.observability.business")

# One stable string to alert on. A mirror that has silently stopped writing
# looks exactly like a channel that was never wired up, which is the whole of
# Phase 6.9 — so it must be greppable rather than inferred from missing rows.
MIRROR_FAILED = "OBSERVABILITY_MIRROR_FAILED"

# Failures that mean this module is wrong, as opposed to its dependencies being
# down. Reported with a traceback; everything else is treated as an outage.
_BUG = (AttributeError, TypeError, NameError, KeyError, IndexError, ImportError)


# How a scope category reads on an operations board. Moved here from the
# browser router so both channels label a turn the same way; a phone call and a
# browser call asking the same question must not show different domains.
_DASHBOARD_DOMAINS = {
    "AUTHENTICATION": "AUTHENTICATION",
    "OWN_ACCOUNT_ENQUIRY": "ACCOUNT",
    "OWN_TRANSACTION_ENQUIRY": "ACCOUNT",
    "OWN_LOAN_ENQUIRY": "LOAN",
    "SOCIAL": "CLOSING",
}

# Tools after which the authoritative identity may have changed.
AUTHENTICATION_TOOLS = {"submit_customer_id", "submit_pin"}


def dashboard_domain(category: str, current: str | None) -> str:
    """The domain column's value for this turn.

    Anything refused shows as GENERAL/SCOPE rather than as the banking domain
    it was pretending to be — an operator watching the board should see that a
    turn was turned away, not that a loan was discussed.
    """
    mapped = _DASHBOARD_DOMAINS.get(category)
    if mapped:
        return mapped
    return "GENERAL/SCOPE" if category else (current or "AUTHENTICATION")


def _never_fails(operation: str):
    """Decorator: log and swallow. Observability never breaks a call.

    The same guarantee `recorder._safe` gives, restated one layer up and for a
    sharper reason. These functions are called from inside a live call's tool
    path, between the banking work finishing and its answer being returned. An
    exception escaping here would turn a correct balance into a failed tool —
    an observability outage presenting to the customer as a banking outage.

    `recorder` guards its own writes, so this covers what is left: reading the
    session, mapping a category, and the thread hop itself.
    """

    def wrap(function):
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except _BUG as error:
                # A defect in this module, not an outage underneath it: a
                # renamed attribute, a changed signature, a wrong type. Still
                # swallowed — the banking answer is not the place to discover
                # it — but reported loudly, because the failure mode otherwise
                # is silence: every call recording nothing, for the same reason
                # Phase 6.9 existed.
                #
                # The traceback carries file, line and source, never values.
                # No argument is formatted into the message, because one of
                # them is a tool result.
                logger.error(
                    "%s %s failed: %s",
                    MIRROR_FAILED,
                    operation,
                    type(error).__name__,
                    exc_info=True,
                )
                return None
            except Exception as error:
                # Everything else is the database or the session store being
                # unavailable. Expected, transient, and not worth a traceback.
                logger.error(
                    "%s %s failed: %s", MIRROR_FAILED, operation, type(error).__name__
                )
                return None

        guarded.__name__ = function.__name__
        guarded.__doc__ = function.__doc__
        return guarded

    return wrap


def succeeded(result) -> bool:
    """Whether a tool result represents success, by the existing convention."""
    return not (isinstance(result, dict) and result.get("success") is False)


# One stable string to grep a failed banking enquiry by, alongside MIRROR_FAILED
# for a failed recording of one. Different things, and an operator chasing a
# customer complaint needs to tell them apart.
TOOL_FAILED = "BANKING_TOOL_FAILED"

# A scope refusal reports `OUT_OF_SCOPE` and puts *why* in `category`, so the
# reason on its own cannot distinguish "asked about the weather" from "asked
# about another customer's money". For an operations record they are not
# remotely the same event.
_SCOPE_REASON = "OUT_OF_SCOPE"


def failure_reason(result) -> str | None:
    """The most specific reason a tool failed, or None if it did not.

    Never the customer-facing sentence, and never a value: a reason code is a
    fixed string from a known set, so it is safe to log where a balance, an
    account number or a spoken PIN would not be.
    """
    if not isinstance(result, dict) or result.get("success") is not False:
        return None

    reason = result.get("reason")
    if reason == _SCOPE_REASON:
        # CROSS_CUSTOMER_REQUEST, SECURITY_OR_PROMPT_ATTACK, and the rest.
        return result.get("category") or reason
    return reason or "UNKNOWN"


@_never_fails("record_tool_outcome")
def record_tool_outcome(
    session_id: str,
    tool_name: str,
    result,
    *,
    duration_ms: int | None = None,
    arguments: dict | None = None,
    session=None,
) -> None:
    """Count one banking tool, exactly once, for whichever channel ran it.

    A refused tool is still an invocation and is still counted — an operator
    needs to see that the caller asked — but it is recorded as `FAILED`, so a
    refusal can never be read off the board as an answered enquiry.

    `agent_tool_events` has no column for *why* it failed, and adding one is a
    schema change this fix does not need. The reason is therefore kept in the
    log, where it is just as greppable and costs nothing: the row says an
    enquiry failed, the log line says whether the caller was unverified, asked
    about somebody else, named an account they do not hold, or found the bank's
    records unreachable. Collapsing those four into one silent FAILED is what
    made an intermittent live regression so slow to place.
    """
    reason = failure_reason(result)
    if reason is not None:
        logger.warning("%s tool=%s reason=%s", TOOL_FAILED, tool_name, reason)

    status = "OK" if succeeded(result) else "FAILED"
    recorder.record_tool_call(
        session_id,
        tool_name,
        status=status,
        duration_ms=duration_ms,
    )

    # The same invocation, told as a story rather than counted. Hung off this
    # function rather than off a second call site, so a tool can never be
    # counted once and traced twice - `tool_call_count` still comes from
    # `record_tool_call` alone and is unaffected by anything below.
    trace.record(
        session_id,
        trace.TraceEvent(
            kind=trace.KIND_TOOL,
            speaker=trace.SPEAKER_SYSTEM,
            tool_name=tool_name,
            tool_arguments=trace.sanitize_arguments(arguments),
            tool_status=status,
            failure_reason=reason,
            duration_ms=duration_ms,
            auth_status=trace.auth_status(session),
            customer_ref=trace.customer_ref(session),
        ),
        session=session,
    )


@_never_fails("record_identity")
def record_identity(
    session_id: str, *, manager: SessionManager = default_manager
) -> None:
    """Copy the *backend's* verdict on who is calling into the dashboard.

    Read from the session rather than from the tool result, because the session
    is the authority. A caller who merely claimed an identity has not
    established one, and the dashboard must not show an unverified claim as
    though it were a customer.
    """
    session = manager.get_session(session_id)
    if session is None:
        return
    recorder.record_authentication(
        session_id,
        customer_id=session.customer_id if session.authenticated else None,
        authenticated=bool(session.authenticated),
        locked=bool(session.authentication_locked),
        failed=bool(session.authentication_attempts) and not session.authenticated,
    )

    # The transition, in the trace, so a replay shows *when* the caller became
    # verified rather than only that they ended up so.
    trace.record(
        session_id,
        trace.TraceEvent(
            kind=trace.KIND_AUTH,
            speaker=trace.SPEAKER_SYSTEM,
            auth_status=trace.auth_status(session),
            customer_ref=trace.customer_ref(session),
        ),
        session=session,
    )


@_never_fails("record_turn_decision")
def record_turn_decision(
    session, decision, transcript: str | None = None, reserved: dict | None = None
) -> None:
    """Persist what this turn was about, for whichever channel classified it.

    Hung off the scope ruling rather than off a tool call, because a turn has a
    domain whether or not it reaches a tool: a refused cross-customer question
    runs no tool at all and is exactly the turn an operator most wants to see.
    """
    if session is None or decision is None:
        return
    category = getattr(getattr(decision, "category", None), "value", None)
    if not category:
        return
    recorder.record_turn(
        session.session_id,
        domain=dashboard_domain(category, session.current_domain),
        intent=category,
    )

    # And the same turn as a trace event: what was said (only if utterances are
    # switched on, and only redacted), what it was understood to be, what the
    # gate ruled, and what the caller is still owed.
    if not settings.trace_enabled:
        # Nothing below is read when the trace is off, and all of it costs
        # something: recalling the held enquiry, and classifying the turn a
        # second time to describe it. Per caller turn, on the audio path, for a
        # row that will not be written. Checked here rather than inside
        # `trace.record`, which is the last thing that runs.
        return

    from app import pending_request

    held = pending_request.recall(session)
    # What the turn *was*, told apart from what the gate *ruled*. See
    # `trace.describe_turn`: the ruling is kept exactly as made.
    expected = reserved["expected"] if reserved else ...
    described = trace.describe_turn(session, decision, transcript, expected)
    trace.record(
        session.session_id,
        trace.TraceEvent(
            kind=trace.KIND_TURN,
            speaker=trace.SPEAKER_CUSTOMER,
            turn=trace.open_turn(session),
            # The session and the ruling travel with the words, so a credential
            # the bank is expecting is replaced before anything is written -
            # whatever language it was transcribed into.
            utterance=trace.utterance_for(
                transcript,
                speaker=trace.SPEAKER_CUSTOMER,
                session=session,
                decision=decision,
                expected=expected,
            ),
            domain=described["domain"],
            intent=described["intent"],
            scope_category=category,
            scope_allowed=bool(getattr(decision, "allowed", False)),
            pending_operation=held.tool if held else None,
            account_type=held.account_type if held else None,
            loan_type=held.loan_type if held else None,
            auth_status=trace.auth_status(session),
            customer_ref=trace.customer_ref(session),
            event_type=described["event_type"],
        ),
        session=session,
        sequence=reserved["sequence"] if reserved else None,
    )
