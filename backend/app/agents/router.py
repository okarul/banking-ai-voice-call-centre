"""The one function the rest of the application calls to handle a turn.

Transport layers — today a development endpoint, later the voice layer — should
depend on this and not on the supervisor class, so the routing internals stay
free to change.
"""

from app.agents.base import AgentResponse
from app.agents.supervisor import supervisor as default_supervisor
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager


def handle_turn(
    session_id: str,
    text: str,
    *,
    manager: SessionManager = default_manager,
    supervisor=default_supervisor,
) -> AgentResponse:
    """Answer one caller utterance on one session."""
    return supervisor.handle_turn(session_id, text, manager=manager)
