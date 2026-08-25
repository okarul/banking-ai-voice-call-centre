"""Why a telephone call ended, in one place and one vocabulary.

Three systems have to tell the same story about a call: the application log,
the database row, and whatever the gateway reports to an operator. When they
use different words for the same event — or worse, the same word for different
events — the story stops being reconstructable, and an operator debugging a
production call is reading three accounts that disagree.

That has already cost this project once. A provider refusing every session was
reported to the gateway as `rejected_capacity`, so the obvious reading was a
full switchboard while the concurrency limit was 1 and nothing was in progress.

**Backward compatibility.** These are values already written to
`agent_sessions.disconnect_reason`, so they are not free to rename. Where an
existing value is merely awkward it is kept and documented; where it was
actively misleading — one name covering two unrelated events — it is split,
and the older name is noted here so historical rows remain readable.
"""

from __future__ import annotations

# --- endings the caller chose ----------------------------------------------

#: The caller said an explicit ending and heard the bank's goodbye in full.
CALLER_GOODBYE = "CALLER_GOODBYE"

#: The caller said nothing for the silence timeout after the bank had finished
#: speaking, was told so, and heard that line in full.
CALLER_SILENT = "CALLER_SILENT"

#: The caller hung up. Recorded by whichever path noticed first — usually the
#: media socket closing under a route that was waiting to read from it.
#:
#: Stored as `CUSTOMER_ENDED` rather than `CALLER_HANGUP` because that is the
#: value already in the database and in operator dashboards. It is written only
#: when nothing else has closed the call: `close_phone_call` moves the row with
#: `WHERE ended_at IS NULL`, so a goodbye or a silence close that already
#: happened keeps its own reason and this never overwrites it.
CALLER_HANGUP = "CUSTOMER_ENDED"

#: The provider told us the call ended — a carrier-side disconnect rather than
#: anything either the caller or this application did.
PROVIDER_HANGUP = "PROVIDER_ENDED"


# --- endings the application chose ------------------------------------------

#: A model session that would not open. The call never carried conversation.
REALTIME_START_FAILURE = "REALTIME_START_FAILURE"

#: A model session that opened and then failed underneath a live call.
#:
#: Split out of the older `PROVIDER_FAILURE`, which covered this and media
#: failure alike and so could not tell an operator which supplier to look at.
REALTIME_RUNTIME_FAILURE = "REALTIME_RUNTIME_FAILURE"

#: The audio path failed: the gateway went away, or a pump could not continue.
MEDIA_FAILURE = "MEDIA_FAILURE"

#: The gateway never attached its audio socket in time.
MEDIA_ATTACH_TIMEOUT = "MEDIA_ATTACH_TIMEOUT"

#: The gateway attached but could not agree a media protocol — a mismatched
#: release, or a gateway too old to acknowledge playback.
PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"

#: No audio in either direction for the idle timeout, so the call is assumed
#: abandoned and its capacity slot reclaimed.
#:
#: Formerly `SILENCE_TIMEOUT`, which was the same word this system uses for a
#: caller who has stopped talking. They are not the same thing and do not lead
#: anywhere near the same investigation: one is a quiet customer, the other is
#: a call nobody is on. Historical rows may still carry the old value.
IDLE_TIMEOUT = "IDLE_TIMEOUT"

#: Something in this application failed while handling the call.
APPLICATION_ERROR = "APPLICATION_ERROR"

#: The application ended the call deliberately for a reason with no more
#: specific name.
APPLICATION_END = "APPLICATION_END"

#: This call used its three PIN attempts. The customer id is **not** locked;
#: the caller may ring back and try again immediately. Kept apart from
#: `AUTH_LOCKOUT` because recording it as a lockout would report an ordinary
#: forgotten PIN as a security event, and would make an honest redial look like
#: an attacker returning.
AUTH_ATTEMPTS_EXHAUSTED = "AUTH_ATTEMPTS_EXHAUSTED"

#: Failures against this id reached the cross-call threshold inside the
#: lockout window. Ringing back does not help until it expires. This is the
#: one an operator should be able to count.
AUTH_LOCKOUT = "AUTH_LOCKOUT"


# --- refusals at admission --------------------------------------------------
#
# A call refused before it ever carried conversation. These are written by the
# admission path rather than by an ending, but they land in the same
# `disconnect_reason` column and are read off the same dashboard, so leaving
# them out of this module made `ALL` a promise it did not keep.

#: Every slot was in use. The one refusal that genuinely means "try later".
CAPACITY_REJECTED = "CAPACITY_REJECTED"

#: The model session did not open within the connect budget. Kept apart from
#: `REALTIME_START_FAILURE` because a timeout and a refusal point at different
#: things: one is a slow or unreachable provider, the other is one that
#: answered and said no.
REALTIME_TIMEOUT = "REALTIME_TIMEOUT"

#: The audio path could not be built at admission, before the call was ever
#: greeted. Distinct from `MEDIA_FAILURE`, which is a path that worked and
#: then broke underneath a live call.
MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"


# --- mapping ----------------------------------------------------------------

# What the lifecycle calls an ending, and what gets recorded for it. The
# lifecycle speaks about the conversation; the database speaks about the call.
FROM_END_REASON = {
    "CALLER_GOODBYE": CALLER_GOODBYE,
    "CALLER_SILENT": CALLER_SILENT,
    "CALLER_DISCONNECTED": CALLER_HANGUP,
    "SYSTEM_ERROR": APPLICATION_ERROR,
    "AUTH_ATTEMPTS_EXHAUSTED": AUTH_ATTEMPTS_EXHAUSTED,
    "AUTHENTICATION_LOCKED": AUTH_LOCKOUT,
}

# Every reason this application may record, for tests and for anyone reading
# dashboard values back.
#
# This tuple is only useful if it is exhaustive. An operator builds a dashboard
# filter by enumerating it, so a value written anywhere in this application but
# missing here does not show up as an unknown category — it silently does not
# show up at all. `test_every_persisted_reason_is_in_the_vocabulary` holds the
# admission path to that.
ALL = (
    CALLER_GOODBYE,
    CALLER_SILENT,
    CALLER_HANGUP,
    PROVIDER_HANGUP,
    REALTIME_START_FAILURE,
    REALTIME_RUNTIME_FAILURE,
    MEDIA_FAILURE,
    MEDIA_ATTACH_TIMEOUT,
    PROTOCOL_MISMATCH,
    IDLE_TIMEOUT,
    APPLICATION_ERROR,
    APPLICATION_END,
    AUTH_ATTEMPTS_EXHAUSTED,
    AUTH_LOCKOUT,
    CAPACITY_REJECTED,
    REALTIME_TIMEOUT,
    MEDIA_UNAVAILABLE,
)


# --- values written by earlier releases -------------------------------------
#
# Renaming a reason does not rewrite the rows already carrying the old name.
# These are recorded here rather than dropped, so a query over historical data
# can resolve them instead of treating them as corrupt.

#: Old name -> the reason it would be written as today. Unambiguous cases only.
HISTORICAL = {
    # Phase 6.8 split this: a caller who has stopped talking is `CALLER_SILENT`,
    # a call nobody is on is `IDLE_TIMEOUT`. Rows written before the split are
    # the second of those, because that is the only thing the sweep recorded.
    "SILENCE_TIMEOUT": IDLE_TIMEOUT,
}

#: Old names that cannot be resolved, because one value covered two events that
#: this application now deliberately tells apart. A row carrying one of these
#: means "the model session or the audio path failed" and nothing narrower;
#: mapping it to either would invent a precision the row never had.
AMBIGUOUS_HISTORICAL = ("PROVIDER_FAILURE",)


def for_end_reason(end_reason: str) -> str:
    """The recorded reason for a lifecycle ending, or a safe default."""
    return FROM_END_REASON.get(str(end_reason), APPLICATION_END)
