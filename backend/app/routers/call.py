"""The browser's telephone: the only endpoints the customer page may call.

Three verbs, matching the three things a handset does — pick it up, ask
something, put it down:

    POST /api/call/start   create the banking session, mint a browser credential
    POST /api/call/tool    run one banking tool the model asked for
    POST /api/call/end     hang up and clean everything away

`/dev/*` stays where it is, for developers. This router is deliberately small
and deliberately separate: it is the one surface a customer's browser touches.

Two rules shape every handler here:

* **Identity is the session, never the payload.** `/tool` takes the session id
  the page was given when its own call started, and rejects a payload that
  tries to name a customer. The model chooses `account_type` and `limit`;
  it does not choose whose account.
* **Nothing leaks outward.** No handler returns the permanent API key, a PIN,
  a database detail or a provider error string. Failures become short,
  customer-safe sentences.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.registry import FORBIDDEN_ARGUMENTS
from app.observability import recorder
from app.realtime.browser_calls import browser_call_manager, sweep_idle_calls
from app.realtime.realtime_manager import Reason, RealtimeSessionError
from app.realtime.turn_gate import record_decision
from app.realtime.webrtc import TOOLS_BY_NAME, execute_tool, mint_client_secret
from app.scope import classify_scope
from app.sessions import session_manager

logger = logging.getLogger("app.call")

router = APIRouter(prefix="/api/call", tags=["call"])

UNAVAILABLE = "Unable to connect to voice banking. Please try again."
NO_SUCH_SESSION = "This banking session is no longer active."

# Said when every voice line is in use. A contact-centre sentence: it tells the
# customer what to do and nothing about why. No provider, no limit, no error.
BUSY = "Voice banking is temporarily busy. Please try again shortly."

# Why a call finished, as the dashboard reports it. The page names one of
# these; anything it invents is recorded as a plain customer hang-up rather
# than trusted into the operational record.
END_REASONS = frozenset(
    {
        "CUSTOMER_ENDED",
        "VOICE_END_CALL",
        "SILENCE_TIMEOUT",
        "MAX_DURATION",
        "NETWORK_FAILURE",
        "PROVIDER_FAILURE",
        "ERROR",
    }
)


def _END_REASONS(reason: str) -> str:
    return reason if reason in END_REASONS else "CUSTOMER_ENDED"


class ToolRequest(BaseModel):
    """One tool call the model made during a browser call."""

    session_id: str = Field(..., description="The call's banking session id")
    name: str = Field(..., description="Tool the model asked for")
    arguments: dict = Field(default_factory=dict, description="Model arguments")


class EndRequest(BaseModel):
    """A hang-up."""

    session_id: str = Field(..., description="The call's banking session id")


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_call() -> dict:
    """Begin a call: a fresh banking session and a short-lived credential.

    The session is created unauthenticated. Who is calling is settled by voice,
    through the same deterministic checks every other interface uses — this
    endpoint takes no customer id and cannot be asked for one.
    """
    await sweep_idle_calls()

    session = session_manager.create_session()

    # The capacity slot is claimed *before* the credential is minted, and the
    # claim is atomic. Two callers pressing Start in the same instant cannot
    # both be admitted, and a caller who is turned away has not cost a paid
    # provider request. The reservation is consumed by `start()` below.
    try:
        await browser_call_manager.reserve(session.session_id)
    except RealtimeSessionError as error:
        session_manager.destroy_session(session.session_id)
        logger.warning("browser call refused: %s", error.reason)
        if error.reason == Reason.REALTIME_AT_CAPACITY:
            # Recorded so the operator can see that somebody was turned away.
            # No banking session survives this path, so the row has no customer.
            await asyncio.to_thread(recorder.record_rejection)
        detail = BUSY if error.reason == Reason.REALTIME_AT_CAPACITY else UNAVAILABLE
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        )

    try:
        secret = await mint_client_secret()
    except RealtimeSessionError as error:
        # Leave nothing half-open behind a failed connection, and give the slot
        # straight back so the next caller can have it.
        await browser_call_manager.release(session.session_id)
        session_manager.destroy_session(session.session_id)
        logger.error("browser call could not start: %s", error.reason)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE
        )

    try:
        connection = await browser_call_manager.start(session.session_id, reserved=True)
    except RealtimeSessionError as error:
        # Fail closed: no half-open call, no orphaned banking session, and no
        # reuse of anyone else's connection. `start()` has already released the
        # reservation. The caller starts again cleanly.
        session_manager.destroy_session(session.session_id)
        logger.warning("browser call could not start: %s", error.reason)
        detail = BUSY if error.reason == Reason.REALTIME_AT_CAPACITY else UNAVAILABLE
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        )

    # The operational record. Written off the event loop, and unable to fail
    # the call: if it cannot be written the customer still gets their line.
    await asyncio.to_thread(recorder.start_session, session.session_id)

    logger.info("browser call started on %s", session.session_id)
    return {
        "session_id": session.session_id,
        "realtime_session_id": connection.realtime_session_id,
        "client_secret": secret,
    }


@router.post("/tool")
async def post_tool(payload: ToolRequest) -> dict:
    """Run one banking tool on behalf of a live browser call."""
    if session_manager.get_session(payload.session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_SUCH_SESSION
        )

    if payload.name not in TOOLS_BY_NAME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="UNKNOWN_TOOL"
        )

    # No schema declares these, so a payload carrying one is either a confused
    # model or an attempt to choose a customer. Refuse either way.
    if set(payload.arguments) & FORBIDDEN_ARGUMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="IDENTITY_NOT_ACCEPTED"
        )

    # No bookkeeping here any more. `execute_tool` reaches the same tool
    # objects the telephone agent is given, and those now record the
    # invocation and the resulting identity themselves — see
    # `app.observability.business`. Recording again at this end would count
    # every browser tool call twice and write the identity twice, which is
    # exactly the channel-specific duplication that moving the mirror down was
    # meant to remove.
    result = await execute_tool(payload.name, payload.session_id, payload.arguments)

    return {"result": result}


@router.post("/end")
async def end_call(payload: EndRequest, reason: str = "CUSTOMER_ENDED") -> dict:
    """Hang up: close the call and end the banking session behind it.

    Safe to call repeatedly and safe to call for a session that never existed —
    a handset that is already down cannot be put down wrongly. The second call
    simply reports that there was nothing left to do.
    """
    call_closed = await browser_call_manager.close(payload.session_id)
    session_ended = session_manager.destroy_session(payload.session_id)

    if call_closed or session_ended:
        logger.info("browser call ended on %s", payload.session_id)
        # Every way a call can finish arrives here, so this is the one place the
        # end time and the reason have to be written.
        await asyncio.to_thread(
            recorder.end_session, payload.session_id, reason=_END_REASONS(reason)
        )

    return {
        "success": True,
        "call_closed": call_closed,
        "session_ended": session_ended,
    }


class TranscriptRequest(BaseModel):
    """One line the assistant spoke, for the operator's transcript."""

    session_id: str = Field(..., description="The call's banking session id")
    text: str = Field(..., description="What the assistant said")


@router.post("/transcript")
def post_transcript(payload: TranscriptRequest) -> dict:
    """Record the assistant's own words for the operations dashboard.

    Only the assistant's side comes through here. The caller's words reach the
    transcript via `/scope`, where they are redacted — this endpoint exists so
    the operator can read the conversation as a conversation, not so the page
    can write arbitrary text into an audit table, which is why the content is
    redacted here too.
    """
    if session_manager.get_session(payload.session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_SUCH_SESSION
        )

    recorder.record_message(payload.session_id, role="AGENT", content=payload.text)
    return {"success": True}


class ScopeRequest(BaseModel):
    """One finalised caller utterance, for the scope gate to rule on."""

    session_id: str = Field(..., description="The call's banking session id")
    transcript: str = Field(..., description="What the caller just said")


@router.post("/scope")
def check_scope(payload: ScopeRequest) -> dict:
    """Decide whether the bank may answer this turn at all.

    The page asks before letting the model reply, so a question outside this
    bank's services is refused by Python rather than by a prompt the model may
    or may not follow.

    The transcript is used and discarded. It is never logged and never stored:
    on the authentication turns it contains the caller's spoken PIN.
    """
    session = session_manager.get_session(payload.session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_SUCH_SESSION
        )

    decision = classify_scope(
        payload.transcript,
        authenticated=session.authenticated,
        customer_id=session.customer_id,
        current_domain=session.current_domain,
    )

    # Remember the ruling for this turn. The page is expected to act on it, but
    # the page is the customer's browser and cannot be the only thing enforcing
    # it: recording it here means a tool call the model makes anyway is refused
    # server-side, before any banking data is read.
    record_decision(session, decision)

    # The turn's domain and intent are written by `record_decision` above,
    # which both channels reach — so recording them again here would give the
    # browser two writes per turn and the telephone none.
    #
    # The caller's line still goes to the transcript from here, redacted
    # before it is stored, because this is the endpoint the authentication
    # turns come through and this is the browser's only path to it.
    recorder.record_message(
        payload.session_id, role="CUSTOMER", content=payload.transcript
    )

    # Category only — the decision carries none of the caller's words.
    logger.info(
        "scope session=%s category=%s allowed=%s",
        payload.session_id,
        decision.category.value,
        decision.allowed,
    )
    return decision.to_dict()


@router.get("/active")
def active_calls() -> dict:
    """How many calls and sessions are live, for concurrency checks.

    Counts only. No session ids, no customer ids, nothing about anyone — this
    answers "are both callers still connected, and did hanging up clean up?"
    and nothing else.
    """
    return {
        "active_banking_sessions": session_manager.active_session_count(),
        "active_browser_calls": browser_call_manager.active_count(),
    }


@router.get("/state/{session_id}")
def call_state(session_id: str) -> dict:
    """Safe status for the page's debug panel.

    Carries no PIN, no hash, no credential and no account data — only whether
    the caller got through and what the conversation is currently about.
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_SUCH_SESSION
        )

    return {
        "session_id": session.session_id,
        "authenticated": session.authenticated,
        "customer_id": session.customer_id,
        "current_domain": session.current_domain,
        "call_active": browser_call_manager.is_active(session_id),
        "active_calls": browser_call_manager.active_count(),
    }
