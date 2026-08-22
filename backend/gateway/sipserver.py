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
import secrets
import time

from gateway.udp_source import MediaPortAllocator, UdpMediaSource

logger = logging.getLogger("gateway.sip")

# How long a dialog may sit between INVITE and ACK before it is abandoned.
ACK_TIMEOUT = 32.0


def _header(text: str, name: str) -> str | None:
    match = re.search(rf"^{name}\s*:\s*(.+)$", text, re.M | re.I)
    return match.group(1).strip() if match else None


def _contact_uri(text: str) -> str | None:
    """The address the far side wants in-dialog requests sent to.

    A BYE is addressed at the peer's Contact, not at the URI the call was
    dialled on: the dialled number identified who to reach, and Contact
    identifies the leg that answered. Absent Contact, the caller falls back to
    the network address the request arrived from.
    """
    contact = _header(text, "Contact")
    if not contact:
        return None
    match = re.search(r"<([^>]+)>", contact)
    return match.group(1) if match else contact.split(";")[0].strip()


def _sdp_media(text: str) -> tuple[str, int] | None:
    """The address and port the far side wants RTP sent to."""
    body = text.split("\r\n\r\n", 1)[-1]
    host = re.search(r"^c=IN IP4 (\S+)", body, re.M)
    port = re.search(r"^m=audio (\d+)", body, re.M)
    if not host or not port:
        return None
    return host.group(1), int(port.group(1))


class _Dialog:
    """One SIP call in progress on this server.

    Carries the minimum SIP identity needed to originate an in-dialog BYE, and
    nothing else. There is no caller number here and no customer: a dialog is a
    network conversation, and who is on it is decided by the PIN check.
    """

    def __init__(
        self,
        call_id: str,
        source: UdpMediaSource,
        *,
        peer,
        remote_from: str,
        local_to: str,
        remote_target: str | None,
    ) -> None:
        self.call_id = call_id
        self.source = source
        self.task: asyncio.Task | None = None
        self.started_at = time.monotonic()
        self.answered = False

        # Where in-dialog requests go, and how they are addressed.
        self.peer = peer
        # The INVITE's From, carrying the *remote* tag. Becomes our To on an
        # outbound request — the role reversal SIP requires.
        self.remote_from = remote_from
        # The To we sent in the 200 OK, carrying *our* tag. Becomes our From.
        # Stored rather than regenerated: a second tag would address a dialog
        # that does not exist and the BYE would be rejected.
        self.local_to = local_to
        self.remote_target = remote_target
        # Requests we originate in this dialog. The peer has its own sequence.
        self.local_cseq = 0

        # Set once this call has been terminated by somebody — a peer BYE, our
        # own BYE, or shutdown. What it guards is a second BYE.
        self.ended = False


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
        # Removing a dialog is a claim: whoever takes it out of this map is the
        # one that terminates it. The lock is what makes an inbound BYE racing
        # the bank's own completion produce one termination rather than two.
        self._claim_lock = asyncio.Lock()
        self._transport: asyncio.DatagramTransport | None = None
        self.accepted = 0
        self.refused = 0
        self.byes_sent = 0

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
        """Shut the server down, releasing every call it still holds.

        Dialogs are *claimed* rather than merely iterated, so the completion
        callbacks that fire as each `handle_call` unwinds find nothing to act on
        and no BYE is sent on the way down. That is deliberate and unchanged
        from before this fix: shutdown is not a call ending, and a burst of
        BYEs from a process that is going away is noise a carrier does not need.
        """
        async with self._claim_lock:
            claimed = list(self._dialogs.values())
            for dialog in claimed:
                dialog.ended = True
            self._dialogs.clear()

        for dialog in claimed:
            await self._release(dialog)
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
            # A response to something we sent — in practice the 200 to our own
            # BYE. Deliberately ignored: this leg runs between two processes on
            # a private link, the BYE is the last thing either of them needs
            # from the dialog, and the call's resources are already released by
            # the time it arrives. Retransmission handling would mean a SIP
            # transaction state machine, which is a great deal of surface for
            # no behaviour a carrier would notice. A response never creates or
            # alters a dialog; a test asserts it.
            return
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
            # Answer first: the peer is entitled to its 200 whether or not we
            # still hold the dialog, and a retransmitted BYE must be answered
            # again rather than ignored.
            self._respond(text, addr, 200, "OK")
            dialog = await self._claim(call_id)
            if dialog is not None:
                # The peer ended it, so we must not also send a BYE — the
                # dialog is already gone at their end and a second request
                # would arrive for a call that no longer exists.
                await self._release(dialog)
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

        # The tag we answer with, generated once and kept. Every later request
        # we originate in this dialog must carry this exact value.
        local_tag = f"gw{secrets.token_hex(4)}"
        to_header = _header(text, "To") or f"<sip:gateway@{self._advertise}>"
        if ";tag=" not in to_header:
            to_header = f"{to_header};tag={local_tag}"

        self._dialogs[call_id] = _Dialog(
            call_id,
            source,
            peer=addr,
            remote_from=_header(text, "From") or "",
            local_to=to_header,
            remote_target=_contact_uri(text),
        )
        self._respond(text, addr, 200, "OK", sdp=self._sdp(port), to_override=to_header)
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
        loop = asyncio.get_running_loop()
        dialog.task = asyncio.create_task(
            self._gateway.handle_call(dialog.source),
            name=f"sip-call-{dialog.source.call_id}",
        )
        # The defect this callback exists for: when the *bank* ends a call —
        # a spoken goodbye, or a caller who went silent — `handle_call` returns
        # and nothing told the telephone network. FreeSWITCH kept the leg up and
        # the caller heard the closing sentence and then dead air until they
        # hung up themselves.
        dialog.task.add_done_callback(
            lambda finished, cid=call_id: self._on_call_finished(cid, finished, loop)
        )
        self.accepted += 1

    def _on_call_finished(self, call_id: str, task: asyncio.Task, loop) -> None:
        """The bank has finished with this call. End the SIP leg.

        Runs as a task callback, so it must not raise and cannot await. The
        task's outcome is consumed here — an unretrieved exception on a task
        nobody awaits is logged by asyncio at garbage-collection time, long
        after the call it belonged to.
        """
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning(
                    "sip: call %s ended with %s", call_id, type(error).__name__
                )
        if loop.is_closed():  # pragma: no cover - during interpreter shutdown
            return
        loop.create_task(self._bank_finished(call_id), name=f"sip-bye-{call_id}")

    async def _bank_finished(self, call_id: str) -> None:
        """Terminate the SIP leg because the application is done with the call.

        Claiming first is what makes this safe against the peer hanging up at
        the same moment: exactly one of the two paths gets the dialog, and only
        that one acts.
        """
        dialog = await self._claim(call_id)
        if dialog is None:
            # The peer ended it first, or the server is shutting down. Either
            # way somebody else owns the teardown.
            return
        await self._send_bye(dialog)
        await self._release(dialog)

    async def _claim(self, call_id: str) -> _Dialog | None:
        """Take a dialog out of the map, or find somebody else already has.

        The single point at which a call's termination is decided. Returns the
        dialog to exactly one caller and `None` to every other, so a BYE racing
        the bank's completion cannot terminate the same call twice.
        """
        async with self._claim_lock:
            dialog = self._dialogs.pop(call_id, None)
            if dialog is None or dialog.ended:
                return None
            dialog.ended = True
            return dialog

    async def _send_bye(self, dialog: _Dialog) -> None:
        """Originate one in-dialog BYE for a call the application has finished.

        Role reversal is the part worth reading twice. The To we sent in the
        200 OK — carrying our tag — becomes the From of this request, and the
        INVITE's From — carrying theirs — becomes the To. Getting it the wrong
        way round produces a BYE for a dialog that does not exist, which is
        answered 481 and leaves the leg up exactly as before.
        """
        if self._transport is None:  # pragma: no cover - during shutdown
            return

        dialog.local_cseq += 1
        target = dialog.remote_target or f"sip:{dialog.peer[0]}:{dialog.peer[1]}"
        message = "\r\n".join(
            [
                f"BYE {target} SIP/2.0",
                f"Via: SIP/2.0/UDP {self._advertise}:{self._port}"
                f";branch=z9hG4bK{secrets.token_hex(6)}",
                "Max-Forwards: 70",
                f"From: {dialog.local_to}",
                f"To: {dialog.remote_from}",
                f"Call-ID: {dialog.call_id}",
                f"CSeq: {dialog.local_cseq} BYE",
                "Content-Length: 0",
            ]
        ) + "\r\n\r\n"

        try:
            self._transport.sendto(message.encode(), dialog.peer)
            self.byes_sent += 1
            # Category and call id only. Never a caller number, never audio,
            # never anything about the customer.
            logger.info("sip: ended call %s (application finished)", dialog.call_id)
        except OSError as error:  # pragma: no cover - transient socket failure
            logger.warning(
                "sip: could not end call %s: %s", dialog.call_id, type(error).__name__
            )

    async def _release(self, dialog: _Dialog) -> None:
        """Release one call's resources. Safe whatever state it reached.

        Closing the source is what frees the media port and unwinds
        `handle_call`. Awaiting the task is skipped when we are *inside* its own
        completion callback — it has already finished, and waiting on it from
        there would be waiting on ourselves.
        """
        await dialog.source.close()
        if dialog.task is not None and not dialog.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(dialog.task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    async def _end(self, dialog: _Dialog) -> None:
        """Backwards-compatible alias for `_release`."""
        await self._release(dialog)

    # --- responses -----------------------------------------------------------

    def _respond(
        self,
        request: str,
        addr,
        code: int,
        reason: str,
        *,
        sdp: str = "",
        to_override: str | None = None,
    ) -> None:
        """Reply to a request, echoing the headers SIP requires be mirrored.

        `to_override` carries the dialog's stored To, tag and all. The tag is
        generated once when the dialog is created and reused for the life of
        the call, because every in-dialog request has to agree on it.
        """
        if self._transport is None:  # pragma: no cover - during shutdown
            return

        to_header = to_override or _header(request, "To") or ""
        if code == 200 and ";tag=" not in to_header:
            # A dialog-establishing response must carry a tag, or the far side
            # cannot address anything back to us.
            to_header += f";tag=gw{secrets.token_hex(4)}"

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
