"""Run the gateway.

    python -m gateway check
    python -m gateway selftest --calls 5
    python -m gateway serve

`check` reports configuration and reachability without placing a call.
`selftest` drives synthetic calls against a running backend — no telephone, no
provider, no cost — and is the command to run before pointing anything real at
this. `serve` answers SIP, for a media server (FreeSWITCH) to bridge calls to.

Nothing here prints the signing secret or a media credential.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from gateway.config import gateway_settings
from gateway.service import CallOutcome, MediaGateway
from gateway.sipserver import SipUas
from gateway.sources import FRAME_BYTES, SILENCE, SyntheticSource
from gateway.udp_source import MediaPortAllocator


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-8s %(name)s %(message)s",
    )


async def _check() -> int:
    print("gateway configuration:")
    for name, value in gateway_settings.safe_summary().items():
        print(f"  {name:18} {value}")
    print(f"  webhook_secret     {'set' if gateway_settings.webhook_secret else 'NOT SET'}")

    if not gateway_settings.configured:
        print("\nNOT READY: set GATEWAY_WEBHOOK_SECRET to the backend's "
              "TELEPHONY_WEBHOOK_SECRET")
        return 1

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=gateway_settings.request_timeout,
            verify=gateway_settings.verify_tls,
        ) as http:
            health = await http.get(gateway_settings.backend_url + "/health")
            print(f"\n  backend /health    {health.status_code} {health.text.strip()}")
            schema = await http.get(gateway_settings.backend_url + "/openapi.json")
            registered = "/api/telephony/incoming" in schema.json().get("paths", {})
            print(f"  telephony route    {'registered' if registered else 'NOT REGISTERED'}")
            if not registered:
                print("\nNOT READY: the backend has telephony disabled, or no "
                      "webhook secret. Set TELEPHONY_ENABLED=true and "
                      "TELEPHONY_WEBHOOK_SECRET, then restart it.")
                return 1
    except Exception as error:
        print(f"\nNOT READY: backend unreachable ({type(error).__name__})")
        return 1

    print("\nREADY: the gateway can reach the bank and the route is registered.")
    return 0


async def _serve() -> int:
    """Answer SIP until interrupted, handing accepted calls to the bank."""
    if not gateway_settings.configured:
        print("NOT CONFIGURED: set GATEWAY_WEBHOOK_SECRET")
        return 1

    if gateway_settings.sip_publicly_bound and not gateway_settings.sip_allowed_peers:
        # Refusing to start is deliberate. A SIP port on a reachable address
        # with no source restriction is found by scanners within hours, and
        # every call they place is one this gateway would offer to a bank.
        print(
            f"REFUSING TO START: GATEWAY_SIP_HOST is {gateway_settings.sip_host}, "
            "which is not loopback, and GATEWAY_SIP_ALLOWED_PEERS is empty.\n"
            "Set the carrier's signalling addresses before answering SIP on a "
            "reachable interface."
        )
        return 1

    gateway = MediaGateway()
    allocator = MediaPortAllocator(
        port_low=gateway_settings.media_port_low,
        port_high=gateway_settings.media_port_high,
    )
    uas = SipUas(
        gateway=gateway,
        allocator=allocator,
        advertise_host=gateway_settings.sip_advertise_host,
        allowed_peers=gateway_settings.sip_allowed_peers or None,
    )
    port = await uas.listen(gateway_settings.sip_host, gateway_settings.sip_port)

    print(f"answering SIP on {gateway_settings.sip_host}:{port}")
    print(f"advertising media at {gateway_settings.sip_advertise_host}")
    print(f"media ports {gateway_settings.media_port_low}-{gateway_settings.media_port_high}")
    print(
        "source restriction: "
        + (
            f"{len(gateway_settings.sip_allowed_peers)} peer(s)"
            if gateway_settings.sip_allowed_peers
            else "loopback binding only"
        )
    )
    print("Ctrl-C to stop.")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await uas.close()
        await gateway.aclose()
    return 0


async def _selftest(calls: int, seconds: float) -> int:
    """Drive synthetic calls end to end. No telephone, no provider, no cost."""
    if not gateway_settings.configured:
        print("NOT CONFIGURED: set GATEWAY_WEBHOOK_SECRET")
        return 1

    gateway = MediaGateway()
    sources = [SyntheticSource(f"selftest-{n:03d}") for n in range(calls)]

    async def one(source: SyntheticSource) -> str:
        handling = asyncio.create_task(gateway.handle_call(source))
        await asyncio.sleep(0.3)  # let the call attach before speaking
        for _ in range(int(seconds * 50)):  # 50 frames a second
            source.speak(SILENCE)
            await asyncio.sleep(0.02)
        source.hang_up()
        return await handling

    try:
        outcomes = await asyncio.gather(*(one(source) for source in sources))
    finally:
        await gateway.aclose()

    print(f"\nplaced {calls} synthetic call(s) of ~{seconds}s each")
    for source, outcome in zip(sources, outcomes):
        print(f"  {source.call_id:16} {outcome:12} heard {len(source.played)} frame(s)")
    print(f"\ncompleted={gateway.completed} refused={gateway.refused} "
          f"failed={gateway.failed}")

    # Every frame the bank sent back must be a telephone frame.
    wrong = [f for s in sources for f in s.played if len(f) != FRAME_BYTES]
    if wrong:
        print(f"FAIL: {len(wrong)} frame(s) were not {FRAME_BYTES} bytes")
        return 1
    return 0 if gateway.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway", description=__doc__)
    parser.add_argument(
        "command", choices=["check", "selftest", "serve"], help="what to do"
    )
    parser.add_argument("--calls", type=int, default=1, help="synthetic calls to place")
    parser.add_argument(
        "--seconds", type=float, default=1.0, help="length of each synthetic call"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _log(args.verbose)

    if args.command == "check":
        return asyncio.run(_check())
    if args.command == "selftest":
        return asyncio.run(_selftest(args.calls, args.seconds))

    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
