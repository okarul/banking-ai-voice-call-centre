"""The gateway's SIP leg: what it answers, what it refuses, what it confers.

`SipUas` is how FreeSWITCH hands a call over. It is the newest surface in the
system and the one reachable from a telephone network, so these tests are
mostly about refusal: an unlisted peer, an offer with no media, a retransmitted
INVITE, a call nobody acknowledges.

The property that matters most is the one it does *not* have. Answering SIP
lets a peer cause a call to be **offered** to the bank. It confers no identity
whatsoever — no customer, no authentication, nothing derived from a From
header, a caller id or a source address. The bank treats such a call as
anonymous until its own PIN check says otherwise.

Real sockets on loopback, no FreeSWITCH required.
"""

import asyncio
import re
import socket

import pytest

from gateway.sipserver import SipUas, _header, _sdp_media
from gateway.udp_source import MediaPortAllocator

PORT_LOW, PORT_HIGH = 17800, 17815


class RecordingGateway:
    """Stands in for the media gateway; records what it was handed."""

    def __init__(self) -> None:
        self.calls = []

    async def handle_call(self, source):
        self.calls.append(source)
        # Behave like a call that runs until the source closes.
        while True:
            frame = await source.receive_frame()
            if frame is None:
                return "completed"


def invite(call_id="abc123@test", *, sdp=True, destination="9001", caller="anon"):
    body = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    ) if sdp else ""
    return (
        f"INVITE sip:{destination}@127.0.0.1 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:9999;branch=z9hG4bKtest\r\n"
        f"From: <sip:{caller}@127.0.0.1>;tag=abcd\r\n"
        f"To: <sip:{destination}@127.0.0.1>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        + ("Content-Type: application/sdp\r\n" if sdp else "")
        + f"Content-Length: {len(body)}\r\n\r\n"
        + body
    ).encode()


def simple(method, call_id="abc123@test"):
    return (
        f"{method} sip:gateway@127.0.0.1 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:9999;branch=z9hG4bKtest2\r\n"
        "From: <sip:anon@127.0.0.1>;tag=abcd\r\n"
        "To: <sip:9001@127.0.0.1>;tag=gw1\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 2 {method}\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()


class Peer:
    """A SIP peer on loopback, driving the server under test."""

    def __init__(self, port: int) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(3.0)
        self.socket.bind(("127.0.0.1", 0))
        self.target = ("127.0.0.1", port)

    def send(self, payload: bytes) -> None:
        self.socket.sendto(payload, self.target)

    def response(self) -> str | None:
        try:
            return self.socket.recvfrom(65535)[0].decode()
        except socket.timeout:
            return None

    def close(self) -> None:
        self.socket.close()


async def _serve(gateway=None, **kwargs):
    uas = SipUas(
        gateway=gateway or RecordingGateway(),
        allocator=MediaPortAllocator(port_low=PORT_LOW, port_high=PORT_HIGH),
        advertise_host="127.0.0.1",
        **kwargs,
    )
    port = await uas.listen("127.0.0.1", 0)
    return uas, port


def run(coro):
    return asyncio.run(coro)


# === parsing ================================================================


def test_headers_are_read_case_insensitively():
    message = "INVITE sip:x SIP/2.0\r\ncall-id: xyz\r\nCSeq: 1 INVITE\r\n\r\n"

    assert _header(message, "Call-ID") == "xyz"
    assert _header(message, "CSeq") == "1 INVITE"
    assert _header(message, "Missing") is None


def test_the_media_address_is_read_from_the_sdp():
    assert _sdp_media(invite().decode()) == ("127.0.0.1", 40000)


def test_an_offer_with_no_sdp_has_no_media():
    assert _sdp_media(invite(sdp=False).decode()) is None


# === answering ==============================================================


def test_an_invite_is_answered_with_media():
    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        state = (uas.active_calls, answer)
        peer.close()
        await uas.close()
        return state

    active, answer = run(scenario())

    assert active == 1
    assert answer.startswith("SIP/2.0 200 OK")
    assert "a=rtpmap:0 PCMU/8000" in answer
    assert ";tag=" in answer, "a dialog-forming answer needs a To tag"


def test_the_contact_header_carries_the_port_we_answer_on():
    """Without it the far side ACKs to 5060 and the call is never acknowledged.

    This cost an hour of Phase 4B: the call answered, the dialog opened, and
    nothing followed, because SIP defaults a portless Contact to 5060.
    """

    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        peer.close()
        await uas.close()
        return answer, port

    answer, port = run(scenario())

    assert f"Contact: <sip:gateway@127.0.0.1:{port}>" in answer


def test_an_offer_without_media_is_refused():
    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(invite(sdp=False))
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        state = (uas.active_calls, uas.refused, answer)
        peer.close()
        await uas.close()
        return state

    active, refused, answer = run(scenario())

    assert answer.startswith("SIP/2.0 488")
    assert active == 0
    assert refused == 1


def test_a_retransmitted_invite_does_not_open_a_second_call():
    """SIP over UDP retransmits freely; answering twice puts two calls on one
    dialog."""

    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        for _ in range(4):
            peer.send(invite())
            await asyncio.sleep(0.1)
        peer.send(simple("ACK"))
        await asyncio.sleep(0.2)
        state = (uas.active_calls, len(gateway.calls))
        peer.close()
        await uas.close()
        return state

    active, handled = run(scenario())

    assert active == 1
    assert handled == 1


def test_the_call_is_offered_to_the_bank_only_after_the_ack():
    """A call nobody acknowledged was never established, and announcing it
    would take a capacity slot for a caller who is not there."""

    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.2)
        before = len(gateway.calls)
        peer.send(simple("ACK"))
        await asyncio.sleep(0.2)
        after = len(gateway.calls)
        peer.close()
        await uas.close()
        return before, after

    before, after = run(scenario())

    assert before == 0
    assert after == 1


def test_two_calls_get_two_media_ports():
    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        for index in range(2):
            peer.send(invite(call_id=f"call{index}@test"))
            await asyncio.sleep(0.15)
            peer.send(simple("ACK", call_id=f"call{index}@test"))
            await asyncio.sleep(0.15)
        ports = [source.port for source in gateway.calls]
        state = (uas.active_calls, ports)
        peer.close()
        await uas.close()
        return state

    active, ports = run(scenario())

    assert active == 2
    assert len(set(ports)) == 2, "two calls shared a media port"


# === ending =================================================================


def test_a_bye_ends_the_call_and_releases_the_dialog():
    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.15)
        peer.send(simple("ACK"))
        await asyncio.sleep(0.15)
        peer.send(simple("BYE"))
        await asyncio.sleep(0.3)
        state = (uas.active_calls, gateway.calls[0].closed)
        peer.close()
        await uas.close()
        return state

    active, closed = run(scenario())

    assert active == 0
    assert closed is True


def test_a_duplicate_bye_is_harmless():
    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.15)
        peer.send(simple("ACK"))
        await asyncio.sleep(0.15)
        for _ in range(3):
            peer.send(simple("BYE"))
            await asyncio.sleep(0.1)
        state = uas.active_calls
        peer.close()
        await uas.close()
        return state

    assert run(scenario()) == 0


def test_closing_the_server_releases_every_call():
    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        for index in range(3):
            peer.send(invite(call_id=f"c{index}@test"))
            await asyncio.sleep(0.12)
            peer.send(simple("ACK", call_id=f"c{index}@test"))
            await asyncio.sleep(0.12)
        during = uas.active_calls
        await uas.close()
        after = uas.active_calls
        closed = [source.closed for source in gateway.calls]
        peer.close()
        return during, after, closed

    during, after, closed = run(scenario())

    assert during == 3
    assert after == 0
    assert closed == [True, True, True]


# === refusal ================================================================


def test_an_unlisted_peer_cannot_offer_a_call():
    """`allowed_peers` is the network-layer control on who may reach this."""

    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway, allowed_peers={"10.9.9.9"})
        peer = Peer(port)
        peer.send(invite())
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        state = (uas.active_calls, len(gateway.calls), answer)
        peer.close()
        await uas.close()
        return state

    active, handled, answer = run(scenario())

    assert answer.startswith("SIP/2.0 403")
    assert active == 0
    assert handled == 0


def test_an_unsupported_method_is_refused():
    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(simple("REGISTER"))
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        peer.close()
        await uas.close()
        return answer

    answer = run(scenario())

    assert answer.startswith("SIP/2.0 405")


def test_options_is_answered_without_creating_a_call():
    async def scenario():
        uas, port = await _serve()
        peer = Peer(port)
        peer.send(simple("OPTIONS"))
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        state = (uas.active_calls, answer)
        peer.close()
        await uas.close()
        return state

    active, answer = run(scenario())

    assert answer.startswith("SIP/2.0 200")
    assert active == 0


def test_a_media_port_shortage_answers_busy_rather_than_failing():
    """Busy is an answer a carrier knows how to act on."""

    async def scenario():
        gateway = RecordingGateway()
        uas = SipUas(
            gateway=gateway,
            allocator=MediaPortAllocator(port_low=17890, port_high=17890),
            advertise_host="127.0.0.1",
        )
        port = await uas.listen("127.0.0.1", 0)
        peer = Peer(port)
        peer.send(invite(call_id="first@test"))
        await asyncio.sleep(0.15)
        peer.send(simple("ACK", call_id="first@test"))
        await asyncio.sleep(0.15)
        peer.response()

        peer.send(invite(call_id="second@test"))
        await asyncio.sleep(0.2)
        answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
        peer.close()
        await uas.close()
        return answer

    answer = run(scenario())

    assert answer.startswith("SIP/2.0 486")


# === it confers no identity =================================================


@pytest.mark.parametrize(
    "caller", ["DEMO001", "6531252836", "127.0.0.1", "admin", "anonymous"]
)
def test_no_sip_identifier_becomes_a_customer(caller):
    """A From header is a claim by whoever placed the call."""

    async def scenario():
        gateway = RecordingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        peer.send(invite(caller=caller))
        await asyncio.sleep(0.15)
        peer.send(simple("ACK"))
        await asyncio.sleep(0.2)
        source = gateway.calls[0]
        peer.close()
        await uas.close()
        return source

    source = run(scenario())

    surface = {name for name in dir(source) if not name.startswith("_")}
    for forbidden in ("customer_id", "authenticated", "verified", "pin"):
        assert forbidden not in surface, forbidden
    assert caller not in source.call_id or source.call_id == "abc123"


def test_the_sip_layer_holds_no_media_credential():
    """The media token belongs to the gateway-to-bank leg, not to SIP."""
    import inspect

    from gateway import sipserver

    source = inspect.getsource(sipserver)
    for absent in ("media_token", "X-Telephony-Media-Token", "webhook_secret"):
        assert absent not in source, absent


# === deployment addressing (Phase 4C) =======================================
#
# Phase 4B pinned a container IP to get a loopback call through. That is fine
# for a test and wrong for a deployment: container addresses change on restart,
# and a pinned one becomes a call that silently stops connecting.


def test_the_sip_leg_is_configured_not_hard_coded():
    import os

    from gateway.config import GatewaySettings

    previous = {k: os.environ.get(k) for k in
                ("GATEWAY_SIP_HOST", "GATEWAY_SIP_PORT", "GATEWAY_SIP_ADVERTISE_HOST")}
    os.environ["GATEWAY_SIP_HOST"] = "10.1.2.3"
    os.environ["GATEWAY_SIP_PORT"] = "5099"
    os.environ["GATEWAY_SIP_ADVERTISE_HOST"] = "203.0.113.9"
    try:
        settings = GatewaySettings()
        assert settings.sip_host == "10.1.2.3"
        assert settings.sip_port == 5099
        assert settings.sip_advertise_host == "203.0.113.9"
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_advertised_address_defaults_to_the_bind_address():
    """Right only when nothing translates in between, which is the default."""
    from gateway.config import GatewaySettings

    settings = GatewaySettings()

    assert settings.sip_advertise_host == settings.sip_host


def test_no_container_or_deployment_address_is_hard_coded_in_the_gateway():
    """The Phase 4B loopback pinned 192.168.65.254. Nothing may pin it now."""
    import pkgutil
    import re

    import gateway

    for module in pkgutil.iter_modules(gateway.__path__):
        if module.name == "sipclient":
            continue  # a test tool, and it discovers its own address
        source = __import__(
            f"gateway.{module.name}", fromlist=["_"]
        ).__file__
        text = open(source, encoding="utf-8").read()
        # Strip comments and docstrings' prose mentions of example addresses.
        code = re.sub(r"#.*", "", text)
        for pinned in ("192.168.65.254", "172.22.0.", "host.docker.internal"):
            assert pinned not in code, f"{module.name} pins {pinned}"


def test_the_defaults_are_loopback_and_therefore_restricted():
    from gateway.config import GatewaySettings

    settings = GatewaySettings()

    assert settings.sip_host == "127.0.0.1"
    assert settings.sip_publicly_bound is False
    assert settings.sip_source_restricted is True


def test_a_public_bind_without_a_peer_list_is_not_restricted():
    """The condition the gateway refuses to start under."""
    import os

    from gateway.config import GatewaySettings

    previous = os.environ.get("GATEWAY_SIP_HOST")
    os.environ["GATEWAY_SIP_HOST"] = "0.0.0.0"
    try:
        settings = GatewaySettings()
        assert settings.sip_publicly_bound is True
        assert settings.sip_source_restricted is False
    finally:
        if previous is None:
            os.environ.pop("GATEWAY_SIP_HOST", None)
        else:
            os.environ["GATEWAY_SIP_HOST"] = previous


def test_a_peer_list_restricts_a_public_bind():
    import os

    from gateway.config import GatewaySettings

    previous = {k: os.environ.get(k) for k in
                ("GATEWAY_SIP_HOST", "GATEWAY_SIP_ALLOWED_PEERS")}
    os.environ["GATEWAY_SIP_HOST"] = "0.0.0.0"
    os.environ["GATEWAY_SIP_ALLOWED_PEERS"] = "46.19.209.1, 46.19.210.2"
    try:
        settings = GatewaySettings()
        assert settings.sip_allowed_peers == {"46.19.209.1", "46.19.210.2"}
        assert settings.sip_source_restricted is True
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_gateway_refuses_to_answer_sip_unrestricted_on_a_public_bind():
    """A SIP port with no source restriction is found by scanners within hours,
    and every call they place is one the gateway would offer to a bank."""
    import os

    from gateway.__main__ import main

    previous = {k: os.environ.get(k) for k in
                ("GATEWAY_SIP_HOST", "GATEWAY_SIP_ALLOWED_PEERS",
                 "GATEWAY_WEBHOOK_SECRET")}
    os.environ["GATEWAY_SIP_HOST"] = "0.0.0.0"
    os.environ.pop("GATEWAY_SIP_ALLOWED_PEERS", None)
    os.environ["GATEWAY_WEBHOOK_SECRET"] = "a-secret-long-enough-to-be-real"
    try:
        import importlib

        import gateway.config

        importlib.reload(gateway.config)
        import gateway.__main__ as entry

        importlib.reload(entry)
        assert entry.main(["serve"]) == 1
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        import importlib

        import gateway.config

        importlib.reload(gateway.config)
        import gateway.__main__ as entry

        importlib.reload(entry)
        assert main is not None


def test_the_peer_list_is_never_printed():
    """An allow-list is a map of what to spoof."""
    import os

    from gateway.config import GatewaySettings

    previous = os.environ.get("GATEWAY_SIP_ALLOWED_PEERS")
    os.environ["GATEWAY_SIP_ALLOWED_PEERS"] = "203.0.113.77"
    try:
        summary = GatewaySettings().safe_summary()
        assert "203.0.113.77" not in str(summary)
        assert summary["sip_source_restricted"] is True
    finally:
        if previous is None:
            os.environ.pop("GATEWAY_SIP_ALLOWED_PEERS", None)
        else:
            os.environ["GATEWAY_SIP_ALLOWED_PEERS"] = previous


def test_sip_source_is_superseded_and_says_where_to_go():
    from gateway.sources import SipSource

    with pytest.raises(NotImplementedError) as error:
        SipSource()

    assert "sipserver" in str(error.value)


# === Phase 6.1: the application ends the SIP leg ============================
#
# The defect: when the *bank* finished a call — a spoken goodbye, or a caller
# who went silent — `handle_call` returned and nothing told the telephone
# network. FreeSWITCH kept the leg up, so the caller heard the closing sentence
# and then dead air until they hung up themselves.
#
# These drive real loopback UDP, like the rest of this file.


class EndableGateway:
    """A gateway whose call can be finished on demand, as the bank would."""

    def __init__(self) -> None:
        self.calls = []
        self.finish = asyncio.Event()

    async def handle_call(self, source):
        self.calls.append(source)
        await self.finish.wait()
        return "completed"


def parse_request(raw: str) -> dict:
    """Split a SIP request into its start line and headers."""
    lines = raw.split("\r\n")
    parsed = {"start_line": lines[0], "method": lines[0].split(" ", 1)[0]}
    for line in lines[1:]:
        if ": " in line:
            name, _, value = line.partition(": ")
            parsed[name.lower()] = value.strip()
    return parsed


async def establish(peer: Peer, call_id="bye-call@test", destination="9001"):
    """INVITE, read the 200, ACK. Returns the 200 OK we answered with."""
    peer.send(invite(call_id=call_id, destination=destination))
    await asyncio.sleep(0.2)
    answer = await asyncio.get_running_loop().run_in_executor(None, peer.response)
    peer.send(simple("ACK", call_id=call_id))
    await asyncio.sleep(0.2)
    return answer


async def collect(peer: Peer, *, timeout: float = 2.0) -> list[str]:
    """Everything the server sent us, until the socket goes quiet."""
    loop = asyncio.get_running_loop()
    seen = []
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        message = await loop.run_in_executor(None, peer.response)
        if message is None:
            break
        seen.append(message)
    return seen


# --- 1-6: the bank finishes the call ---------------------------------------


def test_the_bank_finishing_a_call_sends_exactly_one_bye():
    """The defect, stated as a test."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer)

        gateway.finish.set()                      # the bank is done with the call
        requests = [
            parse_request(raw)
            for raw in await collect(peer)
            if not raw.startswith("SIP/2.0")
        ]
        state = (requests, uas.byes_sent, uas.active_calls)
        peer.close()
        await uas.close()
        return state

    requests, byes_sent, active = run(scenario())

    byes = [r for r in requests if r["method"] == "BYE"]
    assert len(byes) == 1, f"expected one BYE, got {[r['method'] for r in requests]}"
    assert byes_sent == 1
    assert active == 0


def test_the_outbound_bye_carries_the_same_call_id():
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="same-id@test")
        gateway.finish.set()
        sent = [parse_request(r) for r in await collect(peer) if not r.startswith("SIP/2.0")]
        peer.close()
        await uas.close()
        return sent

    bye = [r for r in run(scenario()) if r["method"] == "BYE"][0]

    assert bye["call-id"] == "same-id@test"


def test_the_outbound_bye_reverses_the_dialog_tags():
    """Our To becomes the From; theirs becomes the To.

    The wrong way round produces a BYE for a dialog that does not exist, which
    is answered 481 and leaves the leg up exactly as before.
    """

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        answer = await establish(peer)
        gateway.finish.set()
        sent = [parse_request(r) for r in await collect(peer) if not r.startswith("SIP/2.0")]
        peer.close()
        await uas.close()
        return answer, sent

    answer, sent = run(scenario())
    bye = [r for r in sent if r["method"] == "BYE"][0]
    local_tag = re.search(r";tag=([^\s;]+)", parse_request(answer)["to"]).group(1)

    # Our tag — the one we answered the INVITE with — is on the From.
    assert f"tag={local_tag}" in bye["from"]
    # The caller's tag is on the To.
    assert "tag=abcd" in bye["to"]
    # The local identity is the URI that was dialled, which is what the INVITE
    # addressed and what our 200 OK echoed back — not the gateway's own name.
    assert bye["from"].startswith("<sip:9001@")
    assert bye["to"].startswith("<sip:anon@")


def test_the_local_tag_is_the_one_from_the_two_hundred_ok():
    """Regenerating it would address a dialog nobody has."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        answer = await establish(peer)
        gateway.finish.set()
        sent = [parse_request(r) for r in await collect(peer) if not r.startswith("SIP/2.0")]
        peer.close()
        await uas.close()
        return answer, sent

    answer, sent = run(scenario())
    answered_to = parse_request(answer)["to"]
    bye_from = [r for r in sent if r["method"] == "BYE"][0]["from"]

    assert answered_to == bye_from


def test_the_outbound_bye_has_a_valid_cseq_and_via():
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer)
        gateway.finish.set()
        sent = [parse_request(r) for r in await collect(peer) if not r.startswith("SIP/2.0")]
        peer.close()
        await uas.close()
        return sent

    bye = [r for r in run(scenario()) if r["method"] == "BYE"][0]

    number, _, method = bye["cseq"].partition(" ")
    assert method == "BYE"
    assert int(number) >= 1
    assert bye["via"].startswith("SIP/2.0/UDP")
    assert "branch=z9hG4bK" in bye["via"]
    assert bye["max-forwards"] == "70"
    assert bye["content-length"] == "0"
    assert bye["start_line"].startswith("BYE sip:")


def test_the_dialog_and_its_media_are_released_when_the_bank_finishes():
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer)
        source = gateway.calls[0]
        media_port = source.port

        gateway.finish.set()
        await collect(peer, timeout=1.0)
        state = (uas.active_calls, source.closed, media_port)
        peer.close()
        await uas.close()
        return state

    active, closed, media_port = run(scenario())

    assert active == 0, "the dialog leaked"
    assert closed is True, "the media source was not released"
    # And the media port is free again.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", media_port))
    probe.close()


# --- 7-9: the peer ends first, and races -----------------------------------


def test_a_peer_bye_does_not_produce_a_second_outbound_bye():
    """Their dialog is already gone; a BYE back would name a dead call."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="peer-first@test")

        peer.send(simple("BYE", call_id="peer-first@test"))
        await asyncio.sleep(0.3)
        gateway.finish.set()                       # handle_call unwinds after
        await asyncio.sleep(0.4)

        sent = [parse_request(r) for r in await collect(peer, timeout=1.0)
                if not r.startswith("SIP/2.0")]
        state = (sent, uas.byes_sent, uas.active_calls)
        peer.close()
        await uas.close()
        return state

    sent, byes_sent, active = run(scenario())

    assert [r for r in sent if r["method"] == "BYE"] == []
    assert byes_sent == 0
    assert active == 0


def test_an_inbound_bye_racing_the_bank_yields_at_most_one_bye():
    """Exactly one of the two paths claims the dialog."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="race@test")
        source = gateway.calls[0]

        # Both endings at once.
        gateway.finish.set()
        peer.send(simple("BYE", call_id="race@test"))
        await asyncio.sleep(0.5)

        sent = [parse_request(r) for r in await collect(peer, timeout=1.0)
                if not r.startswith("SIP/2.0")]
        state = (len([r for r in sent if r["method"] == "BYE"]),
                 uas.byes_sent, uas.active_calls, source.closed)
        peer.close()
        await uas.close()
        return state

    byes, byes_sent, active, closed = run(scenario())

    assert byes <= 1, "the racing paths both sent a BYE"
    assert byes_sent == byes
    assert active == 0, "the dialog leaked"
    assert closed is True


def test_a_duplicate_inbound_bye_is_still_harmless():
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="dupe-bye@test")

        for _ in range(4):
            peer.send(simple("BYE", call_id="dupe-bye@test"))
            await asyncio.sleep(0.1)
        gateway.finish.set()
        await asyncio.sleep(0.3)

        answers = [r for r in await collect(peer, timeout=1.0) if r.startswith("SIP/2.0")]
        state = (uas.active_calls, uas.byes_sent, len(answers))
        peer.close()
        await uas.close()
        return state

    active, byes_sent, answered = run(scenario())

    assert active == 0
    assert byes_sent == 0
    # Every retransmitted BYE is answered; ignoring one would have the peer
    # retransmit until it timed the dialog out.
    assert answered >= 4


# --- 10-12: many calls, shutdown, and responses ----------------------------


def test_each_of_several_calls_gets_its_own_correctly_addressed_bye():
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peers = [Peer(port) for _ in range(3)]
        for index, peer in enumerate(peers):
            await establish(peer, call_id=f"multi-{index}@test")

        gateway.finish.set()
        await asyncio.sleep(0.6)

        byes = []
        for peer in peers:
            for raw in await collect(peer, timeout=1.0):
                if not raw.startswith("SIP/2.0"):
                    byes.append(parse_request(raw))
        state = (byes, uas.byes_sent, uas.active_calls)
        for peer in peers:
            peer.close()
        await uas.close()
        return state

    byes, byes_sent, active = run(scenario())

    assert byes_sent == 3
    assert active == 0
    call_ids = {b["call-id"] for b in byes if b["method"] == "BYE"}
    assert call_ids == {"multi-0@test", "multi-1@test", "multi-2@test"}
    # Each BYE carries its own dialog's tags, not another call's.
    from_tags = {b["from"] for b in byes if b["method"] == "BYE"}
    assert len(from_tags) == 3


def test_closing_the_server_still_releases_every_dialog():
    """Shutdown is not a call ending; it releases without a burst of BYEs."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peers = [Peer(port) for _ in range(3)]
        for index, peer in enumerate(peers):
            await establish(peer, call_id=f"shut-{index}@test")
        during = uas.active_calls

        gateway.finish.set()
        await uas.close()
        await asyncio.sleep(0.2)

        state = (during, uas.active_calls, uas.byes_sent,
                 [source.closed for source in gateway.calls])
        for peer in peers:
            peer.close()
        return state

    during, after, byes_sent, closed = run(scenario())

    assert during == 3
    assert after == 0
    assert closed == [True, True, True]
    assert byes_sent == 0, "shutdown should not emit BYEs"


def test_a_sip_response_neither_creates_nor_changes_a_dialog():
    """Responses are ignored by design; this asserts the design is harmless."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)

        # A response arriving out of nowhere, as the 200 to our BYE would.
        for reply in (
            "SIP/2.0 200 OK\r\nCall-ID: ghost@test\r\nCSeq: 1 BYE\r\n"
            "Content-Length: 0\r\n\r\n",
            "SIP/2.0 481 Call/Transaction Does Not Exist\r\n"
            "Call-ID: ghost@test\r\nCSeq: 1 BYE\r\nContent-Length: 0\r\n\r\n",
        ):
            peer.send(reply.encode())
        await asyncio.sleep(0.3)
        before = uas.active_calls

        # And one arriving for a live call does not disturb it either.
        await establish(peer, call_id="live@test")
        peer.send(
            b"SIP/2.0 200 OK\r\nCall-ID: live@test\r\nCSeq: 1 BYE\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await asyncio.sleep(0.3)
        state = (before, uas.active_calls, uas.byes_sent)

        gateway.finish.set()
        await asyncio.sleep(0.3)
        peer.close()
        await uas.close()
        return state

    before, during, byes_sent = run(scenario())

    assert before == 0, "a response created a dialog"
    assert during == 1, "a response destroyed a live dialog"
    assert byes_sent == 0


def test_a_failed_gateway_task_still_ends_the_sip_leg():
    """A call that ends by raising is still a call that has ended."""

    class FailingGateway:
        def __init__(self):
            self.calls = []

        async def handle_call(self, source):
            self.calls.append(source)
            await asyncio.sleep(0.2)
            raise RuntimeError("the bank fell over")

    async def scenario():
        gateway = FailingGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="failing@test")
        await asyncio.sleep(0.5)

        sent = [parse_request(r) for r in await collect(peer, timeout=1.0)
                if not r.startswith("SIP/2.0")]
        state = (len([r for r in sent if r["method"] == "BYE"]),
                 uas.active_calls, gateway.calls[0].closed)
        peer.close()
        await uas.close()
        return state

    byes, active, closed = run(scenario())

    assert byes == 1, "a failed call left the leg up"
    assert active == 0
    assert closed is True


def test_the_bye_is_addressed_at_the_peers_contact_when_it_offers_one():
    """A BYE goes to the leg that answered, not the number that was dialled."""

    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)

        with_contact = invite(call_id="contact@test").decode().replace(
            "CSeq: 1 INVITE\r\n",
            "CSeq: 1 INVITE\r\nContact: <sip:fs@10.9.9.9:5070>\r\n",
        )
        peer.send(with_contact.encode())
        await asyncio.sleep(0.2)
        await asyncio.get_running_loop().run_in_executor(None, peer.response)
        peer.send(simple("ACK", call_id="contact@test"))
        await asyncio.sleep(0.2)

        gateway.finish.set()
        sent = [parse_request(r) for r in await collect(peer, timeout=1.0)
                if not r.startswith("SIP/2.0")]
        peer.close()
        await uas.close()
        return sent

    bye = [r for r in run(scenario()) if r["method"] == "BYE"][0]

    assert bye["start_line"] == "BYE sip:fs@10.9.9.9:5070 SIP/2.0"


def test_the_bye_logs_nothing_sensitive(caplog):
    async def scenario():
        gateway = EndableGateway()
        uas, port = await _serve(gateway)
        peer = Peer(port)
        await establish(peer, call_id="quiet@test")
        gateway.finish.set()
        await collect(peer, timeout=1.0)
        peer.close()
        await uas.close()

    with caplog.at_level("INFO", logger="gateway.sip"):
        run(scenario())

    assert "quiet@test" in caplog.text  # the call id is operational, and useful
    for forbidden in ("4821", "DEMO001", "media_token", "pin", "secret"):
        assert forbidden not in caplog.text.lower(), forbidden
