"""What a realtime tool call is allowed to know about who is calling.

This object is handed to `RealtimeRunner.run(context=...)` and reaches tools as
`RunContextWrapper[BankingRealtimeContext]`. The Agents SDK excludes a
context-typed parameter from the JSON schema it shows the model, so the caller
identity travels beside the model's arguments and never through them.

That is the whole identity guarantee of Phase 9:

    banking session id -> context (server side) -> tool -> guard -> PostgreSQL

The model can say anything it likes about who it thinks it is talking to. It
cannot change this object, because it never sees it.
"""

from dataclasses import dataclass

from app.sessions import Session, SessionManager
from app.sessions import session_manager as default_manager


@dataclass(frozen=True)
class BankingRealtimeContext:
    """The banking session one realtime voice call is bound to.

    Frozen on purpose: a tool must not be able to repoint a live call at a
    different session. Holds no customer identity of its own — the customer is
    read from the session, which stays the source of truth.
    """

    session_id: str
    manager: SessionManager = default_manager

    def session(self) -> Session | None:
        """The live banking session, or None if it has ended."""
        return self.manager.get_session(self.session_id)

    def to_safe_dict(self) -> dict:
        """Safe view for logs. Never contains a PIN or an API key."""
        return {"session_id": self.session_id}
