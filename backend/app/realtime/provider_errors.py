"""Naming what the provider actually did, for our logs only.

Phase 12 measured a live ceiling and then had to argue, from timing traces,
about what kind of ceiling it was. That argument should not have to be repeated
from first principles every time: when a call cannot be opened, the category is
decided once, here, and written to the log.

Two audiences, deliberately different:

* **The operator's log** gets the specific category — RATE_LIMIT is a different
  problem from QUOTA_EXHAUSTED, and reporting both as "Realtime failed" wastes
  the one piece of information that would fix it.
* **The customer** gets one short sentence and nothing else. No provider name,
  no numbers, no configuration. See `app.realtime.realtime_manager.MESSAGES`.

Nothing here is a security control and nothing here changes behaviour. It reads
an exception and returns a string.
"""

import re

RATE_LIMIT = "RATE_LIMIT"
CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
NETWORK = "NETWORK"
TIMEOUT = "TIMEOUT"
UNKNOWN = "UNKNOWN"

CATEGORIES = frozenset(
    {
        RATE_LIMIT,
        CONCURRENCY_LIMIT,
        QUOTA_EXHAUSTED,
        PROVIDER_UNAVAILABLE,
        NETWORK,
        TIMEOUT,
        UNKNOWN,
    }
)

# Order matters. Quota is checked before rate limit because an exhausted balance
# is reported by some providers with a 429, and telling an operator to "wait for
# the rate limit to reset" when the account is actually out of credit sends them
# to the wrong screen.
_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        QUOTA_EXHAUSTED,
        ("insufficient_quota", "credit_balance", "billing_hard_limit",
         "exceeded your current quota", "quota"),
    ),
    (
        CONCURRENCY_LIMIT,
        ("concurrent", "concurrency", "too many active sessions",
         "max_active_sessions", "session limit"),
    ),
    (
        RATE_LIMIT,
        ("rate_limit", "rate limit", "too many requests", "429"),
    ),
    (
        TIMEOUT,
        ("timeout", "timed out", "timeouterror", "deadline"),
    ),
    (
        PROVIDER_UNAVAILABLE,
        ("service_unavailable", "server_error", "internal server error",
         "bad gateway", "503", "502", "500", "overloaded"),
    ),
    (
        NETWORK,
        ("connectionclosed", "connectionreset", "connecterror", "connection refused",
         "websocket", "ssl", "dns", "unreachable", "eof"),
    ),
)

_STATUS_CATEGORIES = {
    429: RATE_LIMIT,
    500: PROVIDER_UNAVAILABLE,
    502: PROVIDER_UNAVAILABLE,
    503: PROVIDER_UNAVAILABLE,
    504: TIMEOUT,
}


def classify_status(status_code: int | None) -> str | None:
    """Category implied by an HTTP status alone, or None if it says nothing."""
    if status_code is None:
        return None
    if status_code == 401 or status_code == 403:
        # Not a capacity problem. Named separately so it is never mistaken for
        # one: a bad key looks like an outage until somebody reads the status.
        return PROVIDER_UNAVAILABLE
    return _STATUS_CATEGORIES.get(status_code)


def classify_provider_error(error: BaseException | None, *, status_code=None) -> str:
    """Which provider condition this exception represents.

    Conservative by design. An exception that matches nothing becomes UNKNOWN
    rather than the nearest-looking category, because a wrong category is worse
    than no category: it sends the operator somewhere confidently wrong.
    """
    from_status = classify_status(status_code)
    if from_status is not None:
        return from_status

    if error is None:
        return UNKNOWN

    text = f"{type(error).__name__} {error}".lower()
    for category, signs in _SIGNATURES:
        if any(sign in text for sign in signs):
            return category
    return UNKNOWN


_RETRY_AFTER = re.compile(r"(?i)retry[- _]?after[\"'\s:=]+(\d+(?:\.\d+)?)")


def retry_after_seconds(text: str | None) -> float | None:
    """A retry-after hint, if the provider gave one. Never invented."""
    if not text:
        return None
    found = _RETRY_AFTER.search(text)
    return float(found.group(1)) if found else None


# Categories worth trying again for: the condition is expected to pass on its
# own. A quota problem is not here — retrying an exhausted balance is a retry
# storm that fixes nothing and costs the operator their rate limit.
RETRYABLE = frozenset({RATE_LIMIT, PROVIDER_UNAVAILABLE, NETWORK, TIMEOUT})
