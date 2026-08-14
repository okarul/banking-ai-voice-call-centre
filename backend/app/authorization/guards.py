"""Authorization guards.

Every route to banking data passes through here. The caller — today a test,
later an agent acting on a language model's output — never supplies the
customer identity: it is read from the validated session and nowhere else.

    caller -> tool -> guard -> validated session -> customer id -> repository

Guards raise AuthorizationError. The tool layer converts that into a structured
result; nothing propagates a stack trace outward.
"""

from dataclasses import dataclass

from app.authorization.errors import AuthorizationError, Reason
from app.sessions import Session, SessionManager, SessionStatus
from app.sessions import session_manager as default_manager


@dataclass(frozen=True)
class CustomerContext:
    """The authenticated identity for one session.

    Holds no credentials. `session` is the live session object for callers that
    need to record conversation context; it is never serialised.
    """

    session_id: str
    customer_id: str
    session: Session

    def to_dict(self) -> dict:
        """Safe view: identity only, no PIN, no hash, no banking values."""
        return {"session_id": self.session_id, "customer_id": self.customer_id}


def require_authenticated_customer(
    session_id: str,
    *,
    manager: SessionManager = default_manager,
) -> CustomerContext:
    """Validate a session and return its authenticated customer context.

    Checks run in this order so the most specific reason wins:
    exists -> active -> not locked -> authenticated -> customer present.
    No database query happens here; identity comes purely from session state.
    """
    session = manager.get_session(session_id)
    if session is None:
        raise AuthorizationError(Reason.SESSION_NOT_FOUND)

    if session.status is not SessionStatus.ACTIVE:
        raise AuthorizationError(Reason.SESSION_NOT_ACTIVE)

    if session.authentication_locked:
        raise AuthorizationError(Reason.AUTHENTICATION_LOCKED)

    if not session.authenticated:
        raise AuthorizationError(Reason.NOT_AUTHENTICATED)

    # Should not happen, but fail closed rather than querying with no identity.
    if not session.customer_id:
        raise AuthorizationError(Reason.CUSTOMER_CONTEXT_MISSING)

    return CustomerContext(
        session_id=session.session_id,
        customer_id=session.customer_id,
        session=session,
    )


def _owner_customer_id(record) -> str | None:
    """Public customer id that owns an account or loan row.

    Uses the ORM relationship rather than the numeric foreign key, because the
    session stores the public id ("DEMO001"). Returns None if the owner cannot
    be determined, which callers treat as "not owned".
    """
    customer = getattr(record, "customer", None)
    return getattr(customer, "customer_id", None)


def require_account_owned(context: CustomerContext, account) -> None:
    """Reject an account that does not belong to the context's customer."""
    if account is None or _owner_customer_id(account) != context.customer_id:
        raise AuthorizationError(Reason.ACCOUNT_NOT_OWNED)


def require_loan_owned(context: CustomerContext, loan) -> None:
    """Reject a loan that does not belong to the context's customer."""
    if loan is None or _owner_customer_id(loan) != context.customer_id:
        raise AuthorizationError(Reason.LOAN_NOT_OWNED)


def require_account_owned_by_session(
    session_id: str,
    account,
    *,
    manager: SessionManager = default_manager,
) -> CustomerContext:
    """Validate the session, then confirm it owns this account."""
    context = require_authenticated_customer(session_id, manager=manager)
    require_account_owned(context, account)
    return context


def require_loan_owned_by_session(
    session_id: str,
    loan,
    *,
    manager: SessionManager = default_manager,
) -> CustomerContext:
    """Validate the session, then confirm it owns this loan."""
    context = require_authenticated_customer(session_id, manager=manager)
    require_loan_owned(context, loan)
    return context
