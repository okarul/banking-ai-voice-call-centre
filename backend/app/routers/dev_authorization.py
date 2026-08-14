"""DEVELOPMENT-ONLY authorization diagnostics.

Reports whether a session may reach banking data, and nothing else: no PIN,
no hash, no balances, no account or loan values.
This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter

from app.authorization import AuthorizationError, require_authenticated_customer

router = APIRouter(prefix="/dev/authorization", tags=["development"])


@router.get("/check/{session_id}")
def check_authorization(session_id: str) -> dict:
    """Report the authorization decision for a session.

    Always 200: this is a diagnostic about a decision, not an attempt to reach
    protected data, so the outcome belongs in the body.
    """
    try:
        context = require_authenticated_customer(session_id)
    except AuthorizationError as error:
        return {"authorized": False, "reason": error.reason}

    return {"authorized": True, **context.to_dict()}
