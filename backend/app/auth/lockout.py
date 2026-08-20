"""Counting failed PIN attempts across calls, not just within one.

`app.auth.authentication` locks a *session* after three wrong PINs. That is a
good control against a caller fumbling in one conversation and a poor one
against anybody deliberate, because hanging up costs nothing: a new call is a
new session with a fresh count, and a four-digit PIN is 10 000 possibilities.
The browser channel makes that tedious. A telephone line makes it a script.

So the count also lives in the database, keyed by the customer id the caller
*claimed*, and it outlives the call that produced it.

Three decisions worth stating, because each looks like a bug otherwise:

**Claimed, not verified.** Failures are recorded against an id that matches no
customer exactly as they are against one that does. Skipping unknown ids would
mean only real customers ever lock, and an attacker could separate real ids
from invented ones by watching which claims eventually lock — reopening the
enumeration channel that the single generic failure message exists to close.

**Locks expire.** `locked_until` is a timestamp and the window is configurable.
An unbounded lock on shared demonstration customers would let one mistyped PIN
deny DEMO001 to an entire classroom: a denial of service dressed as a security
control. Production would want a longer window, an unlock path, and an alert —
all organisational decisions, not code ones.

**The window is also the memory.** Failures older than `PIN_LOCKOUT_MINUTES`
are not counted, so an honest customer who mistypes once a week never
accumulates their way into a lock.

Every state change is a single atomic statement. Two concurrent wrong guesses
must count as two, and a read-modify-write in Python would let them count as
one — which is exactly the race an attacker running parallel calls would rely
on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database.connection import session_scope
from app.database.models import CustomerAuthLock


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LockState:
    """Where a claimed customer id currently stands."""

    customer_id: str
    failed_attempts: int
    locked_until: datetime | None

    @property
    def locked(self) -> bool:
        return self.locked_until is not None


def _window() -> timedelta:
    return timedelta(minutes=settings.pin_lockout_minutes)


def _threshold() -> int:
    """Failures tolerated across calls before a lock.

    A zero here would mean "lock on the first failure", which is a
    misconfiguration that would take the demo bank offline; it is read as
    "disabled" instead, matching how the capacity ceiling treats zero.
    """
    return settings.pin_lockout_max_attempts


def is_locked(customer_id: str, *, now: datetime | None = None) -> datetime | None:
    """The instant this id unlocks, or None if it is not locked.

    An expired lock reads as unlocked. Rows are not cleaned up eagerly: the
    expiry comparison is the authority, and a lock that has run out is simply
    one whose `locked_until` is in the past.
    """
    if not customer_id or _threshold() == 0:
        return None

    moment = now or _now()
    with session_scope() as db:
        row = db.scalars(
            select(CustomerAuthLock).where(
                CustomerAuthLock.customer_id == customer_id
            )
        ).first()
        if row is None or row.locked_until is None:
            return None
        locked_until = row.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        return locked_until if locked_until > moment else None


def record_failure(customer_id: str, *, now: datetime | None = None) -> LockState:
    """Count one failed PIN attempt against a claimed id, atomically.

    The whole decision — is this failure part of the current run or the start
    of a new one, does it reach the threshold, when does the lock lift — is
    expressed as one `INSERT ... ON CONFLICT DO UPDATE`. Reading the row into
    Python and writing it back would let two simultaneous guesses both read
    "two failures so far" and both write three, so the fifth guess never
    arrives and the lock never closes.
    """
    if not customer_id or _threshold() == 0:
        return LockState(customer_id, 0, None)

    moment = now or _now()
    window_start = moment - _window()
    threshold = _threshold()

    table = CustomerAuthLock.__table__

    # Whether the stored run has gone cold. A failure older than the window is
    # not held against the caller, so the count starts again at one.
    stale = case(
        (table.c.last_failed_at < window_start, True),
        (table.c.last_failed_at.is_(None), True),
        else_=False,
    )
    next_attempts = case((stale, 1), else_=table.c.failed_attempts + 1)

    statement = (
        pg_insert(table)
        .values(
            customer_id=customer_id,
            failed_attempts=1,
            first_failed_at=moment,
            last_failed_at=moment,
            locked_until=(moment + _window()) if threshold <= 1 else None,
            updated_at=moment,
        )
        .on_conflict_do_update(
            index_elements=[table.c.customer_id],
            set_={
                "failed_attempts": next_attempts,
                "first_failed_at": case(
                    (stale, moment), else_=table.c.first_failed_at
                ),
                "last_failed_at": moment,
                # Reaching the threshold sets the expiry. Below it, any expired
                # lock is cleared rather than left to look current.
                "locked_until": case(
                    (next_attempts >= threshold, moment + _window()), else_=None
                ),
                "updated_at": moment,
            },
        )
        .returning(table.c.failed_attempts, table.c.locked_until)
    )

    with session_scope() as db:
        attempts, locked_until = db.execute(statement).one()

    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)

    return LockState(customer_id, attempts, locked_until)


def clear(customer_id: str) -> None:
    """Forget the failures for an id, on a successful verification.

    A caller who proves who they are has answered the question the counter was
    asking. Leaving the count standing would mean a customer who mistyped twice
    this morning is two guesses from a lock all day.
    """
    if not customer_id:
        return
    with session_scope() as db:
        db.execute(
            delete(CustomerAuthLock).where(
                CustomerAuthLock.customer_id == customer_id
            )
        )


def clear_all() -> int:
    """Remove every lock. For test isolation and operator recovery only."""
    with session_scope() as db:
        result = db.execute(delete(CustomerAuthLock))
        return result.rowcount or 0
