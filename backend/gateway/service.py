"""The gateway itself: one call in, one call relayed, one call ended.

The whole shape of a call lives in `handle_call`, and it is deliberately linear:

    announce  ->  attach media  ->  relay both ways  ->  end

Every failure unwinds what the steps before it created, in reverse. A call that
cannot be announced never opens a socket; a call whose socket will not attach
is ended rather than left half-open; and the ending is reported exactly once
whatever route the call took to get there.

**One call, one everything.** A `MediaGateway` holds no per-call state: each
call's source, socket and relay tasks are local to the coroutine handling it.
Five simultaneous calls are five coroutines that share nothing, so audio has no
route from one to another — the same property the backend maintains on its
side, arrived at the same way.

Nothing here logs audio, and nothing logs the media credential.
"""

from __future__ import annotations

import asyncio
import logging

import websockets

from gateway.client import AcceptedCall, BackendClient, BackendUnavailable, CallRefused
from gateway.config import GatewaySettings, gateway_settings
from gateway.sources import CallSource

logger = logging.getLogger("gateway")


class CallOutcome:
    """How a call finished, as a category. Never a message."""

    COMPLETED = "completed"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class MediaGateway:
    """Relays telephone calls between a call source and the banking backend."""

    def __init__(
        self,
        settings: GatewaySettings | None = None,
        *,
        client: BackendClient | None = None,
    ) -> None:
        self._settings = settings or gateway_settings
        self._client = client or BackendClient(self._settings)
        # Counters only. No per-call state: a dictionary of live calls here
        # would be a route from one caller's audio to another's.
        self.completed = 0
        self.refused = 0
        self.failed = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def handle_call(self, source: CallSource) -> str:
        """Carry one telephone call from arrival to hang-up."""
        call_id = source.call_id

        try:
            accepted = await self._client.announce(call_id, caller=getattr(source, "caller", None))
        except CallRefused as refusal:
            # A normal answer, not an error. The caller hears busy; retrying
            # would not create capacity and they are waiting.
            self.refused += 1
            logger.info("gateway[%s] refused by the bank: %s", call_id, refusal.status)
            await source.close()
            return CallOutcome.REFUSED
        except BackendUnavailable as error:
            self.failed += 1
            logger.error("gateway[%s] bank unreachable: %s", call_id, error)
            await source.close()
            return CallOutcome.UNAVAILABLE

        try:
            socket = await self._client.open_media(accepted)
        except BackendUnavailable as error:
            # Announced but never attached. Report the ending so the bank
            # releases the slot now rather than at its attach timeout.
            self.failed += 1
            logger.error("gateway[%s] media not attached: %s", call_id, error)
            await source.close()
            await self._client.report_ended(call_id)
            return CallOutcome.UNAVAILABLE

        try:
            await self._relay(source, socket, accepted)
            self.completed += 1
            return CallOutcome.COMPLETED
        except Exception as error:
            self.failed += 1
            logger.error("gateway[%s] relay failed: %s", call_id, type(error).__name__)
            return CallOutcome.FAILED
        finally:
            # Every route out of a live call converges here, so the socket, the
            # source and the bank's record of the call are released exactly
            # once regardless of how it ended.
            await _quietly(socket.close())
            await source.close()
            await self._client.report_ended(call_id)

    # --- the relay -----------------------------------------------------------

    async def _relay(self, source: CallSource, socket, accepted: AcceptedCall) -> None:
        """Move frames both ways until either side stops.

        Two tasks, because audio has to flow in both directions at once, and
        the first to finish ends the call: a caller who hangs up should not
        wait on the bank, and a bank that closes the socket should not leave a
        caller listening to nothing.
        """
        to_bank = asyncio.create_task(
            self._caller_to_bank(source, socket), name=f"gw-in-{source.call_id}"
        )
        to_caller = asyncio.create_task(
            self._bank_to_caller(source, socket), name=f"gw-out-{source.call_id}"
        )

        done, pending = await asyncio.wait(
            {to_bank, to_caller}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            await _quietly(task)
        for task in done:
            # Surface a genuine failure; an ordinary end of call is not one.
            if task.exception() is not None:
                raise task.exception()

    async def _caller_to_bank(self, source: CallSource, socket) -> None:
        while True:
            frame = await source.receive_frame()
            if frame is None:
                return
            await socket.send(frame)

    async def _bank_to_caller(self, source: CallSource, socket) -> None:
        try:
            async for message in socket:
                # Binary only. The bank sends audio down this socket and
                # nothing else, so anything textual is not ours to interpret.
                if isinstance(message, bytes):
                    await source.send_frame(message)
        except websockets.exceptions.ConnectionClosed:
            # A socket that closes, tidily or not, is a call that has ended.
            # Callers hang up mid-sentence and networks drop; neither is a
            # relay failure, and counting them as one would report healthy
            # calls as errors.
            return


async def _quietly(awaitable) -> None:
    """Await something during teardown, where failing again changes nothing."""
    try:
        await awaitable
    except (asyncio.CancelledError, Exception):
        pass
