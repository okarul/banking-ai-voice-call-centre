"""RTP framing, and telling it apart from a bare audio payload.

A media server forking audio out of a call sends one of two things down a UDP
socket, and which one depends on the server and the flags it was given:

    bare      160 bytes of G.711 µ-law — the payload and nothing else
    RTP       a 12-byte RFC 3550 header, then those same 160 bytes

Rather than pick one and require the far side to match, this module reads
either. That is not indecision: the framing a given FreeSWITCH build and
`unicast` invocation produces has to be confirmed against the deployment, and a
gateway that silently mis-parses is a gateway that delivers noise while
reporting success. Accepting both means the media path works either way and the
framing is a detail rather than a prerequisite.

Detection is by shape, and deliberately conservative — see `looks_like_rtp`.

Nothing here logs, and nothing retains a frame. Audio is the customer's voice
and may contain a spoken PIN: it passes through and is not kept.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass

# RFC 3550 fixed header. Version 2, no padding, no extension, no CSRCs.
RTP_HEADER_BYTES = 12
RTP_VERSION = 2

# The payload type for G.711 µ-law, fixed by RFC 3551. Not negotiable and not
# configurable: 0 means PCMU everywhere.
PAYLOAD_TYPE_PCMU = 0
PAYLOAD_TYPE_PCMA = 8

# 20 ms of 8 kHz audio: the frame every leg of this system speaks in.
SAMPLES_PER_FRAME = 160


@dataclass(frozen=True)
class RtpPacket:
    """One parsed RTP packet. Only the fields this gateway acts on."""

    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def looks_like_rtp(datagram: bytes) -> bool:
    """Whether this datagram is plausibly RTP rather than a bare payload.

    Conservative on purpose. A false positive strips twelve bytes of real audio
    and produces a click; a false negative feeds a header into the codec and
    produces a burst of noise. So every cheap structural check has to agree:

    * long enough to hold a header and some audio
    * version bits are exactly 2
    * no CSRC contributors, which a forked call leg does not have
    * a payload type this system actually carries

    A 160-byte bare µ-law frame cannot pass: it is exactly the wrong length to
    hold a header plus a sensible payload, and µ-law silence (0xFF) has version
    bits of 3, not 2.
    """
    if len(datagram) <= RTP_HEADER_BYTES:
        return False

    first, second = datagram[0], datagram[1]
    if (first >> 6) != RTP_VERSION:
        return False
    if (first & 0x0F) != 0:  # CSRC count
        return False
    if (first & 0x10) != 0:  # header extension
        return False

    payload_type = second & 0x7F
    return payload_type in (PAYLOAD_TYPE_PCMU, PAYLOAD_TYPE_PCMA)


def parse(datagram: bytes) -> RtpPacket | None:
    """Read one RTP packet, or None if it is not one."""
    if not looks_like_rtp(datagram):
        return None
    first, second, sequence, timestamp, ssrc = struct.unpack(
        "!BBHII", datagram[:RTP_HEADER_BYTES]
    )
    return RtpPacket(
        payload_type=second & 0x7F,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=datagram[RTP_HEADER_BYTES:],
    )


def payload_of(datagram: bytes) -> bytes:
    """The audio in a datagram, whichever way it was framed."""
    packet = parse(datagram)
    return packet.payload if packet is not None else datagram


class RtpSender:
    """Builds outbound RTP for one call, with its own sequence and clock.

    Per call, never shared. Sequence numbers and timestamps are a stream's
    identity as much as its SSRC is — two calls sharing a sender would produce
    one interleaved stream that no receiver could separate, which is a media
    crossover with extra steps.
    """

    def __init__(self, *, payload_type: int = PAYLOAD_TYPE_PCMU, ssrc: int | None = None) -> None:
        self.payload_type = payload_type
        self.ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        # Random starting points, as RFC 3550 requires: predictable ones make a
        # stream trivial to inject into.
        self._sequence = random.getrandbits(16)
        self._timestamp = random.getrandbits(32)

    def build(self, payload: bytes) -> bytes:
        """Wrap one 20 ms payload as an RTP packet."""
        header = struct.pack(
            "!BBHII",
            RTP_VERSION << 6,
            self.payload_type,
            self._sequence,
            self._timestamp,
            self.ssrc,
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + SAMPLES_PER_FRAME) & 0xFFFFFFFF
        return header + payload
