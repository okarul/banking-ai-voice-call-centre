"""Which live calls belong to *this* backend process.

Startup repairs the rows a dead process left behind. It has to: after a hard
crash nothing else will ever close them, and an operations board showing calls
nobody is on is worse than no board. But the query that did the repairing named
no owner —

    ended_at IS NULL AND status != REJECTED

— and an open row is not evidence of a dead owner. Phase 7.4A.1 measured what
that costs when a second application context starts while the first is still
carrying a call: the live call was stamped `FORCED_CLEANUP`, which means "left
behind by a crash", while the caller was still talking. Worse, `_end_call` read
`close_phone_call`'s return to decide whether it might release anything, so the
owning process then got `None` back, reported `ALREADY_ENDED`, and released
nothing — stranding a bridge, a capacity slot and a provider session.

**Ownership is answered from memory, not from the database, and that is
deliberate.** Two reasons, and the second is decisive:

*It is the more accurate question.* A column saying "process X claimed this"
records a label. `phone_call_registry` holds the actual bridge, and
`voice_call_manager` holds the actual capacity slot. What matters for repair is
whether resources are *held*, not what was once written down — and a row this
process created but whose bridge it has since lost genuinely is an orphan, which
the in-memory answer gets right and a stored label gets wrong.

*A stored column could not be deployed.* This repository has no migration
framework: `ensure_schema` calls `Base.metadata.create_all`, which creates
missing tables and **silently skips existing ones**. A new column on
`agent_sessions` would therefore never appear on any database that already
exists, and the code reading it would query a column that is not there. That is
the Phase 6.12 defect class exactly, and it is not worth reintroducing for a
label that answers the question less well.

**What `PROCESS_OWNER_ID` is for.** Diagnostics, and only diagnostics. It gives
an operator reading two log lines a way to tell which process wrote them, which
is precisely the question that is hard to answer during a restart. It confers no
authority and is never read from caller input.

=============================================================================
DEPLOYMENT CONSTRAINT — read this before scaling anything
=============================================================================

**Cross-process liveness is not established by `PROCESS_OWNER_ID`.** This module
can tell you what *this* process holds. It cannot tell you whether *another*
process is alive, because nothing here leases, heartbeats or locks. Proving that
needs one of those, and Phase 7.4A.1 deliberately did not build one.

So the supported production topology is, and remains:

* exactly **one** backend process;
* exactly **one** application worker (`uvicorn` with no `--workers`);
* **no overlapping** old and new backend processes during a restart or deploy.

Under those conditions startup repair is correct: anything open that this
process does not hold belongs to a generation that is gone.

Violate them — run two workers, or roll a deploy so the new process starts
before the old one has drained — and the new process will stamp the old one's
live calls `FORCED_CLEANUP`. Since Phase 7.4A.1 that is an **observability
inaccuracy rather than a resource leak**: `_end_call` no longer lets the database
decide whether this process may release what it is holding. But the row will
still say the wrong thing, and that is a known, accepted limitation rather than
a claim of rolling-restart safety.

This constraint is operational. Nothing in this repository can enforce it: a
second `uvicorn --workers 2` is a command line, not a code path, and there is no
authoritative way for one process here to detect the other. It is documented
here, in `docs/RUNBOOK.md`, and in `docs/TELEPHONY_MEDIA.md`, which already
recorded that the capacity ceiling is in-memory and single-process.
"""

import os
import socket
import threading
import uuid

# One per process lifetime, generated on first import. Host and pid make it
# legible to an operator reading logs; the random suffix keeps two processes
# that reuse a pid after a restart from looking like one.
#
# Overridable so a test can assert on a stable value. Deliberately read from the
# environment rather than accepting a parameter: nothing about this may ever be
# influenced by a caller, a provider event, or a model.
PROCESS_OWNER_ID = os.getenv("PROCESS_OWNER_ID") or (
    f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
)


# Banking sessions whose *terminal row* this process has not finished writing,
# mapped to the reason it intends to write. Guarded by a lock because the
# webhook race drives several application contexts from several threads in one
# interpreter, and this is read during startup reconciliation on one of them
# while another may be releasing a call.
#
# This is not a second call registry and must never become one. It holds no
# bridge, no session, no capacity and no customer - only "the row for this
# banking session is still ours to finish, and here is what it should say".
# Entries are created at the moment a terminal write is attempted and removed
# the moment one succeeds or the row is found already terminal.
_terminal_pending: dict[str, str] = {}
_terminal_lock = threading.Lock()


def mark_terminal_pending(banking_session_id: str | None, reason: str) -> None:
    """Claim the unfinished terminal row for this process.

    Called *before* the terminal write is attempted, which is the only ordering
    that works: the write may fail, and by the time it has, `tear_down` will
    have destroyed the session, the bridge and the slot - every other piece of
    evidence that this process was ever involved.

    Phase 7.4A.1 proved what that costs. A bounded lock failure on
    `close_phone_call` left an open row with nothing behind it, which is
    indistinguishable from what a crash leaves, so the next startup in the same
    process repaired it as a crash orphan and told an operator that a call
    which had run perfectly well was abandoned by a dead process.
    """
    if not banking_session_id:
        return
    with _terminal_lock:
        _terminal_pending[banking_session_id] = reason


def clear_terminal_pending(banking_session_id: str | None) -> None:
    """Release the claim: the row is terminal, by our hand or somebody's."""
    if not banking_session_id:
        return
    with _terminal_lock:
        _terminal_pending.pop(banking_session_id, None)


def pending_terminal_reason(banking_session_id: str | None) -> str | None:
    """What this process still intends to record, or None."""
    if not banking_session_id:
        return None
    with _terminal_lock:
        return _terminal_pending.get(banking_session_id)


def pending_terminal_sessions() -> set[str]:
    """Every row this process has not finished ending. For tests and logs."""
    with _terminal_lock:
        return set(_terminal_pending)


def forget_all_terminal_pending() -> int:
    """Drop every claim. For test teardown only; production never needs it."""
    with _terminal_lock:
        count = len(_terminal_pending)
        _terminal_pending.clear()
        return count


def locally_owned_banking_sessions() -> set[str]:
    """The banking sessions this process is currently holding resources for.

    Both channels, because both consume the one capacity ceiling: a telephone
    call holds a bridge in `phone_call_registry`, and either channel holds a
    model session in the single authoritative `voice_call_manager`.

    Returns a set rather than a query, so the caller can hand it to the
    reconciler without the observability layer needing to know that telephony
    exists.
    """
    from app.realtime.browser_calls import voice_call_manager
    from app.sessions import session_manager
    from app.telephony.bridge import phone_call_registry

    # The banking session first, and it is the one that matters. A row is
    # created by `claim_phone_call`, which `_register_incoming` reaches *after*
    # `create_session()` and *before* it has taken a capacity slot or registered
    # a bridge. Ownership measured only from those two therefore has a window -
    # a row exists that this process is plainly carrying and cannot yet prove -
    # and a concurrent startup lands in it. Phase 7.4A.1 watched that happen: 8
    # concurrent contexts, and one live call stamped `FORCED_CLEANUP` from
    # inside the gap.
    #
    # The banking session has no such gap. It exists before the row does and is
    # destroyed by the same teardown that releases everything else, so it brackets
    # every other resource this process could hold.
    owned = {session.session_id for session in session_manager.list_active_sessions()}

    # The other two as well. They are redundant while the session store is
    # authoritative, and cheap; keeping them means ownership does not become
    # wrong if a future path ever holds a bridge without a session.
    owned.update(
        bridge.banking_session_id
        for bridge in phone_call_registry.all_bridges()
        if bridge.banking_session_id
    )
    owned.update(voice_call_manager.active_session_ids())

    # And the rows whose ending this process has not managed to write yet. The
    # call itself is long gone - released, correctly, because a database that
    # will not answer must never keep a caller's resources alive - but the row
    # is still ours to finish and is not an orphan while we mean to finish it.
    owned.update(pending_terminal_sessions())
    return owned


def reconcile_on_startup() -> int:
    """Repair the rows a previous generation left open, and only those.

    The one thing lifespan startup does about stale state, and the one entry
    point tests should drive when they mean "another process started". Keeping
    it here rather than in the lifespan means a test can perform a startup
    without building an application.

    Returns how many rows were repaired.
    """
    from app.observability import recorder

    return recorder.reconcile_active_sessions(
        owned_banking_sessions=locally_owned_banking_sessions()
    )
