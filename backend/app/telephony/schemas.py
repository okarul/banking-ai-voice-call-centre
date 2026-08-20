"""The only shape an inbound provider event is allowed to have.

Strict in both directions. Every field the handler needs is required and typed;
every field it does not need is **rejected**, not ignored — `extra="forbid"`.

Forbidding unknown fields rather than dropping them is the deliberate choice
here. Silently ignoring extras would mean a payload carrying
`"authenticated": true` or `"customer_id": "DEMO001"` is accepted, logged as
valid, and differs from a genuine event only in ways nothing ever looks at. One
careless `**payload` downstream and the caller has named themselves. Rejecting
outright means such a payload never reaches the application at all, and the
attempt is visible as a rejection rather than invisible as a success.

That is also why there is no passthrough field for arbitrary provider JSON. A
provider will send more than this — network details, codecs, its own routing
metadata, a display name — and the answer to almost all of it is that the bank
does not need it, so it does not take it.

Note what is *not* here: nothing that could identify a banking customer. There
is no `customer_id`, no `authenticated`, no `verified`. An inbound event can
establish exactly one fact — that a telephone call exists — and the schema is
shaped so that it cannot express any other.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelephonyEventType(str, Enum):
    """The call-control moments this phase handles.

    Two, because two is what Phase 2 needs: a call arrived, and a call
    finished. Provider vocabularies are richer than this — `initiated`,
    `ringing`, `answered`, `completed` — and they are deliberately **not**
    aliased in here. Mapping a provider's words onto these belongs in that
    provider's adapter, where the mapping can be checked against that
    provider's documentation. Guessing that `completed` must mean `ended`
    is how a call gets torn down by an event that meant something else.
    """

    INCOMING = "incoming"
    ENDED = "ended"


class InboundCallEvent(BaseModel):
    """One call-control notification from a telephony provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(..., min_length=1, max_length=40)
    # Identifies this notification. Two notifications about the same call have
    # different event ids, which is what lets a retry be told from a new fact.
    provider_event_id: str = Field(..., min_length=1, max_length=64)
    # Identifies the call. Stable across every notification about it.
    provider_call_id: str = Field(..., min_length=1, max_length=64)
    event_type: TelephonyEventType
    event_timestamp: datetime

    # The calling and called numbers. `source` is accepted because providers
    # send it and rejecting it would fail genuine events — but it is discarded
    # rather than stored: see `app.telephony.service`. It is never an identity.
    source: str | None = Field(default=None, max_length=32)
    destination: str | None = Field(default=None, max_length=32)

    @field_validator("provider_event_id", "provider_call_id")
    @classmethod
    def _plain_identifier(cls, value: str) -> str:
        """Identifiers must look like identifiers.

        These strings end up in a database column, an audit record and an
        operator's dashboard. Constraining them to an unambiguous character set
        means none of those has to wonder what an id containing a quote, a
        newline or a control character will do on arrival. Provider ids are
        untrusted text; this is the point where they stop being arbitrary.
        """
        if not value:
            raise ValueError("must not be empty")
        if not all(character.isalnum() or character in "-_:." for character in value):
            raise ValueError("must be alphanumeric with - _ : . only")
        return value


class InboundEventAccepted(BaseModel):
    """What the provider is told when an event was handled.

    Minimal on purpose. The provider needs to know its event was processed so
    it stops retrying; it does not need our session identifiers, our capacity
    position, or anything about the caller.

    `duplicate` is reported honestly rather than hidden, because a provider
    seeing its retries acknowledged as duplicates is a provider behaving
    correctly, and an operator reading the logs should be able to see it.
    """

    status: str
    provider_event_id: str
    duplicate: bool = False
