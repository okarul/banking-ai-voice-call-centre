"""Authorization domain errors.

A single exception type carrying a machine-readable `reason`. Messages are
short and generic on purpose: nothing here ever contains SQL, connection
details, table internals, PINs or hashes.
"""


class Reason:
    """Reasons an authorization decision can fail."""

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTHENTICATION_LOCKED = "AUTHENTICATION_LOCKED"
    CUSTOMER_CONTEXT_MISSING = "CUSTOMER_CONTEXT_MISSING"
    ACCOUNT_NOT_OWNED = "ACCOUNT_NOT_OWNED"
    LOAN_NOT_OWNED = "LOAN_NOT_OWNED"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    LOAN_NOT_FOUND = "LOAN_NOT_FOUND"


# Generic, caller-safe wording. Deliberately says nothing about which customer
# owns what, so a rejection reveals no information about other customers.
MESSAGES = {
    Reason.SESSION_NOT_FOUND: "No active session with that id.",
    Reason.SESSION_NOT_ACTIVE: "This session is no longer active.",
    Reason.NOT_AUTHENTICATED: "This session is not authenticated.",
    Reason.AUTHENTICATION_LOCKED: "Authentication is locked for this session.",
    Reason.CUSTOMER_CONTEXT_MISSING: "This session has no authenticated customer.",
    Reason.ACCOUNT_NOT_OWNED: "That account is not available for this session.",
    Reason.LOAN_NOT_OWNED: "That loan is not available for this session.",
    Reason.ACCOUNT_NOT_FOUND: "No matching account for this session.",
    Reason.LOAN_NOT_FOUND: "No matching loan for this session.",
}


class AuthorizationError(Exception):
    """Raised when a caller may not reach the banking data it asked for."""

    def __init__(self, reason: str, **details) -> None:
        self.reason = reason
        self.message = MESSAGES.get(reason, "Access denied.")
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Structured, caller-safe representation used by the tool layer."""
        return {"success": False, "reason": self.reason, **self.details}
