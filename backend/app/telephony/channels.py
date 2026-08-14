"""What a voice channel is, and — more importantly — what it is not.

A channel describes how the audio reached the bank. That is the whole of its
authority. It decides nothing about who is calling, what they may see, or
whether they have been verified.

    Channel.WEBRTC   the browser page, working today
    Channel.PHONE    an ordinary telephone over SIP, not yet implemented

The distinction matters most on the telephone. A SIP invitation arrives with a
`From` header and a caller number, and both are trivially forged by whoever
places the call — caller ID spoofing is a solved problem for an attacker and a
completely unsolved one for the recipient. A bank that reads an inbound number
and decides "this is DEMO001" has authenticated nobody.

So the rule is enforced structurally rather than remembered: `CallerMetadata`
below has no route to a customer id, `identifies_customer()` returns False for
every channel, and the adapters expose no method that could set one. Identity
is established by the deterministic PIN check and lives on
`session.customer_id`, which is the only thing the authorization guards read.

Nothing in this module opens a connection. `WebRTCChannelAdapter` is a thin
description of the flow that already exists, so that a future
`SIPPhoneChannelAdapter` has a shape to match — the working browser path is not
rerouted through it, because rewriting a proven flow to satisfy an abstraction
is how working systems break.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Channel(str, Enum):
    """How the caller's audio reaches the bank."""

    WEBRTC = "WEBRTC"
    PHONE = "PHONE"


# What an operator sees. The customer never sees any of this: a caller is not
# told which transport they arrived on, and a refusal never mentions SIP.
CHANNEL_LABELS = {
    Channel.WEBRTC: "Browser (WebRTC)",
    Channel.PHONE: "Telephone (SIP)",
}

DEFAULT_CHANNEL = Channel.WEBRTC


def normalise_channel(value) -> Channel:
    """Read a channel from stored or supplied data, defaulting to the browser.

    Anything unrecognised becomes WEBRTC rather than raising. This is
    operational metadata: a row with an odd channel value should still appear
    on the dashboard, and an unreadable field must never be able to interrupt a
    banking call.
    """
    if isinstance(value, Channel):
        return value
    try:
        return Channel(str(value).strip().upper())
    except (ValueError, AttributeError):
        return DEFAULT_CHANNEL


# --- caller metadata ---------------------------------------------------------


# Country calling codes this demo may plausibly see. Not exhaustive, and
# deliberately not a full E.164 table: a wrong guess here only means a slightly
# less readable mask, never a leaked digit, so a short list beats a dependency.
# Checked longest-first, because "65" and "652" would otherwise both match.
_COUNTRY_CODES = (
    "971", "852", "886", "353", "358", "351",
    "65", "60", "62", "63", "66", "84", "91", "44", "49", "33", "34",
    "39", "31", "61", "64", "81", "82", "86", "27", "20",
    "7", "1",
)


def mask_caller_number(number: str | None) -> str | None:
    """A caller number reduced to something safe to show an operator.

    `+6531252836` becomes `+65 **** 2836`. The last four digits are enough for
    an operator to match a call a customer is describing to them; the middle is
    not theirs to keep.

    Preferably this is never called at all — see `CallerMetadata`, which does
    not retain the number in the first place.
    """
    if not number:
        return None

    text = str(number)
    bare = re.sub(r"\D", "", text)
    tail = bare[-4:]
    if len(tail) < 4:
        return "****"

    country = ""
    if text.strip().startswith("+"):
        for code in _COUNTRY_CODES:
            # The code must be followed by enough digits to be a real number,
            # so a short string cannot be read as country code plus nothing.
            if bare.startswith(code) and len(bare) - len(code) >= 6:
                country = f"+{code} "
                break

    return f"{country}**** {tail}"


@dataclass(frozen=True)
class CallerMetadata:
    """The little a channel is allowed to remember about an inbound call.

    Deliberately small, and deliberately without a customer id. A provider will
    offer far more than this — the full number, a display name, network
    details, its own identifiers — and the answer to almost all of it is that
    the bank does not need it, so it does not take it.

    Only `masked_number` exists, and only because an operator may one day need
    to match a call a customer is describing to them. The full number is never
    stored: it is masked on the way in, by `from_provider`, so there is no
    field for it to sit in.
    """

    channel: Channel
    provider_call_id: str | None = None
    masked_number: str | None = None

    @classmethod
    def from_provider(
        cls,
        channel: Channel,
        *,
        provider_call_id: str | None = None,
        caller_number: str | None = None,
        retain_masked_number: bool = False,
    ) -> "CallerMetadata":
        """Take the minimum from a provider payload and discard the rest.

        `retain_masked_number` defaults to False: for this demonstration there
        is no operational need for a caller's number at all, so the safest
        version of the data is none of it. When it is turned on, only the
        masked form is kept — the full number never reaches an attribute.
        """
        masked = mask_caller_number(caller_number) if retain_masked_number else None
        return cls(
            channel=channel,
            provider_call_id=str(provider_call_id) if provider_call_id else None,
            masked_number=masked,
        )

    def identifies_customer(self) -> bool:
        """Always False. Present so the rule is stated in code, not just prose.

        No channel metadata authenticates anybody. A caller number, a SIP From
        header and a provider call id can all be set to whatever the caller
        likes, so none of them may select a banking customer.
        """
        return False

    def to_safe_dict(self) -> dict:
        """Operator-safe view. Carries no customer identity and no full number."""
        return {
            "channel": self.channel.value,
            "provider_call_id": self.provider_call_id,
            "masked_number": self.masked_number,
        }


# --- adapter boundary --------------------------------------------------------


@runtime_checkable
class VoiceChannelAdapter(Protocol):
    """The shape a voice channel presents to the rest of the application.

    Transport concerns only: opening a line, closing it, describing itself. An
    adapter never reads an account, never checks a PIN and never decides who is
    calling — those live in the existing application services, and a channel
    that could reach them would be a second path around the authorization
    guards.
    """

    channel: Channel

    def is_enabled(self) -> bool:
        """Whether this channel may accept calls at all."""
        ...

    def describe(self) -> dict:
        """Safe description for operators. No credentials, no provider secrets."""
        ...


@dataclass(frozen=True)
class WebRTCChannelAdapter:
    """The browser channel, described rather than reimplemented.

    The working WebRTC flow is untouched: `/api/call/*`, `browser_call_manager`
    and the page's own peer connection continue to do exactly what they did.
    This object exists so the future SIP adapter has a defined shape to match,
    and so the dashboard can name a channel without importing transport code.

    Wrapping the live flow in this abstraction would mean rerouting a proven
    path for the sake of symmetry, which is a poor trade against the risk.
    """

    channel: Channel = Channel.WEBRTC

    def is_enabled(self) -> bool:
        """Always available. The browser channel is the working baseline."""
        return True

    def describe(self) -> dict:
        return {
            "channel": self.channel.value,
            "label": CHANNEL_LABELS[self.channel],
            "enabled": True,
            "transport": "browser WebRTC to the provider, tools relayed via the backend",
        }
