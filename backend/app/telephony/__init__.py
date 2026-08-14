"""Foundation for a second voice channel. Nothing here places a call.

The bank currently answers on one channel: a browser, over WebRTC. This package
prepares for a second — an ordinary telephone, over SIP — without implementing
it and without disturbing the one that works.

Everything in here is inert while `TELEPHONY_ENABLED` is false, which is the
default. No provider is contacted, no SIP stack is imported, no route is
registered, and the browser path runs on exactly the code it ran on before.

The important idea is the boundary. A channel is *how the audio arrives*, and
that is all it is:

    channel  -> transport, capacity accounting, operational reporting
    session  -> who the customer is, what they may see, what they are told

Those two must never be confused. A telephone number, a SIP `From` header, a
provider call id — none of them says who is calling, because any of them can be
forged by whoever places the call. Identity comes from the deterministic PIN
check and lives on `session.customer_id`, exactly as it does today. See
`app.telephony.channels` for the rule expressed in code, and
`docs/TELEPHONY_THREAT_MODEL.md` for what it defends against.
"""

from app.telephony.channels import (
    CHANNEL_LABELS,
    Channel,
    VoiceChannelAdapter,
    WebRTCChannelAdapter,
    mask_caller_number,
    normalise_channel,
)

__all__ = [
    "CHANNEL_LABELS",
    "Channel",
    "VoiceChannelAdapter",
    "WebRTCChannelAdapter",
    "mask_caller_number",
    "normalise_channel",
]
