"""OpenAI Realtime voice as a second interface onto the banking agentic core.

Phase 8's developer text mode and this voice layer answer the same questions
through the same code:

    /dev/agents/turn ---+
                        |--> registry -> tools -> guards -> PostgreSQL
    realtime voice  ----+

Only the way a request arrives differs. The realtime agent's tools wrap the
Phase 8 registry rather than reimplementing any banking logic.

The OpenAI SDK is imported lazily inside `open_openai_session`, so importing
this package — as the whole test suite does — never requires the SDK, a network
or an API key.
"""

from app.realtime.context import BankingRealtimeContext
from app.realtime.events import describe_event, log_event, safe_arguments
from app.realtime.realtime_manager import (
    RealtimeConnection,
    RealtimeManager,
    RealtimeSessionError,
    Reason,
    new_realtime_session_id,
    open_openai_session,
    realtime_manager,
)

__all__ = [
    "BankingRealtimeContext",
    "RealtimeConnection",
    "RealtimeManager",
    "RealtimeSessionError",
    "Reason",
    "new_realtime_session_id",
    "open_openai_session",
    "realtime_manager",
    "describe_event",
    "log_event",
    "safe_arguments",
]
