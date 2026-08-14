"""Account service tools.

Customer identity is taken **only** from the authenticated session. None of
these functions accepts a customer id, so there is no code path by which a
caller can ask for somebody else's accounts:

    session_id -> SessionManager -> session.customer_id -> repository -> PostgreSQL

Money keeps its Decimal type all the way from PostgreSQL and is serialised as a
string, so no value ever passes through binary floating point.
"""

from app.authorization import AuthorizationError, Reason, require_account_owned
from app.database.connection import session_scope
from app.database.repositories import (
    get_accounts_for_customer,
    get_recent_transactions_for_account,
)
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager
from app.tools.common import authenticated_session as _authenticated_session
from app.tools.common import failure as _failure
from app.tools.common import money as _money
from app.tools.common import remember_context

DEFAULT_TRANSACTION_LIMIT = 3
MIN_TRANSACTION_LIMIT = 1
MAX_TRANSACTION_LIMIT = 10

CREDIT = "credit"


def _account_summary(account) -> dict:
    return {
        "account_type": account.account_type,
        "masked_account": account.account_number_masked,
        "available_balance": _money(account.available_balance),
        "currency": account.currency,
    }


def _remember_account_context(manager, context, account) -> None:
    """Record the selected account on this session only."""
    remember_context(
        manager, context.session, "ACCOUNT", "account_type", account.account_type
    )


def _resolve_account(context, account_type: str | None, db):
    """Pick the context customer's account. Returns (account, None) or (None, failure).

    Only accounts belonging to `context.customer_id` are ever considered, and
    the chosen row is re-checked against the session before being returned.
    """
    accounts = get_accounts_for_customer(db, context.customer_id)
    if not accounts:
        return None, _failure(Reason.ACCOUNT_NOT_FOUND)

    if account_type is not None:
        wanted = account_type.strip().lower()
        selected = next(
            (a for a in accounts if a.account_type.lower() == wanted), None
        )
        if selected is None:
            # Never silently substitute a different account.
            return None, _failure(
                Reason.ACCOUNT_NOT_FOUND,
                available_account_types=[a.account_type for a in accounts],
            )
    elif len(accounts) == 1:
        selected = accounts[0]
    else:
        # Several accounts and no choice given: ask rather than guess.
        return None, _failure(
            "ACCOUNT_TYPE_REQUIRED",
            available_account_types=[a.account_type for a in accounts],
        )

    # Defence in depth: confirm ownership of the row we are about to return.
    try:
        require_account_owned(context, selected)
    except AuthorizationError as error:
        return None, error.to_dict()

    return selected, None


def _validate_limit(limit: int) -> dict | None:
    if not isinstance(limit, int) or isinstance(limit, bool):
        return _failure("INVALID_LIMIT", min_limit=MIN_TRANSACTION_LIMIT,
                        max_limit=MAX_TRANSACTION_LIMIT)
    if limit < MIN_TRANSACTION_LIMIT or limit > MAX_TRANSACTION_LIMIT:
        return _failure(
            "INVALID_LIMIT",
            min_limit=MIN_TRANSACTION_LIMIT,
            max_limit=MAX_TRANSACTION_LIMIT,
        )
    return None


# --- tools ------------------------------------------------------------------


def get_account_balance(
    session_id: str,
    account_type: str | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Available balance for the authenticated customer's account."""
    context, error = _authenticated_session(session_id, manager)
    if error:
        return error

    with session_scope() as db:
        account, error = _resolve_account(context, account_type, db)
        if error:
            return error
        result = {"success": True, **_account_summary(account)}

    _remember_account_context(manager, context, account)
    return result


def get_account_details(
    session_id: str,
    account_type: str | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Safe account details for the authenticated customer's account."""
    context, error = _authenticated_session(session_id, manager)
    if error:
        return error

    with session_scope() as db:
        account, error = _resolve_account(context, account_type, db)
        if error:
            return error
        result = {
            "success": True,
            **_account_summary(account),
            "status": account.status.upper(),
        }

    _remember_account_context(manager, context, account)
    return result


def get_recent_transactions(
    session_id: str,
    account_type: str | None = None,
    limit: int = DEFAULT_TRANSACTION_LIMIT,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Most recent transactions, newest first, for the customer's account.

    Debits are returned as negative amounts and credits as positive, so the
    sign carries the direction without any float arithmetic.
    """
    context, error = _authenticated_session(session_id, manager)
    if error:
        return error

    limit_error = _validate_limit(limit)
    if limit_error:
        return limit_error

    with session_scope() as db:
        account, error = _resolve_account(context, account_type, db)
        if error:
            return error

        transactions = get_recent_transactions_for_account(db, account.id, limit=limit)
        result = {
            "success": True,
            "account_type": account.account_type,
            "masked_account": account.account_number_masked,
            "transactions": [
                {
                    "date": txn.transaction_date.date().isoformat(),
                    "description": txn.description,
                    "amount": _money(
                        txn.amount
                        if txn.transaction_type.lower() == CREDIT
                        else -txn.amount
                    ),
                    "transaction_type": txn.transaction_type.upper(),
                }
                for txn in transactions
            ],
        }

    _remember_account_context(manager, context, account)
    return result
