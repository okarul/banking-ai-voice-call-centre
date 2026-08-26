"""The tools the realtime voice agent is allowed to call.

Every tool here is a thin wrapper over work that already existed and was
already tested. Nothing re-implements banking logic:

    realtime tool -> app.agents.registry.dispatch -> app.tools -> guard -> DB
    realtime tool -> app.auth.authentication      -> normalization -> DB

Three properties are enforced by construction rather than by prompting:

* **Identity is not an argument.** The banking session id is read from the
  run context, which the model never sees and cannot write. No tool below
  declares a `session_id` or `customer_id` parameter, and the Phase 8 registry
  rejects one even if it somehow appeared.

* **Authentication is not a model decision.** `submit_customer_id` and
  `submit_pin` hand the spoken text to the existing deterministic verifiers and
  return only their verdict. The model reports that verdict; it cannot reach it.

* **Scope is not a model decision either.** Every tool that reads protected
  money passes through the Phase 11 gate first (`app.realtime.turn_gate`), so a
  question about somebody else is refused *before* anything is fetched — not
  fetched and then declined out loud.

The banking work is synchronous and talks to PostgreSQL, so each tool runs it
on a worker thread. Blocking the event loop would stall audio playback and make
the assistant sound like it is stuttering.
"""

import asyncio
import logging
import time

from agents import RunContextWrapper, function_tool
from sqlalchemy.exc import SQLAlchemyError

from app import pending_request
from app.agents.intents import Domain
from app.agents.registry import dispatch
from app.agents.selection import carry_type
from app.auth import authentication
from app.database.connection import DatabaseNotConfiguredError
from app.observability import business
from app.realtime.context import BankingRealtimeContext
from app.realtime.turn_gate import (
    refusal_for,
    resumes_held_enquiry,
    wait_for_ruling,
)
from app.sessions import SessionNotFoundError

logger = logging.getLogger("app.realtime.tools")

Ctx = RunContextWrapper[BankingRealtimeContext]

# Returned when the call is no longer attached to a live banking session.
SESSION_GONE = {"success": False, "reason": "SESSION_NOT_FOUND"}

# Failures the banking tools cannot report for themselves, because they arrive
# as exceptions rather than as results.
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
INTERNAL_TOOL_ERROR = "INTERNAL_TOOL_ERROR"

# The bank's records are unreachable: not configured, or PostgreSQL is down,
# refusing connections, or timing out. An operational fact about the estate,
# expected and transient, and nothing to do with this caller.
_RECORDS_UNREACHABLE = (DatabaseNotConfiguredError, SQLAlchemyError)


def _run_tool(tool_name: str, session_id: str, arguments: dict, manager) -> dict:
    """Run one registered tool, reporting a failure instead of raising.

    **Why this boundary exists at all.** Left to propagate, an exception out of
    a `@function_tool` body is caught by the Agents SDK
    (`agents.tool._FailureHandlingFunctionToolInvoker.__call__`), which hands it
    to `default_tool_error_function` and returns the result *to the model* as:

        "An error occurred while running the tool. Please try again. Error:
         {str(error)}"

    For the exception this path is most likely to see, `str(error)` is not a
    generic phrase. A SQLAlchemy `OperationalError` renders as the failing
    statement, its bound parameters and the database host:

        (builtins.Exception) connection to server at "127.0.0.1", port 5435
        failed: password authentication failed for user "postgres"
        [SQL: SELECT customers.pin_hash FROM customers WHERE ...]
        [parameters: {'id': 'DEMO001'}]

    Handing that to a language model on a live telephone call, under
    instructions to report what tools return, is an information-disclosure path
    into the audio. Two further things happen on that route, both bad: the
    invocation never reaches `business.record_tool_outcome`, so the operations
    board shows no tool event, no FAILED and no count for an enquiry the caller
    definitely made; and nothing is logged above debug, so the outage leaves no
    trace anywhere.

    So every exit from here is a structured result. `success` is `False` on all
    of them — this boundary can report a failure, and can never invent one that
    succeeded.

    **What is caught, and why.** The two operational cases are named, because
    they are facts about the estate rather than defects and neither deserves a
    traceback:

    * `SessionNotFoundError` — the call ended while the lookup was in flight.
      Reported as `SESSION_NOT_FOUND`, the reason this application already uses
      for a call that is no longer there.
    * `DatabaseNotConfiguredError`, `SQLAlchemyError` — the bank's records are
      unreachable. Reported as `DATABASE_UNAVAILABLE`.

    Everything else is a defect in the tool layer and is *unexpected by
    definition*, which is exactly why the last clause is broad: the set of ways
    Python code can be wrong is not enumerable, and a defect that escaped this
    function would take the SDK route above — telling the model, telling nobody
    else. It is classified `INTERNAL_TOOL_ERROR` and logged at ERROR with a
    traceback, so it is louder here than it was before, not quieter. The
    traceback is scrubbed by `app.redaction.RedactingFilter` before any handler
    formats it.

    `asyncio.CancelledError` derives from `BaseException` and is deliberately
    not caught, so a call being torn down still cancels rather than being
    recorded as a banking failure.

    Runs on a worker thread, via `asyncio.to_thread` in `_dispatch`.
    """
    try:
        return dispatch(tool_name, session_id, arguments, manager=manager)
    except SessionNotFoundError:
        # Not an error condition: the caller hung up mid-enquiry.
        logger.info("tool %s ran against a call that had ended", tool_name)
        return dict(SESSION_GONE)
    except _RECORDS_UNREACHABLE as error:
        # Expected and transient. Only the type name is logged: `str(error)`
        # for a SQLAlchemy failure carries the statement and its parameters.
        logger.error(
            "tool %s could not reach the database: %s",
            tool_name,
            type(error).__name__,
        )
        return {"success": False, "reason": DATABASE_UNAVAILABLE}
    except Exception as error:
        # A defect. Reported with a traceback, which carries file, line and
        # source — never an argument (one is a PIN) and never a result (one is
        # a balance). See the class list above for why this clause is broad.
        logger.error(
            "tool %s failed: %s", tool_name, type(error).__name__, exc_info=True
        )
        return {"success": False, "reason": INTERNAL_TOOL_ERROR}


def _binding(context: Ctx) -> tuple[str, object]:
    """The banking session id and store this call is bound to."""
    banking = context.context
    return banking.session_id, banking.manager


async def _dispatch(context: Ctx, tool_name: str, arguments: dict) -> dict:
    """Run a Phase 8 registered tool off the event loop.

    If the enquiry was refused only because the caller has not been verified
    yet, it is remembered — so once they are, the bank answers the question they
    actually asked instead of asking them what they wanted all over again. Only
    the enquiry and the account or loan type are kept; see
    `app.pending_request`.
    """
    session_id, manager = _binding(context)
    started = time.perf_counter()
    result = await asyncio.to_thread(
        _run_tool, tool_name, session_id, arguments, manager
    )
    # Recorded here rather than in either channel's caller: both the browser
    # route and the telephone agent reach this function, and mirroring it in
    # one of them was what left the telephone unobserved. Name, outcome and
    # duration only — never the arguments (one is a PIN) and never the result
    # (one is a balance).
    await asyncio.to_thread(
        business.record_tool_outcome,
        session_id,
        tool_name,
        result,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )

    if not isinstance(result, dict):
        return result

    if (
        result.get("success") is False
        and result.get("reason") in pending_request.AUTHENTICATION_REASONS
    ):
        session = await asyncio.to_thread(context.context.session)
        pending_request.remember(
            session,
            tool=tool_name,
            account_type=arguments.get("account_type"),
            loan_type=arguments.get("loan_type"),
            manager=manager,
        )
    elif result.get("success") and tool_name in pending_request.RESUMABLE_TOOLS:
        # Answered. Nothing is owed to the caller any more, and a held enquiry
        # left lying about would keep the gate exemption open on later turns.
        session = await asyncio.to_thread(context.context.session)
        pending_request.clear(session, manager=manager)

    return result


async def _mirror(session_id, manager, tool_name, result, started) -> None:
    """Record one authentication tool: the invocation, then the verdict.

    Separate from `_dispatch` because the authentication tools do not go
    through the Phase 8 registry — they call the deterministic verifiers
    directly — and they are also the only tools that can change who the caller
    is, which is a second thing worth writing down.
    """
    duration_ms = int((time.perf_counter() - started) * 1000)
    await asyncio.to_thread(
        business.record_tool_outcome,
        session_id,
        tool_name,
        result,
        duration_ms=duration_ms,
    )
    await asyncio.to_thread(business.record_identity, session_id, manager=manager)


async def _check_scope(context: Ctx, tool_name: str) -> dict | None:
    """The refusal this turn requires, or None if the tool may run.

    Consulted at the top of every protected tool, before arguments are resolved
    and before anything reaches the database — so a question about somebody else
    reads nothing at all, not even the caller's own balance to compare it with.
    """
    banking = context.context
    session = await asyncio.to_thread(banking.session)
    started = time.perf_counter()

    # An enquiry the bank already owes a verified caller is authorised by
    # server-side state alone, so waiting for this turn's ruling could not
    # change the answer — and waiting for a ruling that never comes is exactly
    # how a verified DEMO001 was told their own balance was unavailable, four
    # seconds after asking for it. See `turn_gate.resumes_held_enquiry`.
    if not resumes_held_enquiry(session, tool_name):
        await wait_for_ruling(session)
        # Re-read: the ruling for this turn may have landed while we waited.
        session = await asyncio.to_thread(banking.session)

    refusal = refusal_for(session, tool_name)
    if refusal is not None:
        # Recorded here because a scope refusal returns before `_dispatch`,
        # which is where every other invocation is counted. Left unrecorded,
        # the one enquiry an operator most needs to see — a verified caller
        # reaching for somebody else's money — was the only one that left no
        # trace, on either channel. The refusal dict carries `success: False`,
        # so it is counted as an invocation and recorded FAILED, never OK.
        session_id, _manager = _binding(context)
        await asyncio.to_thread(
            business.record_tool_outcome,
            session_id,
            tool_name,
            refusal,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    return refusal


async def _carried(
    context: Ctx, domain: Domain, stated: str | None, tool_name: str
) -> str | None:
    """Which account or loan this enquiry is about, when the model said none.

    Three sources, in order, and the caller's own words win outright:

    1. what the model passed, which is what the caller just said;
    2. the account or loan this call is already discussing;
    3. the account or loan named in the enquiry being held for this same tool.

    The third is what makes a resumed enquiry answer the question that was
    actually asked. "What is my savings balance?" is classified before the
    caller is verified, and "Savings" is recorded on the held enquiry. Several
    turns later the model resumes it — and if it omits `account_type`, the
    caller is asked which account they meant, having already said. The server
    heard them; they do not need to be asked twice.

    Read from a fixed vocabulary and scoped to the one tool the held enquiry
    names, so it can never redirect a different enquiry, and it carries no
    identity.
    """
    banking = context.context
    session = await asyncio.to_thread(banking.session)
    if session is None:
        return stated

    carried = carry_type(session, domain, stated)
    if carried is not None:
        return carried

    held = pending_request.recall(session)
    if held is None or held.tool != tool_name:
        return None
    return held.account_type if domain is Domain.ACCOUNT else held.loan_type


# --- authentication ---------------------------------------------------------


@function_tool
async def submit_customer_id(context: Ctx, spoken_customer_id: str) -> dict:
    """Check a customer identification number the caller has just spoken.

    Call this as soon as the caller says their demo customer ID. Pass exactly
    what you heard, such as "demo zero zero one"; the backend normalises it.
    This does not authenticate the caller: a PIN is still required.

    Args:
        spoken_customer_id: The customer ID exactly as the caller said it.
    """
    session_id, manager = _binding(context)
    started = time.perf_counter()
    result = await asyncio.to_thread(
        authentication.submit_customer_id,
        session_id,
        spoken_customer_id,
        manager=manager,
    )
    # The authentication tools do not pass through `_dispatch`, so they mirror
    # themselves. Identity is re-read from the session afterwards rather than
    # taken from `result`: a claim is not a verification.
    await _mirror(session_id, manager, "submit_customer_id", result, started)
    return result


@function_tool
async def submit_pin(context: Ctx, spoken_pin: str) -> dict:
    """Check the four-digit demo PIN the caller has just spoken.

    Never say the PIN back to the caller and never include it in any other
    message. Report only the outcome this tool returns. The backend decides
    whether the PIN is correct and how many attempts remain.

    Args:
        spoken_pin: The PIN exactly as the caller said it.
    """
    session_id, manager = _binding(context)
    # The spoken value is passed straight through and kept in no local state.
    started = time.perf_counter()
    result = await asyncio.to_thread(
        authentication.submit_pin, session_id, spoken_pin, manager=manager
    )
    await _mirror(session_id, manager, "submit_pin", result, started)

    # Verified — so if they told us what they wanted before we knew who they
    # were, hand that back now and let the caller be answered rather than
    # re-interviewed. The pending enquiry carries no identity: the tool that
    # runs next reads `session.customer_id`, which this check just established.
    # Read, do not consume. The held enquiry is what lets the scope gate admit
    # the follow-up lookup on this same turn — a bare spoken PIN is not itself a
    # banking enquiry — so it must still be there when that tool runs. It is
    # cleared once the enquiry has actually been answered.
    if isinstance(result, dict) and result.get("success"):
        session = await asyncio.to_thread(context.context.session)
        pending = pending_request.recall(session)
        if pending is not None:
            result = {**result, "pending_request": pending.to_dict()}

    return result


@function_tool
async def get_authentication_status(context: Ctx) -> dict:
    """Check whether this caller has been verified yet.

    Use this if you are unsure whether to ask for identification. Returns no
    PIN and no banking values.
    """
    session_id, manager = _binding(context)
    started = time.perf_counter()
    status = await asyncio.to_thread(
        authentication.authentication_status, session_id, manager=manager
    )
    result = status or SESSION_GONE
    # Counted like any other tool — an operator reading the board should see
    # that the agent asked — but it changes nothing, so no identity is written.
    await asyncio.to_thread(
        business.record_tool_outcome,
        session_id,
        "get_authentication_status",
        result,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


# --- account enquiries ------------------------------------------------------


@function_tool
async def get_account_balance(context: Ctx, account_type: str | None = None) -> dict:
    """Available balance on the verified caller's account.

    Args:
        account_type: "Savings" or "Current". Pass null if the caller did not
            say which; the account already being discussed is used, and if
            there is none the reply asks which they mean.
    """
    refusal = await _check_scope(context, "get_account_balance")
    if refusal is not None:
        return refusal
    account_type = await _carried(
        context, Domain.ACCOUNT, account_type, "get_account_balance"
    )
    return await _dispatch(
        context, "get_account_balance", {"account_type": account_type}
    )


@function_tool
async def get_account_details(context: Ctx, account_type: str | None = None) -> dict:
    """Type, masked number, balance and status of the caller's account.

    Args:
        account_type: "Savings" or "Current", or null if the caller did not say.
    """
    refusal = await _check_scope(context, "get_account_details")
    if refusal is not None:
        return refusal
    account_type = await _carried(
        context, Domain.ACCOUNT, account_type, "get_account_details"
    )
    return await _dispatch(
        context, "get_account_details", {"account_type": account_type}
    )


@function_tool
async def get_recent_transactions(
    context: Ctx, account_type: str | None = None, limit: int | None = None
) -> dict:
    """Most recent transactions on the caller's account, newest first.

    Amounts are signed: negative is money out, positive is money in. Read them
    back briefly — date, description and amount — and do not read any other
    field aloud.

    Args:
        account_type: "Savings" or "Current", or null if the caller did not say.
        limit: How many to return, 1 to 10. Pass null for the default of three.
    """
    refusal = await _check_scope(context, "get_recent_transactions")
    if refusal is not None:
        return refusal
    account_type = await _carried(
        context, Domain.ACCOUNT, account_type, "get_recent_transactions"
    )
    arguments: dict = {"account_type": account_type}
    if limit is not None:
        arguments["limit"] = limit
    return await _dispatch(context, "get_recent_transactions", arguments)


# --- loan enquiries ---------------------------------------------------------


@function_tool
async def get_loan_balance(context: Ctx, loan_type: str | None = None) -> dict:
    """Outstanding balance on the verified caller's loan.

    Args:
        loan_type: "Home Loan", "Personal Loan" or "Car Loan". Pass null if the
            caller did not say; the loan already being discussed is used.
    """
    refusal = await _check_scope(context, "get_loan_balance")
    if refusal is not None:
        return refusal
    loan_type = await _carried(context, Domain.LOAN, loan_type, "get_loan_balance")
    return await _dispatch(context, "get_loan_balance", {"loan_type": loan_type})


@function_tool
async def get_loan_details(context: Ctx, loan_type: str | None = None) -> dict:
    """Balance, interest rate, next instalment and maturity date of a loan.

    Args:
        loan_type: "Home Loan", "Personal Loan" or "Car Loan", or null.
    """
    refusal = await _check_scope(context, "get_loan_details")
    if refusal is not None:
        return refusal
    loan_type = await _carried(context, Domain.LOAN, loan_type, "get_loan_details")
    return await _dispatch(context, "get_loan_details", {"loan_type": loan_type})


@function_tool
async def get_next_instalment(context: Ctx, loan_type: str | None = None) -> dict:
    """Amount and date of the next instalment due on the caller's loan.

    Args:
        loan_type: "Home Loan", "Personal Loan" or "Car Loan", or null to use
            the loan already being discussed.
    """
    refusal = await _check_scope(context, "get_next_instalment")
    if refusal is not None:
        return refusal
    loan_type = await _carried(context, Domain.LOAN, loan_type, "get_next_instalment")
    return await _dispatch(context, "get_next_instalment", {"loan_type": loan_type})


AUTHENTICATION_TOOLS = [
    submit_customer_id,
    submit_pin,
    get_authentication_status,
]

ACCOUNT_TOOLS = [
    get_account_balance,
    get_account_details,
    get_recent_transactions,
]

LOAN_TOOLS = [
    get_loan_balance,
    get_loan_details,
    get_next_instalment,
]

BANKING_TOOLS = AUTHENTICATION_TOOLS + ACCOUNT_TOOLS + LOAN_TOOLS
