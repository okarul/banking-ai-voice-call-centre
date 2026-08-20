"""The media transport boundary, and the queues either side of it.

The banking application must not know how audio arrives. `MediaTransport` is
the whole of what it needs — four verbs, no protocol:

    on_call_started   the transport is ready to carry audio
    receive_audio     one frame from the caller, or None when the call ends
    send_audio        one frame to the caller
    on_call_ended     tear the transport down

Two implementations exist here. `WebSocketMediaTransport` carries real audio
from a media gateway. `LoopbackMediaTransport` carries whatever a test hands
it, in memory, deterministically — which is what makes the five-call
concurrency suite runnable with no provider, no sockets and no cost.

**On queues, and why they are bounded.** A telephone delivers 50 frames a
second whether or not anything is reading them. If the far side of the bridge
stalls — a slow model turn, a paused task, a network hiccup — an unbounded
queue grows until the process dies, and one caller can do that to the whole
bank. So every queue has a ceiling and a stated policy for what happens when it
is reached.

The policy is **drop the oldest**, and that is the right way round for audio.
Dropping the newest frame preserves stale audio and discards the present, so a
caller who has been waiting hears an ever-growing delay. Dropping the oldest
keeps the conversation live: the caller loses a moment of speech, which is
recoverable, rather than the call sliding permanently out of sync, which is
not. Drops are counted so an operator can see the call was degraded.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Protocol, runtime_checkable

logger = logging.getLogger("app.telephony.media")


class MediaTransportError(Exception):
    """The media path could not be established or has failed."""


class BoundedAudioQueue:
    """An audio queue that cannot grow without limit.

    Deliberately not `asyncio.Queue(maxsize=...)`. That one makes the *producer*
    wait when full, and the producer here is a telephone: it does not wait, it
    keeps sending, and the backpressure would land on the socket reader instead
    — where it would stall every call sharing that task rather than degrade the
    one call that is behind.
    """

    def __init__(self, *, max_frames: int, name: str = "audio") -> None:
        self._frames: deque[bytes] = deque()
        self._max = max_frames
        self._name = name
        self._waiters: deque[asyncio.Future] = deque()
        self._closed = False
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def max_frames(self) -> int:
        return self._max

    @property
    def closed(self) -> bool:
        return self._closed

    def put(self, frame: bytes) -> None:
        """Add a frame, discarding the oldest if the queue is full."""
        if self._closed:
            return

        if len(self._frames) >= self._max:
            self._frames.popleft()
            self.dropped += 1
            # Logged once per hundred so a degraded call is visible without a
            # busy queue writing fifty lines a second.
            if self.dropped % 100 == 1:
                logger.warning(
                    "audio queue %s is full; dropped %s frame(s) so far",
                    self._name,
                    self.dropped,
                )

        self._frames.append(frame)
        self._wake()

    def _wake(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    async def get(self) -> bytes | None:
        """The next frame, or None once the queue is closed and drained."""
        while True:
            if self._frames:
                return self._frames.popleft()
            if self._closed:
                return None
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter

    def clear(self) -> None:
        """Discard everything queued but stay open.

        Used on barge-in: when the caller interrupts, the assistant audio still
        queued is audio for a sentence the caller has stopped listening to.
        Playing it out would talk over them.
        """
        self._frames.clear()

    def close(self) -> None:
        """No more frames. Waiting readers are released with None."""
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)


@runtime_checkable
class MediaTransport(Protocol):
    """How audio reaches and leaves one telephone call."""

    async def on_call_started(self) -> None:
        """Prepare the transport. Raises `MediaTransportError` on failure."""
        ...

    async def wait_until_ready(self, timeout: float) -> bool:
        """Whether audio can actually reach the caller yet.

        The distinction matters because of the greeting. A WebSocket transport
        exists from the moment the call is registered, but the gateway attaches
        its socket a little later — and anything sent before that is discarded,
        so a greeting spoken too early is a greeting the caller never hears.

        Returns False if the transport never became ready within `timeout`.
        """
        ...

    async def receive_audio(self) -> bytes | None:
        """One µ-law frame from the caller, or None when the call has ended."""
        ...

    async def send_audio(self, frame: bytes) -> None:
        """Send one µ-law frame to the caller."""
        ...

    async def on_call_ended(self) -> None:
        """Release the transport. Must be safe to call more than once."""
        ...


class LoopbackMediaTransport:
    """An in-memory transport for tests and local development.

    Not a simulation of SIP and not labelled as one: it carries the same µ-law
    frames through the same bridge, and stops exactly where the network would
    begin. What it proves is everything above the socket — isolation, capacity,
    conversion, cleanup — which is the part that has to be right before a real
    call is worth attempting.
    """

    def __init__(self, *, max_frames: int = 200) -> None:
        self.inbound = BoundedAudioQueue(max_frames=max_frames, name="loopback-in")
        # What the caller would have heard. Kept so a test can assert that this
        # caller — and only this caller — received it.
        self.sent: list[bytes] = []
        self.started = False
        self.ended = False

    async def on_call_started(self) -> None:
        self.started = True

    async def wait_until_ready(self, timeout: float) -> bool:
        """Immediately. There is no socket to wait for."""
        return not self.ended

    async def receive_audio(self) -> bytes | None:
        return await self.inbound.get()

    async def send_audio(self, frame: bytes) -> None:
        self.sent.append(frame)

    async def on_call_ended(self) -> None:
        self.ended = True
        self.inbound.close()

    # --- test-facing helpers -------------------------------------------------

    def feed(self, frame: bytes) -> None:
        """Deliver one frame as though the caller had spoken it."""
        self.inbound.put(frame)

    def hang_up(self) -> None:
        """End the call from the caller's side."""
        self.inbound.close()


class WebSocketMediaTransport:
    """Audio over a WebSocket from a media gateway.

    This is the transport a SIP gateway speaks when it has been told to stream
    a call to an application: frames of µ-law in binary messages, one call per
    socket. It is a real transport, not a stand-in — what is *not* implemented
    is the SIP and RTP termination that produces it, which needs either a media
    gateway in front of this application or a SIP stack inside it. See
    `docs/TELEPHONY_MEDIA.md`.

    The socket is owned by the route that accepted it. This class reads and
    writes; it does not close the underlying connection out from under FastAPI.
    """

    def __init__(self, *, max_frames: int) -> None:
        self._websocket = None
        self._inbound = BoundedAudioQueue(max_frames=max_frames, name="ws-in")
        self._closed = False
        self._attached = asyncio.Event()

    @property
    def inbound(self) -> BoundedAudioQueue:
        return self._inbound

    @property
    def attached(self) -> bool:
        return self._websocket is not None

    def attach(self, websocket) -> None:
        """Bind the gateway's socket to a call that is already registered.

        The socket arrives *after* the control event that announced the call —
        the gateway has to be told a call exists before it can stream it. So
        the transport exists from registration and waits here, which keeps one
        call to one bridge regardless of how late the audio turns up.
        """
        self._websocket = websocket
        self._attached.set()

    async def wait_for_attach(self, timeout: float) -> bool:
        """Wait for the gateway to connect. False if it never did."""
        try:
            await asyncio.wait_for(self._attached.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_until_ready(self, timeout: float) -> bool:
        """Ready once the gateway has attached — and not before.

        `on_call_ended` also sets the attach event so a closing call releases
        anybody waiting here, which is why the result is qualified: attached and
        still open is ready; attached because it ended is not.
        """
        attached = await self.wait_for_attach(timeout)
        return attached and not self._closed and self._websocket is not None

    async def on_call_started(self) -> None:
        return None

    async def receive_audio(self) -> bytes | None:
        return await self._inbound.get()

    def deliver(self, frame: bytes) -> None:
        """Hand a frame read off the socket to the bridge."""
        self._inbound.put(frame)

    async def send_audio(self, frame: bytes) -> None:
        """Send to the caller, or discard if there is nobody to send to.

        Audio generated before the gateway attaches, or after the caller has
        gone, is dropped rather than queued. Holding it would mean the caller
        eventually hears a greeting recorded for a moment that has passed.
        """
        if self._closed or self._websocket is None:
            return
        try:
            await self._websocket.send_bytes(frame)
        except Exception as error:
            # A caller who has gone away is an ordinary end of call, not an
            # error worth propagating into the banking session.
            self._closed = True
            logger.info("media socket closed while sending: %s", type(error).__name__)

    async def on_call_ended(self) -> None:
        self._closed = True
        self._attached.set()
        self._inbound.close()
