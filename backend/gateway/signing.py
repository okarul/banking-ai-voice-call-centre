"""Signing an event the way the bank verifies one.

Deliberately a second, independent implementation rather than an import of
`app.telephony.signature`. Two reasons, and the second is the important one:

* The gateway is a separate service. In a real deployment it is a different
  process on a different host and cannot import the bank's code at all, so
  writing it as though it could would hide a dependency that does not exist.

* A test that signs with the same function the backend verifies with can pass
  while both are wrong together. Two implementations that must agree are a
  check on each other, and the suite asserts they produce identical bytes for
  the same input.

The scheme, matching the contract:

    signed    = f"{timestamp}.{raw_body}"
    signature = "v1=" + hmac_sha256(secret, signed).hexdigest()

The raw body matters. The signature covers the exact bytes sent, so the body
must be serialised once and both signed and transmitted — re-serialising
between the two is how a signature that looks right stops matching.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Telephony-Timestamp"
SIGNATURE_HEADER = "X-Telephony-Signature"
MEDIA_TOKEN_HEADER = "X-Telephony-Media-Token"

_VERSION_PREFIX = "v1="


def sign(secret: str, body: bytes, *, timestamp: str | None = None) -> dict[str, str]:
    """Headers for one signed request, including the timestamp it was signed at.

    Returned together on purpose: the timestamp is inside the signed string, so
    a caller that generated one and sent another would produce a signature that
    can never verify. Handing back both makes them impossible to mismatch.
    """
    stamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        stamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: _VERSION_PREFIX + digest,
    }
