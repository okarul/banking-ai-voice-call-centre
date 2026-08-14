"""DEVELOPMENT-ONLY session endpoints.

Thin wiring only: every decision lives in the SessionManager. Responses carry
session metadata only — no PINs, no hashes, no balances, no loan data.
This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, HTTPException, status

from app.sessions import session_manager

router = APIRouter(prefix="/dev/sessions", tags=["development"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_session() -> dict:
    """Create a new isolated session."""
    session = session_manager.create_session()
    return {
        "session_id": session.session_id,
        "authenticated": session.authenticated,
        "status": session.status.value,
    }


@router.get("")
def count_sessions() -> dict:
    """Report how many sessions are currently active."""
    return {"active_sessions": session_manager.active_session_count()}


@router.get("/{session_id}")
def read_session(session_id: str) -> dict:
    """Return safe metadata for one active session."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    return session.to_safe_dict()


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    """End a session and remove it from the active store."""
    if not session_manager.destroy_session(session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    return {"session_id": session_id, "destroyed": True}
