"""A call whose audio arrives over UDP from a media server.

This is the `CallSource` that FreeSWITCH talks to. FreeSWITCH terminates the
SIP signalling and the RTP from the telephone network, then forks the call's
audio to a UDP socket this class is listening on — and reads this class's
replies back into the call. Everything above it is unchanged: the gateway
relays these frames to the bank exactly as it relays synthetic ones.

**One call, one socket, one port.** Each call binds its own UDP port out of a
bounded range, so two calls cannot share a socket and audio has no route
between them. The port is released when the call ends.

**The peer is learned, then fixed.** The first datagram to arrive on a call's
port establishes where replies go, and after that the address does not change.
A socket that re-pointed itself at whoever sent the most recent packet would
let anyone who found the port redirect a live call's audio to themselves by
sending a single datagram.

**Framing is whatever the media server sends.** Bare µ-law or RTP, detected per
datagram — see `gateway.rtp`. Replies are sent back in the same framing the
peer used, because a media server that sent RTP expects RTP.
"""

from __future__ import annotations

import asyncio
import logging

from gateway.rtp import RtpSender, looks_like_rtp, payload_of
from gateway.sources import FRAME_BYTES

logger = logging.getLogger("gateway.udp")


class MediaPortsExhausted(Exception):
    """Every port in the configured range is in use."""


class UdpMediaSource:
    """One telephone call, carried over UDP from a media server."""

    def __init__(
        self,
        call_id: str,
        *,
        caller: str | None = None,
        bind_host: str = "127.0.0.1",
        max_queued_frames: int = 200,
    ) -> None:
        self._call_id = call_id
        self.caller = caller
        self._bind_host = bind_host
        self._max_queued = max_queued_frames

        self._transport: asyncio.DatagramTransport | None = None
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._peer: tuple[str, int] | None = None
        self._peer_uses_rtp = False
        self._sender = RtpSender()
        self.port: int | None = None
        self.closed = False

        # Counters, for the operator and the tests. Never audio.
        self.frames_in = 0
        self.frames_out = 0
        self.dropped = 0

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def peer(self) -> tuple[str, int] | None:
        return self._peer

    # --- lifecycle -----------------------------------------------------------

    async def bind(self, *, port_low: int, port_high: int) -> int:
        """Take one port from the range, and listen on it.

        Ports are tried in order rather than at random so that a run with a
        small range is reproducible, and so an operator reading a log can tell
        how much of the range is in use.
        """
        loop = asyncio.get_running_loop()
        for port in range(port_low, port_high + 1):
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Receiver(self), local_addr=(self._bind_host, port)
                )
            except OSError:
                continue  # in use by another call, or by something else
            self._transport = transport
            self.port = port
            return port
        raise MediaPortsExhausted(
            f"no free media port in {port_low}-{port_high}"
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        # Release a relay waiting on the next frame, so teardown cannot hang.
        self._inbound.put_nowait(None)

    # --- the CallSource contract --------------------------------------------

    async def receive_frame(self) -> bytes | None:
        return await self._inbound.get()

    async def send_frame(self, frame: bytes) -> None:
        """Play one µ-law frame back into the call.

        Discarded rather than queued when there is nobody to send to. Audio
        held until a peer appears is audio for a moment that has passed — the
        caller would hear a greeting seconds late, over whatever they had
        started saying instead.
        """
        if self.closed or self._transport is None or self._peer is None:
            return
        datagram = self._sender.build(frame) if self._peer_uses_rtp else frame
        try:
            self._transport.sendto(datagram, self._peer)
            self.frames_out += 1
        except OSError as error:
            logger.info(
                "udp[%s] send failed: %s", self._call_id, type(error).__name__
            )

    # --- called by the datagram protocol ------------------------------------

    def _on_datagram(self, datagram: bytes, address: tuple[str, int]) -> None:
        if self.closed:
            return

        if self._peer is None:
            # First datagram wins, and fixes the peer for the call.
            self._peer = address
            self._peer_uses_rtp = looks_like_rtp(datagram)
            logger.info(
                "udp[%s] media peer attached on port %s (%s framing)",
                self._call_id,
                self.port,
                "rtp" if self._peer_uses_rtp else "bare",
            )
        elif address != self._peer:
            # Anyone else is ignored. Without this, one datagram from a
            # stranger would redirect a live call's audio to them.
            self.dropped += 1
            return

        payload = payload_of(datagram)
        if not payload:
            return
        if len(payload) != FRAME_BYTES:
            # A short or odd frame is a framing mismatch, not audio. Counted so
            # it is visible, and dropped so it never reaches the codec.
            self.dropped += 1
            return

        if self._inbound.qsize() >= self._max_queued:
            # Bounded, like every other audio queue in this system: a media
            # server that outruns the relay must not grow this without limit.
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - racing drain
                pass
            self.dropped += 1

        self._inbound.put_nowait(payload)
        self.frames_in += 1


class _Receiver(asyncio.DatagramProtocol):
    """Hands datagrams to one call's source. Owns nothing else."""

    def __init__(self, source: UdpMediaSource) -> None:
        self._source = source

    def datagram_received(self, data: bytes, addr) -> None:
        self._source._on_datagram(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover - transient
        logger.debug(
            "udp[%s] transient socket error: %s",
            self._source.call_id,
            type(exc).__name__,
        )


class MediaPortAllocator:
    """Hands out media ports from a bounded range, one call at a time.

    Bounded because an RTP range is a firewall rule: every port in it has to be
    open, and a range far larger than the call ceiling is a larger opening than
    the service needs. Two ports per call is the usual RTP convention (media
    and control); this gateway uses one, and the default range still leaves
    generous room above the five-call target.
    """

    def __init__(self, *, port_low: int, port_high: int) -> None:
        if port_high < port_low:
            raise ValueError("media port range is inverted")
        self.port_low = port_low
        self.port_high = port_high
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self.port_high - self.port_low + 1

    async def bind(self, source: UdpMediaSource) -> int:
        """Bind one source, serialised so two calls cannot claim one port."""
        async with self._lock:
            return await source.bind(
                port_low=self.port_low, port_high=self.port_high
            )
