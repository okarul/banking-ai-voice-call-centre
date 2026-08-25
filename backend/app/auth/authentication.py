"""Deterministic customer authentication.

Every accept/reject decision is made here in plain Python against PostgreSQL.
No language model is involved, and none ever should be: a later voice layer
will only collect spoken text and hand the normalised value to these functions.

Layering: route -> normalization -> this service -> SessionManager + repository.
"""

import secrets

from app.auth import lockout
from app.auth.normalization import normalize_customer_id, normalize_pin
from app.database.connection import session_scope
from app.database.repositories import get_customer_by_customer_id
from app.security import hash_pin
from app.security import verify_pin as verify_pin_hash
from app.sessions import SessionManager
from app.sessions import session_manager as default_manager

MAX_AUTHENTICATION_ATTEMPTS = 3

ACTIVE_CUSTOMER_STATUS = "active"

# The single failure returned for every unsuccessful credential check.
#
# An unknown customer id, a known customer with the wrong PIN, and a customer
# whose record is not active all produce this and nothing else. Distinguishing
# them would let anyone with a phone line discover who banks here simply by
# reading back the difference: "that id was accepted, try a PIN" versus "no
# such customer" is a customer list, one guess at a time.
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"

# Returned when a verified session is asked to identify or verify again.
#
# Identity is settled once per call. The only thing that can ask for it to
# change mid-call is spoken text, and spoken text is not authority: a caller
# saying "I'm DEMO002 now", or an enquiry that merely mentions another customer,
# must not move the session. Without this the session's customer id — which
# every authorization guard trusts — would be writable by anything the caller
# or the model said. Switching customer requires a new call.
ALREADY_AUTHENTICATED = "ALREADY_AUTHENTICATED"

# A PIN-shaped value that matches nobody, hashed at import.
#
# When the claimed customer does not exist there is no stored hash to check,
# and simply returning would make an unknown id answer far faster than a wrong
# PIN — the timing alone would say whether a customer is real. Verifying
# against this decoy does the same key-derivation work, so both paths cost
# about the same. It is derived from random bytes, so no PIN matches it.
_DECOY_HASH = hash_pin(secrets.token_hex(16))


def _failure(reason: str, **extra) -> dict:
    return {"success": False, "reason": reason, **extra}


# Why authentication stopped accepting attempts. The `reason` stays
# `AUTHENTICATION_LOCKED` for both, because the authorization guard, the
# dashboard and twenty-two existing tests read that string and it is not free
# to rename.
#
# The two states must, however, be **communicated differently**. They are
# materially different facts for the caller: one may ring back and try again,
# the other may not until the window expires. Telling a customer their PIN is
# locked when it is not is a false statement about their account, and telling
# them to ring back when they are locked wastes their time. The security
# requirement is to avoid leaking internals — attempt counters, thresholds,
# which half of the credential pair was wrong — not to pretend two different
# outcomes are the same one.
#
#   SESSION    this call has used its three attempts. Nothing is locked
#              anywhere else; the caller may ring back and try again.
#   PERSISTENT five failures against this id inside the lockout window.
#              Ringing back will not help until the window expires.
#
# Saying "your PIN is locked" to a caller who has merely used up one call's
# attempts is a false statement about the state of their account, and it is the
# kind of thing that sends a customer to a branch for no reason.
LOCK_SCOPE_SESSION = "SESSION"
LOCK_SCOPE_PERSISTENT = "PERSISTENT"

# Where the scope is left for the rest of the call to read. On the session, so
# the telephone lifecycle can decide how to close without a database round trip
# on the audio event loop — the mistake Phase 6.9.1 removed and must not
# reintroduce here.
LOCK_SCOPE_KEY = "auth_lock_scope"


def _remember_lock_scope(session_id, scope, manager) -> None:
    """Record which limit stopped this caller, for the lifecycle and the agent."""
    session = manager.get_session(session_id)
    if session is not None:
        session.conversation_context[LOCK_SCOPE_KEY] = scope


def lock_scope(session) -> str | None:
    """Which limit stopped this session, or None if it was never stopped."""
    if session is None:
        return None
    return session.conversation_context.get(LOCK_SCOPE_KEY)


def verify_customer(
    session_id: str,
    customer_id: str,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Record the customer id the caller is claiming, and ask for the PIN.

    Deliberately does not look the id up. Whether it exists is decided at the
    PIN step, so this step behaves identically for a real customer, a customer
    who does not exist, and one whose record is closed. Anything else would
    turn the identification prompt into a customer directory: try an id, and
    the difference between "now your PIN" and "no such customer" answers the
    question for you.

    This never authenticates. It only establishes who the caller claims to be,
    and only while nobody has been verified yet — a caller who misspeaks their
    id can correct it, but a verified session cannot be re-pointed at anyone.
    """
    session = manager.get_session(session_id)
    if session is None:
        return _failure("SESSION_NOT_FOUND")

    if session.authentication_locked:
        return _failure("AUTHENTICATION_LOCKED")

    if session.authenticated:
        return _failure(ALREADY_AUTHENTICATED)

    if not customer_id:
        return _failure("INVALID_CUSTOMER_ID")

    # The claim is held apart from `customer_id`, which stays empty until a PIN
    # proves it. The attempt count is deliberately not reset: it belongs to the
    # call, not to the identification, or a caller could clear it by naming an
    # id again between guesses and the lockout would never bite.
    manager.update_session(
        session_id,
        candidate_customer_id=customer_id,
        authenticated=False,
    )

    return {"success": True, "customer_id": customer_id, "next_step": "PIN"}


def verify_pin(
    session_id: str,
    pin: str,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Check the claimed customer id and PIN together, as one credential.

    This is where existence is finally decided, and it is decided silently. An
    id that matches nobody, a real customer with the wrong PIN, and a customer
    whose record is closed all return the same failure, cost the same attempt
    and take about the same time. The caller learns that the pair was wrong and
    nothing else.

    On success the claimed id is promoted to the session's verified customer.
    Only here does `session.customer_id` ever get set.
    """
    session = manager.get_session(session_id)
    if session is None:
        return _failure("SESSION_NOT_FOUND", authenticated=False)

    if session.authentication_locked:
        # Already stopped. Which limit did it is still worth reporting, so a
        # repeat attempt on a locked session is described the same way as the
        # attempt that locked it rather than becoming vaguer on the second ask.
        candidate = session.candidate_customer_id
        persistent = candidate is not None and lockout.is_locked(candidate) is not None
        scope = LOCK_SCOPE_PERSISTENT if persistent else LOCK_SCOPE_SESSION
        _remember_lock_scope(session_id, scope, manager)
        return _failure(
            "AUTHENTICATION_LOCKED", authenticated=False, lock_scope=scope
        )

    # Already verified: the PIN step is over. Re-running it could only lower
    # this session's standing — a wrong value would raise the attempt count and
    # could lock out the very caller who has already passed.
    if session.authenticated:
        return _failure(ALREADY_AUTHENTICATED, authenticated=True)

    candidate = session.candidate_customer_id
    if candidate is None:
        return _failure("CUSTOMER_NOT_IDENTIFIED", authenticated=False)

    if not pin:
        return _failure("INVALID_PIN_FORMAT", authenticated=False)

    # The count that survives hanging up. Checked before the PIN is compared,
    # so a locked id costs an attacker a refusal rather than a guess — and
    # checked against the *claimed* id, which is the only thing known at this
    # point and the only thing an attacker can iterate.
    if lockout.is_locked(candidate) is not None:
        # The session is told, not just the caller. This return used to leave
        # `authentication_locked` False on a session whose caller was genuinely
        # locked out, so three things then disagreed with the tool result: the
        # telephone's own view of the call, the `auth_status` written to the
        # dashboard (FAILED rather than LOCKED), and the invariant that the
        # assistant may only claim a lock the backend holds. The refusal was
        # always correct; the record of it was not.
        manager.update_session(
            session_id,
            authenticated=False,
            authentication_locked=True,
        )
        _remember_lock_scope(session_id, LOCK_SCOPE_PERSISTENT, manager)
        return _failure(
            "AUTHENTICATION_LOCKED",
            authenticated=False,
            lock_scope=LOCK_SCOPE_PERSISTENT,
        )

    with session_scope() as db:
        customer = get_customer_by_customer_id(db, candidate)
        # Read the hash into a local only long enough to compare it. It is
        # never returned, logged or attached to the session.
        if customer is not None and customer.status == ACTIVE_CUSTOMER_STATUS:
            stored_hash = customer.pin_hash
            known = True
        else:
            # No record to check against. The decoy is verified anyway so that
            # an unknown id does not answer noticeably faster than a wrong PIN.
            stored_hash = _DECOY_HASH
            known = False

    if verify_pin_hash(pin, stored_hash) and known:
        # Proving identity answers the question the counter was asking.
        lockout.clear(candidate)
        manager.update_session(
            session_id,
            customer_id=candidate,
            authenticated=True,
            authentication_attempts=0,
        )
        return {
            "success": True,
            "authenticated": True,
            "customer_id": candidate,
        }

    # Recorded against the claimed id before anything else, so a caller who
    # hangs up mid-guess still pays for the attempt they just made.
    persistent = lockout.record_failure(candidate)

    attempts = session.authentication_attempts + 1
    # Either limit can end this call's attempts: three wrong PINs inside one
    # call, or enough failures across calls to trip the persistent lock. The
    # caller is told the same thing by both, because the difference between
    # "you have used this call's attempts" and "this id is locked everywhere"
    # is precisely the information an attacker is probing for.
    locked = attempts >= MAX_AUTHENTICATION_ATTEMPTS or persistent.locked

    manager.update_session(
        session_id,
        authenticated=False,
        authentication_attempts=attempts,
        authentication_locked=locked,
    )

    if locked:
        # Which of the two limits stopped them decides what the caller is told.
        # Both end this call's attempts; only one is a lock on the id itself,
        # and claiming the wrong one misinforms the customer about their own
        # account. `persistent` wins when both are true, because it is the
        # stronger and longer-lasting fact.
        scope = (
            LOCK_SCOPE_PERSISTENT if persistent.locked else LOCK_SCOPE_SESSION
        )
        _remember_lock_scope(session_id, scope, manager)
        return _failure(
            "AUTHENTICATION_LOCKED",
            authenticated=False,
            attempts_remaining=0,
            lock_scope=scope,
        )

    return _failure(
        INVALID_CREDENTIALS,
        authenticated=False,
        attempts_remaining=MAX_AUTHENTICATION_ATTEMPTS - attempts,
    )


# --- entry points that accept raw (spoken) text -----------------------------


def _is_verified(session_id: str, manager: SessionManager) -> bool:
    """Whether this session has already passed authentication."""
    session = manager.get_session(session_id)
    return bool(session and session.authenticated)


def submit_customer_id(
    session_id: str,
    spoken_customer_id: str,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Normalise spoken customer-id text, then verify it.

    On a session that is already verified this refuses before normalising, so
    nothing about the spoken value is examined or reflected back. Every input
    then gets the identical answer: a well-formed id, an unknown id and a word
    that is not an id are indistinguishable, and none of them is looked up.
    """
    if _is_verified(session_id, manager):
        return _failure(ALREADY_AUTHENTICATED)

    normalized = normalize_customer_id(spoken_customer_id)
    if normalized is None:
        return _failure("INVALID_CUSTOMER_ID_FORMAT")
    return verify_customer(session_id, normalized, manager=manager)


def submit_pin(
    session_id: str,
    spoken_pin: str,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Normalise spoken PIN text, then verify it.

    A value that is not four digits is rejected before any database work and
    does not count as a failed attempt. On an already-verified session the PIN
    step is over, so the spoken value is not normalised, checked or stored —
    it is simply not looked at.
    """
    if _is_verified(session_id, manager):
        return _failure(ALREADY_AUTHENTICATED, authenticated=True)

    normalized = normalize_pin(spoken_pin)
    if normalized is None:
        return _failure("INVALID_PIN_FORMAT", authenticated=False)
    return verify_pin(session_id, normalized, manager=manager)


def authentication_status(
    session_id: str,
    *,
    manager: SessionManager = default_manager,
) -> dict | None:
    """Safe authentication view of a session, or None if it does not exist."""
    session = manager.get_session(session_id)
    if session is None:
        return None

    return {
        "session_id": session.session_id,
        "customer_id": session.customer_id,
        "authenticated": session.authenticated,
        "authentication_attempts": session.authentication_attempts,
        "authentication_locked": session.authentication_locked,
    }
