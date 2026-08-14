"""DEVELOPMENT-ONLY agent endpoints.

The only identity input is the session id. There is deliberately no
`customer_id` parameter and no way to name a tool: the supervisor classifies the
text and the registry chooses the tool.

A turn always produces something to say, so an unauthenticated or misunderstood
turn is still a 200 carrying the sentence and a machine-readable reason. Only an
unknown session id is a 404, matching the session router.

This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agents import handle_turn, tool_schemas
from app.authorization import Reason
from app.sessions import session_manager

router = APIRouter(prefix="/dev/agents", tags=["development"])


class TurnRequest(BaseModel):
    """One spoken utterance on one session."""

    session_id: str = Field(..., description="Existing session id")
    text: str = Field(..., description="What the caller said")


@router.get("/tools")
def read_tools() -> dict:
    """The tool surface the agent layer may call.

    Note that no schema declares a session id or customer id: the caller of a
    tool never chooses whose data is read.
    """
    return {"tools": tool_schemas()}


@router.post("/turn")
def post_turn(payload: TurnRequest) -> dict:
    """Route one utterance and return what the agent would say."""
    if session_manager.get_session(payload.session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=Reason.SESSION_NOT_FOUND
        )

    return handle_turn(payload.session_id, payload.text).to_dict()
