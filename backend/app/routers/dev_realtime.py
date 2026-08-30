"""DEVELOPMENT-ONLY realtime voice endpoints.

These manage the lifecycle of a voice call only. Audio does not flow through
them: the Python process holds a WebSocket to OpenAI, and Phase 10 will add the
browser transport. Streaming audio over ordinary REST would be the wrong shape.

No response here ever contains the OpenAI API key, a PIN, or customer data.

This router must not be exposed in a production deployment, and since Phase 7.2
it is not: `main.create_app` only mounts the development routers when `APP_ENV`
is not production.

**It shares the application's one capacity ceiling.** It used to hold its own
`RealtimeManager`, which meant a process could carry twice the configured number
of provider sessions and readiness would report half of them. Whatever this
router opens now counts against the same pool as a browser call and a telephone
call, because there is only one pool.
"""

from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.realtime import RealtimeSessionError
from app.realtime.browser_calls import voice_call_manager
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
        "active_calls": voice_call_manager.active_count(),
    }


@router.post("/session/{session_id}/start", status_code=status.HTTP_201_CREATED)
async def start_realtime_session(session_id: str) -> dict:
    """Open a voice call on an existing banking session.

    The connector is named rather than left to the manager's default. The
    shared manager's default belongs to the browser channel, and a development
    call wants the server-side session this process holds - saying so is both
    correct and the one line a reader has to check to know what this opens.
    """
    from app.realtime.realtime_manager import open_openai_session

    try:
        connection = await voice_call_manager.start(
            session_id, connect=open_openai_session
        )
    except RealtimeSessionError as error:
        raise _http_error(error) from error
    return connection.to_safe_dict()


@router.get("/session/{session_id}")
def read_realtime_session(session_id: str) -> dict:
    """Report whether a voice call is live on this banking session."""
    connection = voice_call_manager.get(session_id)
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
    closed = await voice_call_manager.close(session_id)
    return {"session_id": session_id, "closed": closed}
