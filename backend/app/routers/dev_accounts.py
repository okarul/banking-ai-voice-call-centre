"""DEVELOPMENT-ONLY account endpoints.

The only identity input is the session id. There is deliberately no
`customer_id` parameter: the account tools derive the customer from the
authenticated session.

This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, Query

from app.routers.error_mapping import respond as _respond
from app.tools import accounts

router = APIRouter(prefix="/dev/accounts", tags=["development"])


@router.get("/balance/{session_id}")
def read_balance(
    session_id: str,
    account_type: str | None = Query(default=None),
) -> dict:
    """Available balance for the authenticated session's own account."""
    return _respond(accounts.get_account_balance(session_id, account_type))


@router.get("/details/{session_id}")
def read_details(
    session_id: str,
    account_type: str | None = Query(default=None),
) -> dict:
    """Safe account details for the authenticated session's own account."""
    return _respond(accounts.get_account_details(session_id, account_type))


@router.get("/transactions/{session_id}")
def read_transactions(
    session_id: str,
    account_type: str | None = Query(default=None),
    limit: int = Query(default=accounts.DEFAULT_TRANSACTION_LIMIT),
) -> dict:
    """Recent transactions, newest first, for the session's own account."""
    return _respond(
        accounts.get_recent_transactions(session_id, account_type, limit=limit)
    )
