"""In-memory session store.

Version 1 keeps sessions in a dictionary inside the FastAPI process. Redis is
deliberately not used yet; it belongs to a later scaling phase.

Missing-session convention:
    * `get_session()` returns None.
    * `require_session()` raises SessionNotFoundError.
    * `update_session()` raises SessionNotFoundError (it must not silently
      discard an update).
    * `destroy_session()` returns False, so ending a call twice is harmless.
"""

import threading

from app.sessions.session import Session, SessionStatus

# Fields callers may change. session_id and created_at are deliberately absent:
# a session's identity and creation time must never be reassigned.
UPDATABLE_FIELDS = frozenset(
    {
        "customer_id",
        "candidate_customer_id",
        "authenticated",
        "authentication_attempts",
        "authentication_locked",
        "current_domain",
        "previous_intent",
        "conversation_context",
        "realtime_session_id",
        "status",
    }
)


class SessionNotFoundError(KeyError):
    """Raised when a session id does not match an active session."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id

    def __str__(self) -> str:
        return f"No active session with id {self.session_id!r}"


class SessionManager:
    """Thread-safe in-memory store of active sessions.

    A `threading.RLock` guards the store. FastAPI runs synchronous endpoints in
    a worker thread pool, so a thread lock (rather than an asyncio lock)
    protects both synchronous and asynchronous callers. The critical sections
    are tiny dictionary operations, so contention is negligible.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # --- creation ---------------------------------------------------------

    def create_session(self) -> Session:
        """Create, store and return a new independent ACTIVE session."""
        session = Session()
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    # --- retrieval --------------------------------------------------------

    def get_session(self, session_id: str) -> Session | None:
        """Return the active session, or None if there is no such session."""
        with self._lock:
            return self._sessions.get(session_id)

    def require_session(self, session_id: str) -> Session:
        """Return the active session, or raise SessionNotFoundError."""
        session = self.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def list_active_sessions(self) -> list[Session]:
        """Return all active sessions. The list is a copy; the sessions are live."""
        with self._lock:
            return list(self._sessions.values())

    def active_session_count(self) -> int:
        """Number of sessions currently held in the store."""
        with self._lock:
            return len(self._sessions)

    # --- mutation ---------------------------------------------------------

    def update_session(self, session_id: str, /, **updates) -> Session:
        """Apply field updates to one session and refresh `updated_at`.

        The target id is positional-only, so a caller passing
        `session_id="..."` as an update is rejected as a protected field
        rather than colliding with this method's own parameter.

        Raises SessionNotFoundError for an unknown id and ValueError for a
        field that is unknown or not permitted to change.
        """
        unknown = set(updates) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(
                f"Cannot update {sorted(unknown)}. "
                f"Updatable fields: {sorted(UPDATABLE_FIELDS)}."
            )

        with self._lock:
            session = self.require_session(session_id)
            for name, value in updates.items():
                # Copy dictionaries so a caller's dict cannot later be mutated
                # from outside and leak into this session.
                if isinstance(value, dict):
                    value = dict(value)
                setattr(session, name, value)
            session.touch()
            return session

    # --- teardown ---------------------------------------------------------

    def destroy_session(self, session_id: str) -> bool:
        """End a session and remove it from the store.

        Clears authentication state and conversation context, marks the session
        COMPLETED, then removes it. Returns False if the session was not found,
        so calling this twice is safe.
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False

            session.customer_id = None
            session.candidate_customer_id = None
            session.authenticated = False
            session.authentication_attempts = 0
            session.authentication_locked = False
            session.current_domain = None
            session.previous_intent = None
            session.conversation_context.clear()
            session.realtime_session_id = None
            session.status = SessionStatus.COMPLETED
            session.touch()
            return True

    def clear(self) -> None:
        """Remove every session. Intended for tests and local development."""
        with self._lock:
            self._sessions.clear()


# Shared store for the running FastAPI process. This holds sessions keyed by
# session id — it is never a place to put one customer's state.
session_manager = SessionManager()
