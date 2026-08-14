"""The session model: one active banking conversation.

Every customer-specific value lives on a Session instance. Nothing about a
customer is ever stored at module level, so two callers can never see each
other's state.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SessionStatus(str, Enum):
    """Lifecycle states for a session."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"


def new_session_id() -> str:
    """Return a unique session identifier, e.g. SESSION-9f4c2e78-...."""
    return f"SESSION-{uuid.uuid4()}"


def utc_now() -> datetime:
    """Timezone-aware current time, used for all session timestamps."""
    return datetime.now(timezone.utc)


@dataclass
class Session:
    """State for a single customer conversation.

    `conversation_context` uses a default factory, so each session gets its own
    dictionary. A shared class-level default would leak one customer's context
    into another's session.
    """

    session_id: str = field(default_factory=new_session_id)
    # Only ever a customer who has passed the PIN check. A value the caller
    # merely claimed lives in `candidate_customer_id` until it is proven, so
    # everything downstream that trusts `customer_id` is trusting a verified
    # identity and nothing else.
    customer_id: str | None = None
    # The identifier being tried on this call. May be an id that does not
    # exist: nothing looks it up until a PIN arrives, which is what stops the
    # identification step from revealing who banks here.
    candidate_customer_id: str | None = None
    authenticated: bool = False
    authentication_attempts: int = 0
    # Set once the attempt limit is reached; blocks further PIN checks.
    authentication_locked: bool = False
    current_domain: str | None = None
    previous_intent: str | None = None
    conversation_context: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    status: SessionStatus = SessionStatus.ACTIVE
    # Reserved for a later phase. No OpenAI object is created here.
    realtime_session_id: str | None = None

    def touch(self) -> None:
        """Record that the session state just changed."""
        self.updated_at = utc_now()

    def to_safe_dict(self) -> dict:
        """Non-sensitive view of the session, safe for development endpoints.

        Contains no PINs, no hashes, no balances and no loan data — a session
        never holds any of those.
        """
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "authenticated": self.authenticated,
            "authentication_attempts": self.authentication_attempts,
            "authentication_locked": self.authentication_locked,
            "current_domain": self.current_domain,
            "previous_intent": self.previous_intent,
            "context_keys": sorted(self.conversation_context),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status.value,
        }
