"""Deciding whether an inbound event really came from the provider.

This is the first thing that touches a request from the public internet, and it
runs before the body is parsed, before anything is logged, and long before a
session exists. Its only question is: *did whoever holds the shared secret
produce this exact byte sequence, recently?*

The scheme is HMAC-SHA256 over `timestamp.body`, which is the shape most voice
providers converge on:

    signed = f"{timestamp}.{raw_body}"
    signature = hmac_sha256(secret, signed).hexdigest()

Three properties matter, and each is doing separate work:

* **The secret** proves origin. Without it a request is a stranger's claim.
* **The body** is inside the signed string, so a payload edited in flight no
  longer matches. Signing only the timestamp would authenticate the envelope
  and leave the contents free to rewrite.
* **The timestamp** is inside it too, and is separately checked against the
  clock. Signing the body alone would produce a token that stays valid for
  ever — capture one and you may replay it whenever you like.

**This is an abstraction, not a claim of DIDWW compatibility.** No provider
here has published a signing scheme this implements. `verify_provider_request`
is the seam: when a provider's real rules are known, they are implemented as
another `_SCHEMES` entry and selected by configuration, and everything upstream
of this module stays as it is. The default scheme is used by the demonstration
and by the tests, with deterministic test credentials.

Nothing here logs. A verification failure is a category returned to the caller,
which decides what to record — a module that logged the body it was in the
middle of rejecting would be the leak it exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

# Provider-neutral header names. A real provider will use its own; that is a
# per-scheme detail, which is why they live beside the scheme rather than being
# hard-coded into the route.
TIMESTAMP_HEADER = "X-Telephony-Timestamp"
SIGNATURE_HEADER = "X-Telephony-Signature"

# Signature values may be bare hex or carry a version prefix (`v1=...`), the way
# several providers version their schemes. Accepting both costs nothing and
# means a future scheme change does not need a new header.
_VERSION_PREFIX = "v1="

# The largest body this endpoint will consider. A signature check has to hash
# the whole body, so an unbounded read is work an unauthenticated stranger can
# ask for. 64 KiB is far beyond any plausible call-control event.
MAX_BODY_BYTES = 64 * 1024


class VerificationFailure(str):
    """A reason category. Deliberately a string: it is safe to log."""


# Every one of these means "rejected". They are distinguished for the operator's
# audit trail, never for the sender: the response says only that verification
# failed, because an error explaining *which* part failed is a tool for the next
# attempt.
NOT_CONFIGURED = VerificationFailure("NOT_CONFIGURED")
BODY_TOO_LARGE = VerificationFailure("BODY_TOO_LARGE")
SIGNATURE_MISSING = VerificationFailure("SIGNATURE_MISSING")
TIMESTAMP_MISSING = VerificationFailure("TIMESTAMP_MISSING")
TIMESTAMP_MALFORMED = VerificationFailure("TIMESTAMP_MALFORMED")
TIMESTAMP_STALE = VerificationFailure("TIMESTAMP_STALE")
TIMESTAMP_IN_FUTURE = VerificationFailure("TIMESTAMP_IN_FUTURE")
SIGNATURE_INVALID = VerificationFailure("SIGNATURE_INVALID")


class ProviderVerificationError(Exception):
    """Raised when an inbound event is not provably from the provider."""

    def __init__(self, reason: VerificationFailure) -> None:
        super().__init__(str(reason))
        self.reason = reason


@dataclass(frozen=True)
class VerifiedRequest:
    """What survived verification. Not yet parsed, and not yet trusted as data.

    Passing verification means the bytes came from whoever holds the secret and
    arrived recently. It says nothing about whether the JSON inside is
    well-formed, whether the event is one we handle, or — most importantly —
    who is holding the telephone.
    """

    body: bytes
    timestamp: datetime


def _digest(secret: str, timestamp: str, body: bytes) -> str:
    """The expected signature for this exact timestamp and these exact bytes."""
    signed = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Produce a signature the way a provider would.

    Used by the tests and by local development to generate genuine requests.
    It is the same computation `verify_provider_request` checks against, on
    purpose: a test that signed with different code could pass while the real
    verification was broken.
    """
    return _VERSION_PREFIX + _digest(secret, timestamp, body)


def _parse_timestamp(raw: str) -> datetime:
    """Read a Unix-seconds timestamp, rejecting anything else."""
    try:
        seconds = int(raw.strip())
    except (TypeError, ValueError):
        raise ProviderVerificationError(TIMESTAMP_MALFORMED) from None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ProviderVerificationError(TIMESTAMP_MALFORMED) from None


def verify_provider_request(
    *,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: str | None,
    tolerance_seconds: int,
    now: datetime | None = None,
) -> VerifiedRequest:
    """Verify one inbound provider request, or raise with a reason category.

    `now` is injected rather than read from the clock inside, so the staleness
    rules can be tested at chosen instants instead of by sleeping.

    The order of the checks is not arbitrary. Cheap structural checks come
    first, so a malformed request is refused without spending a hash; the HMAC
    comparison comes last, once there is a well-formed timestamp to include in
    the signed string.
    """
    if not secret:
        # Refusing everything is the only safe behaviour: the alternative —
        # skipping verification when unconfigured — turns a missing environment
        # variable into an open door.
        raise ProviderVerificationError(NOT_CONFIGURED)

    if len(body) > MAX_BODY_BYTES:
        raise ProviderVerificationError(BODY_TOO_LARGE)

    if not signature_header:
        raise ProviderVerificationError(SIGNATURE_MISSING)
    if not timestamp_header:
        raise ProviderVerificationError(TIMESTAMP_MISSING)

    stamped = _parse_timestamp(timestamp_header)
    moment = now or datetime.now(timezone.utc)
    drift = (moment - stamped).total_seconds()

    # Both directions are checked. Only rejecting old timestamps would let a
    # sender with a far-future clock — or an attacker choosing one — mint a
    # request that stays valid for as long as they like.
    if drift > tolerance_seconds:
        raise ProviderVerificationError(TIMESTAMP_STALE)
    if drift < -tolerance_seconds:
        raise ProviderVerificationError(TIMESTAMP_IN_FUTURE)

    supplied = signature_header.strip()
    if supplied.startswith(_VERSION_PREFIX):
        supplied = supplied[len(_VERSION_PREFIX) :]

    expected = _digest(secret, timestamp_header.strip(), body)

    # Constant-time. A plain `==` returns as soon as two bytes differ, and the
    # time it took is a measurement of how much of the signature was right —
    # enough, over many attempts, to construct a valid one byte at a time.
    if not hmac.compare_digest(supplied, expected):
        raise ProviderVerificationError(SIGNATURE_INVALID)

    return VerifiedRequest(body=body, timestamp=stamped)
