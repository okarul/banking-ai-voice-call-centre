"""The reference media gateway: a separate service, not part of the bank.

The banking backend answers calls; it does not speak to telephone networks. Two
things stand between a caller and it, and this package is the second:

    DIDWW  ──SIP/RTP──►  [ SIP termination ]  ──►  [ this gateway ]  ──►  bank
                              not built              built here

**What this package does.** Everything the backend's Phase 3 media contract
requires of the far side: sign and post the call-control events, hold the
short-lived credential it is issued, attach the media socket with it, relay
µ-law frames in both directions for the life of the call, and report the ending.

**What it does not do.** Terminate SIP. That needs a SIP stack in-process or a
media server in front, and it is the one piece that cannot be written honestly
without a provider to test against — so it is a named seam (`sources.SipSource`)
rather than a plausible-looking implementation. `SyntheticSource` fills the same
seam with frames a test supplies, which is what makes the five-call suite
runnable with no provider, no telephone and no cost.

**The boundary is real.** Nothing here imports `app.*`. The gateway knows the
backend only as a URL, a shared secret and a wire format — exactly what a
FreeSWITCH or Asterisk deployment would know — so nothing about the bank's
internals can leak into it, and a test asserts the boundary holds.

Configuration is separate too: `GATEWAY_*` variables, its own `.env` in a real
deployment, on its own host.
"""

from gateway.config import GatewaySettings, gateway_settings
from gateway.service import MediaGateway
from gateway.sources import CallSource, SyntheticSource

__all__ = [
    "CallSource",
    "GatewaySettings",
    "MediaGateway",
    "SyntheticSource",
    "gateway_settings",
]
