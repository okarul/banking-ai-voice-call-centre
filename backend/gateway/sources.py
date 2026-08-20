"""Where a call's audio comes from, before the gateway relays it.

A `CallSource` is one telephone call as the *network* side sees it: an
identifier, frames arriving, frames to play back, and an ending. The gateway
relays between a source and the bank and knows nothing else about either.

Two implementations, and the difference between them is the honest edge of this
work:

    SyntheticSource   frames a test supplies, in memory — complete
    SipSource         frames from a real telephone call — NOT IMPLEMENTED

`SipSource` is a named seam, not a stub pretending to be a stack. Terminating
SIP means answering an INVITE, negotiating SDP, de-jittering RTP and handling
BYE, and doing that convincingly without a provider to test against would
produce code that looks finished and fails on the first real call. What it
would plug into is defined here so the shape is fixed; what fills it is either
a SIP stack in this process or a media server in front of it.

Everything above this file is complete either way: the gateway, the signing,
the credential handling, the relay and the teardown are all exercised by
`SyntheticSource` in the offline suite.
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
    """A real telephone call over SIP. **Not implemented.**

    This is the seam, stated plainly rather than filled with something that
    would look convincing and fail on the first live call. To complete it,
    either:

    * put a SIP stack in this process (`pjsua2` or similar) and drive it from
      here — one new native dependency, and hard to test offline; or

    * run a media server in front (FreeSWITCH `mod_audio_stream`, Asterisk with
      AudioSocket) and make this class a client of *that*, which is the
      recommended shape: the protocol detail stays outside the process that
      talks to a bank, and the media server is separately testable.

    Either way the contract above does not change, and nothing else in this
    gateway or in the backend needs to.

    See `docs/PHASE4_LIVE_ACTIVATION.md`.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "SIP termination is not implemented. Run a media server in front of "
            "this gateway, or add a SIP stack to it. See "
            "docs/PHASE4_LIVE_ACTIVATION.md."
        )
