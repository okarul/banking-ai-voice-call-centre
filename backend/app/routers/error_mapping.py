"""Domain reason -> HTTP status, shared by the development routers.

Kept in the transport layer so the authorization package stays free of HTTP
concerns. Clarification reasons (ACCOUNT_TYPE_REQUIRED, LOAN_TYPE_REQUIRED) are
deliberately absent: they are normal conversational branches returned as 200.
"""

from fastapi import HTTPException, status

from app.authorization import Reason

STATUS_BY_REASON = {
    Reason.SESSION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    Reason.SESSION_NOT_ACTIVE: status.HTTP_403_FORBIDDEN,
    Reason.NOT_AUTHENTICATED: status.HTTP_401_UNAUTHORIZED,
    Reason.AUTHENTICATION_LOCKED: status.HTTP_403_FORBIDDEN,
    Reason.CUSTOMER_CONTEXT_MISSING: status.HTTP_403_FORBIDDEN,
    Reason.ACCOUNT_NOT_OWNED: status.HTTP_403_FORBIDDEN,
    Reason.LOAN_NOT_OWNED: status.HTTP_403_FORBIDDEN,
    Reason.ACCOUNT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    Reason.LOAN_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    "INVALID_LIMIT": status.HTTP_400_BAD_REQUEST,
}


def respond(result: dict) -> dict:
    """Return a successful result, or raise the mapped HTTP error."""
    if result.get("success"):
        return result

    reason = result.get("reason", "")
    if reason in STATUS_BY_REASON:
        raise HTTPException(status_code=STATUS_BY_REASON[reason], detail=result)
    return result
