"""Loan service tools.

Customer identity comes **only** from the authenticated session. None of these
functions accepts a customer id, so there is no code path by which a caller can
ask for somebody else's loans:

    session_id -> SessionManager -> session.customer_id -> repository -> PostgreSQL

Money and interest rates keep their Decimal type from PostgreSQL and are
serialised as strings, matching the account tools.
"""

from app.authorization import AuthorizationError, Reason, require_loan_owned
from app.database.connection import session_scope
from app.database.repositories import get_loans_for_customer
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager
from app.tools.common import authenticated_session, failure, money, remember_context


def _remember_loan_context(manager, context, loan) -> None:
    """Record the selected loan on this session only."""
    remember_context(manager, context.session, "LOAN", "loan_type", loan.loan_type)


def _loan_summary(loan) -> dict:
    return {
        "loan_type": loan.loan_type,
        "loan_reference": loan.loan_reference,
        "outstanding_balance": money(loan.outstanding_balance),
        "currency": loan.currency,
    }


def _resolve_loan(context, loan_type: str | None, db):
    """Pick the context customer's loan. Returns (loan, None) or (None, failure).

    Only loans belonging to `context.customer_id` are ever considered, and the
    chosen row is re-checked against the session before being returned.
    """
    loans = get_loans_for_customer(db, context.customer_id)
    if not loans:
        return None, failure(Reason.LOAN_NOT_FOUND)

    if loan_type is not None:
        wanted = loan_type.strip().lower()
        selected = next((l for l in loans if l.loan_type.lower() == wanted), None)
        if selected is None:
            # Never silently substitute a different loan.
            return None, failure(
                Reason.LOAN_NOT_FOUND,
                available_loan_types=[loan.loan_type for loan in loans],
            )
    elif len(loans) == 1:
        selected = loans[0]
    else:
        # Several loans and no choice given: ask rather than guess.
        return None, failure(
            "LOAN_TYPE_REQUIRED",
            available_loan_types=[loan.loan_type for loan in loans],
        )

    # Defence in depth: confirm ownership of the row we are about to return.
    try:
        require_loan_owned(context, selected)
    except AuthorizationError as error:
        return None, error.to_dict()

    return selected, None


# --- tools ------------------------------------------------------------------


def get_loan_balance(
    session_id: str,
    loan_type: str | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Outstanding balance for the authenticated customer's loan."""
    context, error = authenticated_session(session_id, manager)
    if error:
        return error

    with session_scope() as db:
        loan, error = _resolve_loan(context, loan_type, db)
        if error:
            return error
        result = {"success": True, **_loan_summary(loan)}

    _remember_loan_context(manager, context, loan)
    return result


def get_next_instalment(
    session_id: str,
    loan_type: str | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Next instalment amount and date for the authenticated customer's loan."""
    context, error = authenticated_session(session_id, manager)
    if error:
        return error

    with session_scope() as db:
        loan, error = _resolve_loan(context, loan_type, db)
        if error:
            return error
        result = {
            "success": True,
            "loan_type": loan.loan_type,
            "next_instalment_amount": money(loan.next_instalment_amount),
            "next_instalment_date": loan.next_instalment_date.isoformat(),
            "currency": loan.currency,
        }

    _remember_loan_context(manager, context, loan)
    return result


def get_loan_details(
    session_id: str,
    loan_type: str | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Safe loan details for the authenticated customer's loan."""
    context, error = authenticated_session(session_id, manager)
    if error:
        return error

    with session_scope() as db:
        loan, error = _resolve_loan(context, loan_type, db)
        if error:
            return error
        result = {
            "success": True,
            **_loan_summary(loan),
            "interest_rate": money(loan.interest_rate),
            "next_instalment_amount": money(loan.next_instalment_amount),
            "next_instalment_date": loan.next_instalment_date.isoformat(),
            "maturity_date": loan.maturity_date.isoformat(),
            "status": loan.status.upper(),
        }

    _remember_loan_context(manager, context, loan)
    return result
