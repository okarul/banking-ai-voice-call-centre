"""PIN hashing helpers.

Kept outside the database package so the later authentication phase can reuse
it without importing database code.

Uses PBKDF2-HMAC-SHA256 from the Python standard library, so no extra
dependency is required. Stored format::

    pbkdf2_sha256$<iterations>$<base64 salt>$<base64 hash>
"""

import base64
import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def hash_pin(pin: str) -> str:
    """Hash a PIN. A fresh random salt is generated for every call."""
    salt = os.urandom(SALT_BYTES)
    derived = _derive(pin, salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${_b64(salt)}${_b64(derived)}"


def verify_pin(pin: str, stored_hash: str) -> bool:
    """Check a PIN against a stored hash using a constant-time comparison."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored_hash.split("$")
    except (ValueError, AttributeError):
        return False

    if algorithm != ALGORITHM:
        return False

    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = _derive(pin, salt, int(iterations))
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(derived, expected)
