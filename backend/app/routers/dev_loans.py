"""DEVELOPMENT-ONLY loan endpoints.

The only identity input is the session id. There is deliberately no
`customer_id` parameter: the loan tools derive the customer from the
authenticated session.

Status mapping matches the account endpoints.
This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter, Query

from app.routers.error_mapping import respond as _respond
from app.tools import loans

router = APIRouter(prefix="/dev/loans", tags=["development"])


@router.get("/balance/{session_id}")
def read_loan_balance(
    session_id: str,
    loan_type: str | None = Query(default=None),
) -> dict:
    """Outstanding balance for the authenticated session's own loan."""
    return _respond(loans.get_loan_balance(session_id, loan_type))


@router.get("/instalment/{session_id}")
def read_next_instalment(
    session_id: str,
    loan_type: str | None = Query(default=None),
) -> dict:
    """Next instalment for the authenticated session's own loan."""
    return _respond(loans.get_next_instalment(session_id, loan_type))


@router.get("/details/{session_id}")
def read_loan_details(
    session_id: str,
    loan_type: str | None = Query(default=None),
) -> dict:
    """Safe loan details for the authenticated session's own loan."""
    return _respond(loans.get_loan_details(session_id, loan_type))
