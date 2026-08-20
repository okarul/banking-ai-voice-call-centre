"""Run the gateway.

    python -m gateway check
    python -m gateway selftest --calls 5
    python -m gateway serve

`check` reports configuration and reachability without placing a call.
`selftest` drives synthetic calls against a running backend — no telephone, no
provider, no cost — and is the command to run before pointing anything real at
this. `serve` requires SIP termination, which is not implemented, and says so
rather than starting something that cannot answer.

Nothing here prints the signing secret or a media credential.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from gateway.config import gateway_settings
from gateway.service import CallOutcome, MediaGateway
from gateway.sources import FRAME_BYTES, SILENCE, SyntheticSource


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

    print(
        "serve: SIP termination is not implemented.\n\n"
        "This gateway relays audio between a call source and the bank, and it "
        "is complete.\nWhat it has no source for is a real telephone call: "
        "that needs a media server\nin front of it (FreeSWITCH, Asterisk) or a "
        "SIP stack inside it.\n\n"
        "See docs/PHASE4_LIVE_ACTIVATION.md, and use `selftest` to exercise "
        "everything else."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
