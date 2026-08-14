"""Deterministic customer authentication and spoken-input normalisation."""

from app.auth.authentication import (
    MAX_AUTHENTICATION_ATTEMPTS,
    authentication_status,
    submit_customer_id,
    submit_pin,
    verify_customer,
    verify_pin,
)
from app.auth.normalization import normalize_customer_id, normalize_pin

__all__ = [
    "MAX_AUTHENTICATION_ATTEMPTS",
    "authentication_status",
    "normalize_customer_id",
    "normalize_pin",
    "submit_customer_id",
    "submit_pin",
    "verify_customer",
    "verify_pin",
]
