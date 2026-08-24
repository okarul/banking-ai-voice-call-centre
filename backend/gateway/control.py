"""The playback control protocol, gateway side.

Binary frames on the media socket are audio. Text frames are control, and there
are exactly two messages:

    backend -> gateway   {"type": "playback_boundary", "id": "<opaque>"}
    gateway -> backend   {"type": "playback_drained",  "id": "<same>"}

They exist because the two ends of that socket run at completely different
speeds. The backend hands over audio as fast as the socket will take it; the
gateway paces it onto RTP at 160 bytes every 20 ms, because that is the rate a
telephone plays. A twenty-second answer leaves the backend in a fraction of a
second and takes twenty seconds to reach the caller. The backend therefore
cannot know from its own queue when the caller has heard anything, and the
boundary is how it asks.

The id is an opaque per-call counter and nothing else. These messages carry no
transcript, no customer identity, no banking data and no credential — there is
nothing in them worth logging, and their contents are never logged.

This module is deliberately duplicated rather than shared with the application.
The gateway is a standalone reference implementation that a different one could
replace, so the two ends agree by wire contract, not by import. The application
side lives in `app.telephony.media`; the two must be changed together.
"""

from __future__ import annotations

import json

PLAYBACK_BOUNDARY = "playback_boundary"
PLAYBACK_DRAINED = "playback_drained"

# An id longer than this is not something we produced. Bounded so a malformed
# or hostile frame cannot be echoed back at size.
MAX_ID_LENGTH = 64


def playback_drained_message(boundary_id: str) -> str:
    """The acknowledgement, sent once the audio before it has been paced out."""
    return json.dumps({"type": PLAYBACK_DRAINED, "id": boundary_id})


def read_control_message(text) -> tuple[str, str] | None:
    """Parse a control frame, or None if it is not one we recognise.

    Strict on purpose. Anything unparseable, unknown, or the wrong shape is not
    ours to act on, and guessing at it is how a media socket starts doing
    something other than carrying one call's audio.
    """
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    kind = payload.get("type")
    boundary_id = payload.get("id")
    if kind not in (PLAYBACK_BOUNDARY, PLAYBACK_DRAINED):
        return None
    if not isinstance(boundary_id, str):
        return None
    if not boundary_id or len(boundary_id) > MAX_ID_LENGTH:
        return None
    return kind, boundary_id
