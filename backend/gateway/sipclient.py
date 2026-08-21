"""A minimal SIP user agent, for driving loopback validation.

**A test and diagnostic tool, not part of the call path.** Nothing in the
gateway or the bank imports it. It exists so Phase 4B can place a real SIP call
into FreeSWITCH — a real INVITE, real SDP negotiation, real RTP — without
pulling a second opaque third-party image onto the machine. Written rather than
downloaded means it can be read, reviewed and changed.

It implements exactly the flow a loopback test needs and no more:

    INVITE (SDP offer: PCMU) ──►
                              ◄── 100 Trying / 180 Ringing
                              ◄── 200 OK (SDP answer)
    ACK ─────────────────────►
    ◄──────── RTP both ways ─────────►
    BYE ─────────────────────►
                              ◄── 200 OK

No registration, no authentication, no re-INVITE, no hold, no DTMF, no
transfer. A real softphone does all of those; a loopback test needs none of
them, and each one is a place to be subtly wrong about a protocol nobody is
checking here.

**The advertised address may differ from the bind address.** On Docker for
Windows the two sides of a call see each other at different addresses, so the
SDP `c=` line is configurable independently of the socket. That is the same
asymmetry `ext-rtp-ip` exists for on the server side, and getting it wrong is
the classic one-way-audio failure rather than a visible error.
"""

from __future__ import annotations

import random
import re
import socket
import string
import time


def _token(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


class SipCallFailed(Exception):
    """The call was not established. Carries the status only, never a body."""


class SipUac:
    """One outbound SIP call, with its own RTP socket."""

    def __init__(
        self,
        *,
        target_host: str,
        target_port: int = 5060,
        destination: str = "1000",
        local_host: str = "0.0.0.0",
        advertise_host: str | None = None,
        user: str = "loopback",
    ) -> None:
        self.target = (target_host, target_port)
        self.destination = destination
        self.user = user

        self._sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sip.bind((local_host, 0))
        self._sip.settimeout(5.0)
        self.sip_port = self._sip.getsockname()[1]

        self._rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp.bind((local_host, 0))
        self._rtp.setblocking(False)
        self.rtp_port = self._rtp.getsockname()[1]

        # What we tell the far side to send to. Not necessarily where we bound.
        self.advertise = advertise_host or self._discover_local_address()

        self.call_id = f"{_token(16)}@{self.advertise}"
        self.from_tag = _token(8)
        self.to_tag: str | None = None
        self.remote_target: str | None = None
        self.remote_rtp: tuple[str, int] | None = None
        self.status: int | None = None

        self.received_rtp: list[bytes] = []
        self._sequence = random.getrandbits(16)
        self._timestamp = random.getrandbits(32)
        self._ssrc = random.getrandbits(32)

    def _discover_local_address(self) -> str:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(self.target)
            return probe.getsockname()[0]
        finally:
            probe.close()

    # --- signalling ----------------------------------------------------------

    def _sdp(self) -> str:
        session = random.getrandbits(31)
        return (
            "v=0\r\n"
            f"o=- {session} {session} IN IP4 {self.advertise}\r\n"
            "s=loopback\r\n"
            f"c=IN IP4 {self.advertise}\r\n"
            "t=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )

    def _request(self, method: str, uri: str, cseq: int, body: str = "") -> str:
        to_header = f"<sip:{self.destination}@{self.target[0]}>"
        if self.to_tag:
            to_header += f";tag={self.to_tag}"
        headers = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.advertise}:{self.sip_port};branch=z9hG4bK{_token()}",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{self.advertise}>;tag={self.from_tag}",
            f"To: {to_header}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            f"Contact: <sip:{self.user}@{self.advertise}:{self.sip_port}>",
            "User-Agent: channel2-loopback-uac",
        ]
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body)}")
        return "\r\n".join(headers) + "\r\n\r\n" + body

    def _await_response(self, deadline: float) -> tuple[int, str]:
        while time.time() < deadline:
            try:
                data, _ = self._sip.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            match = re.match(r"SIP/2\.0 (\d{3})", text)
            if match:
                return int(match.group(1)), text
        raise SipCallFailed("no response before deadline")

    def invite(self, *, timeout: float = 15.0) -> None:
        """Place the call and complete the handshake."""
        uri = f"sip:{self.destination}@{self.target[0]}:{self.target[1]}"
        self._sip.sendto(self._request("INVITE", uri, 1, self._sdp()).encode(), self.target)

        deadline = time.time() + timeout
        while True:
            status, text = self._await_response(deadline)
            self.status = status
            if status < 200:
                continue  # 100 Trying, 180 Ringing
            if status >= 300:
                raise SipCallFailed(f"call rejected with {status}")

            tag = re.search(r"^To:.*;tag=([^\s;]+)", text, re.M | re.I)
            if tag:
                self.to_tag = tag.group(1).strip()
            contact = re.search(r"^Contact:\s*<([^>]+)>", text, re.M | re.I)
            self.remote_target = contact.group(1) if contact else uri
            self.remote_rtp = self._parse_sdp(text)
            break

        # ACK completes the three-way handshake; without it the far side
        # retransmits its 200 and eventually tears the call down.
        self._sip.sendto(
            self._request("ACK", self.remote_target, 1).encode(), self.target
        )

    def _parse_sdp(self, text: str) -> tuple[str, int] | None:
        body = text.split("\r\n\r\n", 1)[-1]
        host = re.search(r"^c=IN IP4 (\S+)", body, re.M)
        port = re.search(r"^m=audio (\d+)", body, re.M)
        if not host or not port:
            return None
        return host.group(1), int(port.group(1))

    def hangup(self) -> None:
        """End the call. Safe to call more than once."""
        if self.remote_target and self.to_tag:
            try:
                self._sip.sendto(
                    self._request("BYE", self.remote_target, 2).encode(), self.target
                )
                self._await_response(time.time() + 3.0)
            except (SipCallFailed, OSError):
                pass
        self.remote_target = None
        self.close()

    def close(self) -> None:
        for handle in (self._sip, self._rtp):
            try:
                handle.close()
            except OSError:
                pass

    # --- media ---------------------------------------------------------------

    def speak(self, payload: bytes) -> None:
        """Send one 20 ms µ-law frame as RTP."""
        if not self.remote_rtp:
            return
        import struct

        header = struct.pack(
            "!BBHII", 2 << 6, 0, self._sequence, self._timestamp, self._ssrc
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + 160) & 0xFFFFFFFF
        try:
            self._rtp.sendto(header + payload, self.remote_rtp)
        except OSError:
            pass

    def drain(self) -> list[bytes]:
        """Every RTP payload received so far, headers stripped."""
        while True:
            try:
                data, _ = self._rtp.recvfrom(4096)
            except (BlockingIOError, OSError):
                break
            self.received_rtp.append(data[12:] if len(data) > 12 else data)
        return self.received_rtp
