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
from gateway.control import (
    PLAYBACK_BOUNDARY,
    PROTOCOL_HELLO,
    playback_drained_message,
    protocol_ready_message,
    read_control_message,
    read_protocol_message,
)
from gateway.sources import FRAME_BYTES, SILENCE, CallSource

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
        try:
            while True:
                frame = await source.receive_frame()
                if frame is None:
                    return
                await socket.send(frame)
        except websockets.exceptions.ConnectionClosed:
            # The bank closed the socket because the call is over — it has said
            # goodbye and torn the call down. Whichever direction notices first
            # is a race, and only the other one handled it, so a perfectly
            # normal ending was logged as "relay failed: ConnectionClosedOK"
            # and counted against the gateway. Returning ends the relay exactly
            # as the far direction already does; nothing else changes.
            return

    async def _bank_to_caller(self, source: CallSource, socket) -> None:
        """Assistant audio, repacketised to 20 ms frames and paced in real time.

        Two things a WebSocket does not do for us, and RTP requires both.

        **Packetisation.** The bank may deliver several 20 ms telephone frames
        in one WebSocket message, or split one across two. RTP carries exactly
        one 160-byte PCMU payload per packet, so the stream is buffered and cut
        on frame boundaries rather than forwarded message-for-message. Sending
        a whole message as one frame puts a payload of the wrong length on the
        wire, which a carrier either drops or plays as a burst of noise.

        **Pacing.** A telephone plays 50 frames a second and no faster. The
        model generates far quicker than that, so frames handed over as fast as
        they arrive arrive faster than they can be played — and the far end,
        having no jitter buffer for a flood, discards the overflow. The caller
        hears the beginning of a sentence and then silence.

        When the loop falls behind, `next_send` is reset to now rather than
        allowed to run in the past. Catching up would mean bursting audio that
        is already stale, which is the same failure again with worse timing.
        """
        buffer = bytearray()
        frame_interval = 0.020
        loop = asyncio.get_running_loop()
        next_send = loop.time()

        try:
            async for message in socket:
                if not isinstance(message, bytes):
                    # Text is control. This loop is serial, so every binary
                    # message before it has already been paced out below at
                    # telephone rate — except for a final partial frame, which
                    # the boundary handler plays before answering.
                    next_send = await self._boundary_reached(
                        socket, source, message, buffer, next_send
                    )
                    continue

                buffer.extend(message)

                while len(buffer) >= FRAME_BYTES:
                    frame = bytes(buffer[:FRAME_BYTES])
                    del buffer[:FRAME_BYTES]

                    await source.send_frame(frame)

                    next_send += frame_interval
                    delay = next_send - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    else:
                        # Do not try to catch up by bursting old audio.
                        next_send = loop.time()
        except websockets.exceptions.ConnectionClosed:
            # A socket that closes, tidily or not, is a call that has ended.
            # Callers hang up mid-sentence and networks drop; neither is a
            # relay failure, and counting them as one would report healthy
            # calls as errors.
            return

    async def _boundary_reached(
        self, socket, source: CallSource, message, buffer: bytearray, next_send: float
    ) -> float:
        """Answer a playback boundary — but only once everything is played.

        A turn almost never ends on a 160-byte boundary, so when the boundary
        arrives there are usually 1..159 bytes still in `buffer`: real audio,
        too short for an RTP packet, that the steady-state loop above cannot
        emit. Answering with those bytes unsent would make the acknowledgement
        a lie, and the bank would hang up on the last syllable of a sentence.

        Only a valid boundary touches the media at all. Anything unparseable or
        unknown leaves the buffer exactly as it was and is not answered — a
        frame we cannot read is not a reason to flush a call's audio.

        The tail is padded to a whole packet with µ-law silence rather than
        sent short. `gateway.sources.SILENCE` is the project's convention and
        says why: µ-law silence is 0xFF, not 0x00, because the encoding is
        inverted. A short payload is the other failure this gateway already
        documents — a carrier drops it or plays it as a burst of noise.

        The reply is sent from this task while `_caller_to_bank` may be sending
        audio on the same socket. Both are small, unfragmented messages, which
        the websockets library writes as complete frames; they cannot interleave.
        """
        negotiation = read_protocol_message(message)
        if negotiation is not None:
            if negotiation[0] == PROTOCOL_HELLO:
                await self._answer_hello(socket)
            return next_send

        control = read_control_message(message)
        if control is None:
            return next_send
        kind, boundary_id = control
        if kind != PLAYBACK_BOUNDARY:
            return next_send

        next_send = await self._play_final_frame(source, buffer, next_send)

        try:
            await socket.send(playback_drained_message(boundary_id))
        except websockets.exceptions.ConnectionClosed:
            # The call ended while we were answering. Nothing to report to.
            pass
        return next_send

    @staticmethod
    async def _answer_hello(socket) -> None:
        """Say what this gateway speaks, so the bank can refuse a mismatch.

        Answered unconditionally: deciding compatibility is the application's
        business, because it is the one that would hang waiting for something
        this gateway does not send. All this end does is state the truth about
        itself and let the other end judge.
        """
        try:
            await socket.send(protocol_ready_message())
        except websockets.exceptions.ConnectionClosed:
            return

    @staticmethod
    async def _play_final_frame(
        source: CallSource, buffer: bytearray, next_send: float
    ) -> float:
        """Play a turn's leftover bytes as one padded packet, at playout pace.

        Waits out that packet's 20 ms like every other, so that when this
        returns the audio really has been played rather than merely sent.
        """
        if not buffer:
            return next_send

        tail = bytes(buffer) + SILENCE[: FRAME_BYTES - len(buffer)]
        del buffer[:]
        await source.send_frame(tail)

        loop = asyncio.get_running_loop()
        next_send += 0.020
        delay = next_send - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_send = loop.time()
        return next_send


async def _quietly(awaitable) -> None:
    """Await something during teardown, where failing again changes nothing."""
    try:
        await awaitable
    except (asyncio.CancelledError, Exception):
        pass
