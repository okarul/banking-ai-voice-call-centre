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
import json
import logging
from collections import deque
from typing import Protocol, runtime_checkable

logger = logging.getLogger("app.telephony.media")


# === the playback control protocol =========================================
#
# Binary frames on the media socket are audio. Text frames are control, and
# there are exactly two messages:
#
#     backend -> gateway   {"type": "playback_boundary", "id": "<opaque>"}
#     gateway -> backend   {"type": "playback_drained",  "id": "<same>"}
#
# They exist because the two ends of that socket run at completely different
# speeds. The backend hands over audio as fast as the socket will take it; the
# gateway paces it onto RTP at 160 bytes every 20 ms, because that is the rate
# a telephone plays. A twenty-second answer leaves the backend's queue in a
# fraction of a second and takes twenty seconds to reach the caller.
#
# Treating our own queue emptying as "the caller has heard it" is what closed a
# live call mid-sentence: the ten-second silence timer was armed while the
# gateway still had several seconds of speech to play, and it expired while the
# agent was still talking. Playback completion is something only the far end
# knows, so the boundary asks it.
#
# The id is an opaque per-call counter. These messages carry no transcript, no
# customer identity, no banking data and no credential, and are never logged.
#
# The gateway's copy is `gateway/control.py`, deliberately duplicated rather
# than imported: the gateway is standalone and replaceable, so the two ends
# agree by wire contract. The two must be changed together.

PLAYBACK_BOUNDARY = "playback_boundary"
PLAYBACK_DRAINED = "playback_drained"

# An id longer than this is not something we issued.
MAX_BOUNDARY_ID_LENGTH = 64


# A control frame is a handful of short words. Anything larger is not ours,
# and parsing it would be doing unbounded work on behalf of whoever sent it.
# The webhook has capped bodies since Phase 2; this path had no equivalent.
MAX_CONTROL_BYTES = 4096


def _too_large(text) -> bool:
    return not isinstance(text, str) or len(text) > MAX_CONTROL_BYTES


def playback_boundary_message(boundary_id: str) -> str:
    """Ask the far end to report when everything before this has been played."""
    return json.dumps({"type": PLAYBACK_BOUNDARY, "id": boundary_id})


def read_control_message(text) -> tuple[str, str] | None:
    """Parse a control frame, or None if it is not one we recognise.

    Strict on purpose. Anything unparseable, unknown, or the wrong shape is not
    ours to act on, and guessing at it is how a media socket starts doing
    something other than carrying one call's audio.
    """
    if _too_large(text):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    kind = payload.get("type")
    boundary_id = payload.get("id")
    if kind not in (PLAYBACK_BOUNDARY, PLAYBACK_DRAINED):
        return None
    if not isinstance(boundary_id, str):
        return None
    if not boundary_id or len(boundary_id) > MAX_BOUNDARY_ID_LENGTH:
        return None
    return kind, boundary_id


# --- negotiation ------------------------------------------------------------
#
# The playback boundary only works if the far end answers it. A backend that
# expects an acknowledgement from a gateway too old to send one waits for ever:
# the turn never completes, the silence timer never arms, and the call sits
# open with nobody able to explain why. Deploying the two together is the
# intent, but intent is not a mechanism, and a hung call is a bad way to
# discover a mismatched release.
#
# So the two ends say what they are before any conversation depends on it:
#
#     backend -> gateway   {"type": "protocol_hello", "version": 1,
#                           "features": ["playback_ack"]}
#     gateway -> backend   {"type": "protocol_ready", "version": 1,
#                           "features": ["playback_ack"]}
#
# Answered and compatible, the call proceeds. Unanswered, refused, or missing
# the feature, the call is given up before the caller is greeted — a refusal
# an operator can read beats a conversation that cannot end.

PROTOCOL_HELLO = "protocol_hello"
PROTOCOL_READY = "protocol_ready"

# Bumped only when the wire contract changes in a way an older peer cannot
# honour. Adding a feature name does not need a new version; removing or
# redefining one does.
PROTOCOL_VERSION = 1

# The one capability a telephone call cannot do without. Named rather than
# implied by the version, so a later gateway can offer more without either end
# having to guess what a version number includes.
FEATURE_PLAYBACK_ACK = "playback_ack"
REQUIRED_FEATURES = (FEATURE_PLAYBACK_ACK,)

# Bounds. A negotiation frame is a handful of short words; anything larger is
# not ours and is not worth parsing.
MAX_FEATURES = 16
MAX_FEATURE_LENGTH = 32


def protocol_hello_message() -> str:
    """What this backend is, and what it needs the far end to support."""
    return json.dumps(
        {
            "type": PROTOCOL_HELLO,
            "version": PROTOCOL_VERSION,
            "features": list(REQUIRED_FEATURES),
        }
    )


def protocol_ready_message() -> str:
    """The same, in answer. Used by the gateway; defined here as the contract."""
    return json.dumps(
        {
            "type": PROTOCOL_READY,
            "version": PROTOCOL_VERSION,
            "features": list(REQUIRED_FEATURES),
        }
    )


def read_protocol_message(text) -> tuple[str, int, tuple[str, ...]] | None:
    """Parse a negotiation frame, or None if it is not one.

    As strict as the playback parser and for the same reason: a media socket
    that acts on frames it cannot fully understand is a media socket doing
    something other than carrying one call's audio.
    """
    if _too_large(text):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    kind = payload.get("type")
    if kind not in (PROTOCOL_HELLO, PROTOCOL_READY):
        return None

    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        return None

    features = payload.get("features")
    if not isinstance(features, list) or len(features) > MAX_FEATURES:
        return None
    if not all(
        isinstance(name, str) and 0 < len(name) <= MAX_FEATURE_LENGTH
        for name in features
    ):
        return None

    return kind, version, tuple(features)


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

    async def send_playback_boundary(self, boundary_id: str) -> bool:
        """Ask the far end to report when it has finished playing.

        True if the question was asked and an answer should be waited for.
        False if there is nobody to ask — then this transport's own queue is
        the only playout there is, and emptying it is completion.
        """
        ...

    async def wait_for_protocol(self, timeout: float) -> bool:
        """Whether the far end has agreed a compatible protocol in time.

        False means no conversation may begin on this transport. A transport
        with no far end to negotiate with is compatible by definition.
        """
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

    async def send_playback_boundary(self, boundary_id: str) -> bool:
        """Nothing paces audio here, so there is nobody to ask.

        The loopback transport delivers straight into a test's hands at
        whatever speed it reads. Its queue emptying really is completion.
        """
        return False

    async def wait_for_protocol(self, timeout: float) -> bool:
        """No far end, so nothing to disagree with."""
        return True

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

    **Ownership.** The route accepts the socket and hands it here; from that
    point this transport may also *close* it, and `on_call_ended` does.

    That is a deliberate change of ownership, and the reason is the deadlock it
    removes. When the bank ends a call the route is blocked in
    `websocket.receive()`, waiting for a caller who has been disconnected in
    every sense except the socket. Nothing else can wake it. The gateway's
    `async for message in socket` therefore never finishes, `handle_call` never
    returns, and the outbound SIP BYE that ends the telephone leg never runs —
    so the caller hears the closing sentence and then dead air.

    Closing here breaks that circle. Audio already sent is not lost: every
    `send_audio` is awaited before the queue drains, so the close frame is
    queued behind the audio and the gateway's iterator yields all of it before
    it sees the socket end.
    """

    def __init__(self, *, max_frames: int) -> None:
        self._websocket = None
        self._inbound = BoundedAudioQueue(max_frames=max_frames, name="ws-in")
        self._closed = False
        self._attached = asyncio.Event()
        # Negotiation. The event is set on any outcome — agreed, refused or
        # the call ending — so nobody waits out the timeout for an answer that
        # has already arrived.
        self._negotiated = asyncio.Event()
        self._compatible = False
        self._peer_version: int | None = None

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

    @property
    def compatible(self) -> bool:
        """Whether the far end answered with a protocol this backend can use."""
        return self._compatible

    @property
    def peer_version(self) -> int | None:
        """The version the far end reported, for the operator. None if silent."""
        return self._peer_version

    async def send_protocol_hello(self) -> bool:
        """Say what this backend is. False if there is nothing to say it to."""
        if self._closed or self._websocket is None:
            return False
        try:
            await self._websocket.send_text(protocol_hello_message())
            return True
        except Exception as error:
            self._closed = True
            self._negotiated.set()
            logger.info(
                "media socket closed during negotiation: %s", type(error).__name__
            )
            return False

    def on_protocol_ready(self, version: int, features: tuple[str, ...]) -> bool:
        """Record the far end's answer. False means this call cannot proceed.

        Both halves matter. A version this backend does not speak is a
        mismatched release; the right version without `playback_ack` is a
        gateway that would never answer a playback boundary, which is the hang
        this negotiation exists to prevent.
        """
        self._peer_version = version
        missing = [name for name in REQUIRED_FEATURES if name not in features]

        if version != PROTOCOL_VERSION:
            logger.error(
                "media protocol version mismatch: gateway=%s backend=%s",
                version,
                PROTOCOL_VERSION,
            )
        elif missing:
            logger.error("media protocol missing feature: %s", ",".join(missing))
        else:
            self._compatible = True

        self._negotiated.set()
        return self._compatible

    async def wait_for_protocol(self, timeout: float) -> bool:
        """Wait for the far end to agree a protocol. False if it never did."""
        if self._closed:
            return False
        try:
            await asyncio.wait_for(self._negotiated.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("media protocol negotiation timed out after %ss", timeout)
            return False
        return self._compatible and not self._closed

    async def send_playback_boundary(self, boundary_id: str) -> bool:
        """Ask the gateway when this turn has actually reached the telephone."""
        if self._closed or self._websocket is None:
            return False
        try:
            await self._websocket.send_text(playback_boundary_message(boundary_id))
            return True
        except Exception as error:
            self._closed = True
            logger.info(
                "media socket closed while sending control: %s", type(error).__name__
            )
            return False

    async def on_call_ended(self) -> None:
        """Release the transport and close the socket. Safe to call twice.

        The socket reference is taken first, so a second call has nothing left
        to close and a caller who already hung up cannot turn an ordinary
        teardown into an error.
        """
        self._closed = True
        self._attached.set()
        # Anything waiting on negotiation is waiting for a call that has ended.
        self._negotiated.set()
        self._inbound.close()

        socket = self._websocket
        self._websocket = None
        if socket is None:
            return

        try:
            await socket.close()
        except Exception as error:
            # Already gone: the caller hung up, or the server tore the
            # connection down first. That is the normal end of a call, not a
            # failure, and it must not propagate into the teardown path.
            logger.info(
                "media socket already closed on teardown: %s", type(error).__name__
            )
