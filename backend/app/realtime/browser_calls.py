"""Tracking browser WebRTC calls with the Phase 9 call lifecycle.

A browser call has no provider session object on this side: the media path runs
browser <-> OpenAI, so there is nothing here to pump events from or to close
over the network. What there *is* is exactly the bookkeeping Phase 9 already
does — one call per banking session, a realtime id recorded on the session,
closing a call without destroying the session behind it.

Rather than write a second lifecycle that drifts from the first, a browser call
is registered in a `RealtimeManager` with a local stand-in for the provider
session. `active_count()`, `close()` and `close_all()` then mean the same thing
they meant in Phase 9.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.realtime.context import BankingRealtimeContext
from app.realtime.realtime_manager import RealtimeManager
from app.sessions import session_manager

logger = logging.getLogger("app.realtime.browser")

# A browser that closes without telling us leaves a call registered here. The
# page reports its own hang-ups, and this is the backstop for when it cannot.
IDLE_CALL_TIMEOUT = timedelta(minutes=30)


@dataclass
class BrowserCall:
    """Stand-in for the provider session on a browser-hosted call.

    The real connection is the page's `RTCPeerConnection`, which the page owns
    and closes. This object exists so the shared lifecycle has something with
    the shape it expects.
    """

    banking_session_id: str

    async def close(self) -> None:
        """Nothing to release on this side; the peer connection is the page's."""
        return None


async def open_browser_call(context: BankingRealtimeContext) -> BrowserCall:
    """Register a call whose audio is carried by the browser."""
    return BrowserCall(banking_session_id=context.session_id)


# Browser calls are tracked separately from server-side voice calls: they are a
# different transport with a different owner, and mixing them would make
# `active_count()` ambiguous.
browser_call_manager = RealtimeManager(connect=open_browser_call)

# The same object under a channel-neutral name.
#
# This one manager is the application-wide voice ceiling: every live call, on
# any channel, is registered here, so `used_capacity()` is the whole bank's
# usage rather than one channel's. Its original name predates the telephone
# channel, and renaming it would touch the working browser path for cosmetic
# reasons — so new code refers to it by what it now is.
#
# A telephone call passes its own connector to `start(connect=...)`, because it
# needs a real server-side model session where a browser call needs only a
# stand-in. Same ceiling, same lifecycle, different transport.
voice_call_manager = browser_call_manager


def _last_touched(session) -> datetime:
    stamp = getattr(session, "updated_at", None) or getattr(session, "created_at", None)
    if stamp is None:
        return datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


async def sweep_idle_calls(*, timeout: timedelta = IDLE_CALL_TIMEOUT) -> int:
    """End calls whose browser is gone, and the sessions behind them.

    A refreshed or crashed tab cannot always report its own hang-up, so the
    server does not depend on it: a call whose banking session has vanished, or
    which has sat untouched past the timeout, is closed here. Returns how many
    were reclaimed.
    """
    now = datetime.now(timezone.utc)
    reclaimed = 0

    for banking_session_id in browser_call_manager.active_session_ids():
        session = session_manager.get_session(banking_session_id)
        stale = session is None or now - _last_touched(session) > timeout
        if not stale:
            continue

        await browser_call_manager.close(banking_session_id)
        session_manager.destroy_session(banking_session_id)
        reclaimed += 1
        logger.info("reclaimed abandoned browser call on %s", banking_session_id)

    return reclaimed
