"""DEVELOPMENT-ONLY realtime voice endpoints.

These manage the lifecycle of a voice call only. Audio does not flow through
them: the Python process holds a WebSocket to OpenAI, and Phase 10 will add the
browser transport. Streaming audio over ordinary REST would be the wrong shape.

No response here ever contains the OpenAI API key, a PIN, or customer data.

This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.realtime import RealtimeSessionError, realtime_manager
from app.realtime.realtime_manager import Reason
from app.sessions import session_manager

router = APIRouter(prefix="/dev/realtime", tags=["development"])

STATUS_BY_REASON = {
    Reason.SESSION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    Reason.REALTIME_NOT_ACTIVE: status.HTTP_404_NOT_FOUND,
    Reason.REALTIME_ALREADY_ACTIVE: status.HTTP_409_CONFLICT,
    Reason.REALTIME_NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
    Reason.REALTIME_CONNECTION_FAILED: status.HTTP_502_BAD_GATEWAY,
}


def _http_error(error: RealtimeSessionError) -> HTTPException:
    return HTTPException(
        status_code=STATUS_BY_REASON.get(
            error.reason, status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        detail=error.to_dict(),
    )


@router.get("/status")
def read_status() -> dict:
    """Whether realtime voice is available, and how many calls are live.

    Reports only that a key is configured, never any part of the key itself.
    """
    return {
        "configured": settings.realtime_configured,
        "model": settings.realtime_model,
        "voice": settings.realtime_voice,
        "active_calls": realtime_manager.active_count(),
    }


@router.post("/session/{session_id}/start", status_code=status.HTTP_201_CREATED)
async def start_realtime_session(session_id: str) -> dict:
    """Open a voice call on an existing banking session."""
    try:
        connection = await realtime_manager.start(session_id)
    except RealtimeSessionError as error:
        raise _http_error(error) from error
    return connection.to_safe_dict()


@router.get("/session/{session_id}")
def read_realtime_session(session_id: str) -> dict:
    """Report whether a voice call is live on this banking session."""
    connection = realtime_manager.get(session_id)
    if connection is None:
        if session_manager.get_session(session_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"success": False, "reason": Reason.SESSION_NOT_FOUND},
            )
        return {"session_id": session_id, "realtime_session_id": None, "active": False}
    return connection.to_safe_dict()


@router.delete("/session/{session_id}")
async def close_realtime_session(session_id: str) -> dict:
    """End the voice call. The banking session itself is left untouched."""
    closed = await realtime_manager.close(session_id)
    return {"session_id": session_id, "closed": closed}
