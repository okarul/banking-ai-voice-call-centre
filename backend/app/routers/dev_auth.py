"""DEVELOPMENT-ONLY authentication endpoints.

Thin wiring: the route hands raw text to the authentication service and returns
its structured result. Responses carry no PIN and no PIN hash.
This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import authentication

router = APIRouter(prefix="/dev/auth", tags=["development"])


class CustomerIdRequest(BaseModel):
    session_id: str = Field(..., description="Existing session id")
    customer_id: str = Field(..., description="Spoken or typed customer id")


class PinRequest(BaseModel):
    session_id: str = Field(..., description="Existing session id")
    pin: str = Field(..., description="Spoken or typed four-digit PIN")


@router.post("/customer")
def authenticate_customer(payload: CustomerIdRequest) -> dict:
    """Normalise and verify a customer id. Never authenticates on its own."""
    return authentication.submit_customer_id(payload.session_id, payload.customer_id)


@router.post("/pin")
def authenticate_pin(payload: PinRequest) -> dict:
    """Normalise and verify a PIN, updating the session's auth state."""
    return authentication.submit_pin(payload.session_id, payload.pin)


@router.get("/status/{session_id}")
def read_authentication_status(session_id: str) -> dict:
    """Safe authentication view of one session."""
    result = authentication.authentication_status(session_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    return result
