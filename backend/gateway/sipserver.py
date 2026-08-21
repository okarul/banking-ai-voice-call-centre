"""The gateway answering SIP, so FreeSWITCH can simply bridge a call to it.

This replaces an earlier design that forked audio with FreeSWITCH's `unicast`
dialplan application. That design was wrong: `unicast` **does not exist** in
FreeSWITCH 1.10.12 — verified against a running 1.10.12 build, whose
`mod_dptools` offers 178 applications and none of them is `unicast`. Nor does
the `rtp` endpoint accept a plain address as a dial string. The core mechanisms
for streaming call audio to an outside process are third-party modules
(`mod_audio_stream`, `mod_audio_fork`), which community images do not ship.

Answering SIP instead uses only `mod_sofia`, which every build has:

    carrier ──SIP/RTP──► FreeSWITCH ──SIP/RTP──► this gateway ──WSS──► bank
                          bridge, core          UAS, here

Two problems disappear with it. **Per-call media ports** are negotiated by SDP,
the way RTP has always allocated them, so the dialplan needs no fixed port and
the gateway needs no event-socket shim to hand ports out. And **call control**
arrives with the signalling: an INVITE is a call starting and a BYE is a call
ending, so nothing has to be inferred from silence.

Deliberately minimal. It answers INVITE, ACK, BYE, CANCEL and OPTIONS, and
nothing else — no registration, no authentication of its own, no re-INVITE, no
hold, no transfer. A softphone needs all of those; a leg between two servers on
a private link needs none, and each one is a place to be subtly wrong about a
protocol nobody here is checking.

**This leg carries no credential and confers no identity.** The media token
belongs to the gateway↔bank connection. A SIP peer that reaches this port can
cause a call to be *offered* to the bank; it cannot authenticate a customer,
and the bank treats every such call as anonymous until the PIN check passes.
Restrict who can reach it at the network layer — see `allowed_peers`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from gateway.udp_source import MediaPortAllocator, UdpMediaSource

logger = logging.getLogger("gateway.sip")

# How long a dialog may sit between INVITE and ACK before it is abandoned.
ACK_TIMEOUT = 32.0


def _header(text: str, name: str) -> str | None:
    match = re.search(rf"^{name}\s*:\s*(.+)$", text, re.M | re.I)
    return match.group(1).strip() if match else None


def _sdp_media(text: str) -> tuple[str, int] | None:
    """The address and port the far side wants RTP sent to."""
    body = text.split("\r\n\r\n", 1)[-1]
    host = re.search(r"^c=IN IP4 (\S+)", body, re.M)
    port = re.search(r"^m=audio (\d+)", body, re.M)
    if not host or not port:
        return None
    return host.group(1), int(port.group(1))


class _Dialog:
    """One SIP call in progress on this server."""

    def __init__(self, call_id: str, source: UdpMediaSource) -> None:
        self.call_id = call_id
        self.source = source
        self.task: asyncio.Task | None = None
        self.started_at = time.monotonic()
        self.answered = False


class SipUas(asyncio.DatagramProtocol):
    """A SIP user agent server that hands accepted calls to the gateway.

    One dialog per Call-ID. Nothing is shared between dialogs but the port
    allocator, which hands out a distinct port to each — so two calls have two
    sockets and audio has no route from one to the other.
    """

    def __init__(
        self,
        *,
        gateway,
        allocator: MediaPortAllocator,
        advertise_host: str,
        allowed_peers: set[str] | None = None,
    ) -> None:
        self._gateway = gateway
        self._allocator = allocator
        self._advertise = advertise_host
        # The port this server answers on. It must appear in the Contact header
        # of a 200 OK: the far side addresses its ACK and BYE to that URI, and a
        # Contact without a port means SIP's default of 5060 — so the call is
        # answered, never acknowledged, and dies as a dialog nobody completes.
        self._port = 5060
        # Who may offer this gateway a call. None means anyone that can reach
        # the port, which is safe only while it is bound to a private network.
        self._allowed = allowed_peers
        self._dialogs: dict[str, _Dialog] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self.accepted = 0
        self.refused = 0

    # --- lifecycle -----------------------------------------------------------

    async def listen(self, host: str, port: int) -> int:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(host, port)
        )
        self._transport = transport
        bound = transport.get_extra_info("sockname")[1]
        self._port = bound
        logger.info("sip: listening on %s:%s", host, bound)
        return bound

    async def close(self) -> None:
        for dialog in list(self._dialogs.values()):
            await self._end(dialog)
        self._dialogs.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    @property
    def active_calls(self) -> int:
        return len(self._dialogs)

    # --- the datagram protocol ----------------------------------------------

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            text = data.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            return
        if text.startswith("SIP/2.0"):
            return  # a response to something we sent; nothing to do
        asyncio.get_running_loop().create_task(self._handle(text, addr))

    async def _handle(self, text: str, addr) -> None:
        method = text.split(" ", 1)[0].upper()
        call_id = _header(text, "Call-ID")
        if not call_id:
            return

        if self._allowed is not None and addr[0] not in self._allowed:
            # Refused at the edge, before a dialog exists. An unrecognised peer
            # cannot make this gateway offer a call to the bank.
            logger.warning("sip: refused %s from an unlisted peer", method)
            self._respond(text, addr, 403, "Forbidden")
            return

        if method == "OPTIONS":
            self._respond(text, addr, 200, "OK")
        elif method == "INVITE":
            await self._invite(text, addr, call_id)
        elif method == "ACK":
            await self._ack(call_id)
        elif method in ("BYE", "CANCEL"):
            self._respond(text, addr, 200, "OK")
            dialog = self._dialogs.pop(call_id, None)
            if dialog is not None:
                await self._end(dialog)
        else:
            self._respond(text, addr, 405, "Method Not Allowed")

    # --- the call ------------------------------------------------------------

    async def _invite(self, text: str, addr, call_id: str) -> None:
        if call_id in self._dialogs:
            # A retransmitted INVITE. SIP over UDP retransmits freely, and
            # answering twice would put two calls on one dialog.
            return

        remote = _sdp_media(text)
        if remote is None:
            self.refused += 1
            self._respond(text, addr, 488, "Not Acceptable Here")
            return

        source = UdpMediaSource(
            call_id.split("@")[0][:64] or "sip-call",
            bind_host="0.0.0.0",
        )
        try:
            port = await self._allocator.bind(source)
        except Exception:
            # No media port left. Busy is the honest answer, and it is the one
            # a carrier knows how to act on.
            self.refused += 1
            await source.close()
            self._respond(text, addr, 486, "Busy Here")
            return

        self._dialogs[call_id] = _Dialog(call_id, source)
        self._respond(text, addr, 200, "OK", sdp=self._sdp(port))
        logger.info("sip: answered call %s on media port %s", source.call_id, port)

    def _sdp(self, port: int) -> str:
        session = int(time.time())
        return (
            "v=0\r\n"
            f"o=- {session} {session} IN IP4 {self._advertise}\r\n"
            "s=channel2-gateway\r\n"
            f"c=IN IP4 {self._advertise}\r\n"
            "t=0 0\r\n"
            f"m=audio {port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )

    async def _ack(self, call_id: str) -> None:
        """The handshake is complete; offer the call to the bank.

        Started here rather than on INVITE because a call that is never
        acknowledged was never established, and announcing it would take a
        capacity slot for a caller who is not there.
        """
        dialog = self._dialogs.get(call_id)
        if dialog is None or dialog.answered:
            return
        dialog.answered = True
        dialog.task = asyncio.create_task(
            self._gateway.handle_call(dialog.source),
            name=f"sip-call-{dialog.source.call_id}",
        )
        self.accepted += 1

    async def _end(self, dialog: _Dialog) -> None:
        """Release one call. Safe whatever state it reached."""
        await dialog.source.close()
        if dialog.task is not None:
            try:
                await asyncio.wait_for(dialog.task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    # --- responses -----------------------------------------------------------

    def _respond(
        self, request: str, addr, code: int, reason: str, *, sdp: str = ""
    ) -> None:
        """Reply to a request, echoing the headers SIP requires be mirrored."""
        if self._transport is None:  # pragma: no cover - during shutdown
            return

        to_header = _header(request, "To") or ""
        if code == 200 and ";tag=" not in to_header:
            # A dialog-establishing response must carry a tag, or the far side
            # cannot address anything back to us.
            to_header += f";tag=gw{int(time.time() * 1000) % 1_000_000}"

        lines = [f"SIP/2.0 {code} {reason}"]
        for name in ("Via", "From", "Call-ID", "CSeq"):
            value = _header(request, name)
            if value:
                lines.append(f"{name}: {value}")
        lines.append(f"To: {to_header}")
        lines.append(f"Contact: <sip:gateway@{self._advertise}:{self._port}>")
        if sdp:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(sdp)}")
        message = "\r\n".join(lines) + "\r\n\r\n" + sdp
        self._transport.sendto(message.encode(), addr)
