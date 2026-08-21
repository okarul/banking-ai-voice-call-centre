"""Where a call's audio comes from, before the gateway relays it.

A `CallSource` is one telephone call as the *network* side sees it: an
identifier, frames arriving, frames to play back, and an ending. The gateway
relays between a source and the bank and knows nothing else about either.

Two implementations, and the difference between them is the honest edge of this
work:

    SyntheticSource   frames a test supplies, in memory — complete
    UdpMediaSource    frames from a real call, via a media server (gateway.sipserver)

Real calls arrive through `gateway.sipserver.SipUas`: FreeSWITCH terminates the
carrier leg and bridges the call to the gateway over SIP, which negotiates a
media port per call and produces a `UdpMediaSource`. `SipSource` below is the
superseded seam, kept only as a signpost.

`SyntheticSource` remains the offline path. It carries the same frames through
the same gateway and stops where the network begins, which is what lets
isolation, capacity, credentials and cleanup be tested with no provider, no
telephone and no cost.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

# 20 ms of G.711 µ-law at 8 kHz — one RTP packet, and the unit both sides of
# this gateway speak in.
FRAME_BYTES = 160

# Frames of silence, for a source with nothing to say. µ-law silence is 0xFF,
# not 0x00: the encoding is inverted, so a buffer of zero bytes is loud.
SILENCE = b"\xff" * FRAME_BYTES


@runtime_checkable
class CallSource(Protocol):
    """One telephone call, as the network side of the gateway sees it."""

    @property
    def call_id(self) -> str:
        """The provider's identifier for this call. Not a customer id."""
        ...

    async def receive_frame(self) -> bytes | None:
        """One µ-law frame from the caller, or None when the call has ended."""
        ...

    async def send_frame(self, frame: bytes) -> None:
        """Play one µ-law frame to the caller."""
        ...

    async def close(self) -> None:
        """Release the call. Must be safe to call more than once."""
        ...


class SyntheticSource:
    """A call whose audio a test supplies, carried entirely in memory.

    Not a simulation of SIP and not described as one. It produces and consumes
    exactly the frames a real source would, and stops precisely where the
    network begins — which is what lets the whole gateway above it be tested
    for isolation, capacity, credentials and cleanup with no provider, no
    telephone and no cost.
    """

    def __init__(self, call_id: str, *, caller: str | None = None) -> None:
        self._call_id = call_id
        self.caller = caller
        # What the caller "said", queued for the gateway to relay.
        self._inbound: asyncio.Queue = asyncio.Queue()
        # What the bank sent back. Kept so a test can assert that this caller —
        # and only this caller — heard it.
        self.played: list[bytes] = []
        self.closed = False

    @property
    def call_id(self) -> str:
        return self._call_id

    async def receive_frame(self) -> bytes | None:
        frame = await self._inbound.get()
        return frame

    async def send_frame(self, frame: bytes) -> None:
        if not self.closed:
            self.played.append(frame)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Release a relay waiting on the next frame, so teardown cannot hang.
        self._inbound.put_nowait(None)

    # --- what a test drives -------------------------------------------------

    def speak(self, frame: bytes) -> None:
        """Deliver one frame as though the caller had spoken it."""
        self._inbound.put_nowait(frame)

    def hang_up(self) -> None:
        """End the call from the caller's side."""
        self._inbound.put_nowait(None)


class SipSource:
    """Superseded. Use `gateway.sipserver.SipUas` with `UdpMediaSource`.

    This was the seam for terminating SIP inside the gateway. It is no longer
    the plan: FreeSWITCH terminates the carrier leg and **bridges the call to
    the gateway over SIP**, which needs only `mod_sofia` and negotiates a media
    port per call through SDP. `SipUas` answers that leg and produces a
    `UdpMediaSource`, which is what the rest of the gateway consumes.

    Kept as a signpost rather than deleted, so anyone following the older
    documentation lands here instead of concluding the feature is missing.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "SipSource is superseded. FreeSWITCH bridges the call over SIP to "
            "gateway.sipserver.SipUas, which produces a UdpMediaSource. See "
            "deploy/freeswitch/README.md."
        )
