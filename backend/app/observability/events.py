"""The names an audit event may have, and the rule about what goes in one.

A shared vocabulary matters more once there are two channels: a browser call
and a telephone call should produce the same event names for the same moments,
or an operator has to learn two dialects to read one board.

The rule that governs every event: **an event says what happened, never what
was said.** A PIN, a balance, an account number, a caller's number, a
credential and the model's reasoning are all absent by construction — the
payload carries identifiers, a category and a timestamp, and nothing that would
matter if it were read aloud.

The names are deliberately channel-neutral. A telephone call being received is
`CALL_RECEIVED` with `channel="PHONE"`, not `PHONE_CALL_RECEIVED`: the moment
is the same moment, and the channel is a field on it. Prefixing the channel
into the name would double the vocabulary, and — worse — it would let the two
channels drift apart, so that a control could quietly be enforced on one and
not the other without any name looking wrong. Filtering by `channel` gives an
operator the per-channel view; nothing is lost.

Nothing here writes anything. `app.observability.recorder` does the writing;
this is the vocabulary it uses.
"""

from enum import Enum


class AuditEvent(str, Enum):
    """Moments in a call's life that an operator may need to account for."""

    # The first three happen before any banking session exists, and on the
    # telephone they are three genuinely different moments an operator may need
    # to tell apart: an event arrived, it proved to be genuine and well-formed,
    # and only then was a call admitted.
    #
    # A provider told us about an inbound call. Nothing has been checked yet.
    CALL_RECEIVED = "CALL_RECEIVED"
    # The event passed signature, schema and size checks at the boundary. It is
    # a real notification from the provider; it still says nothing about who is
    # holding the telephone.
    CALL_VALIDATED = "CALL_VALIDATED"
    # Admission control had a slot and the call was taken.
    CALL_ACCEPTED = "CALL_ACCEPTED"
    # Admission control refused it. Carries a `reason` category, never a
    # message, and never reaches the caller in that form.
    CALL_REJECTED = "CALL_REJECTED"

    CALL_STARTED = "CALL_STARTED"

    AUTH_STARTED = "AUTH_STARTED"
    AUTH_SUCCEEDED = "AUTH_SUCCEEDED"
    AUTH_FAILED = "AUTH_FAILED"

    INTENT_RECEIVED = "INTENT_RECEIVED"
    TOOL_EXECUTED = "TOOL_EXECUTED"

    CALL_END_REQUESTED = "CALL_END_REQUESTED"
    CALL_ENDED = "CALL_ENDED"
    CALL_FAILED = "CALL_FAILED"


# Fields an event may carry. Anything not on this list does not go in an event,
# which is why it is an allow-list: a denylist of forbidden names would start
# leaking the day somebody added a field and forgot to update it.
ALLOWED_EVENT_FIELDS = frozenset(
    {
        "event",
        "agent_session_id",
        "provider_call_id",
        "provider_event_id",  # which notification, for spotting a retry
        "channel",
        "customer_id",     # only after a real PIN check, never a claim
        "tool_name",
        "intent",
        "domain",
        "reason",          # a category such as CAPACITY_REJECTED, never a message
        "error_category",  # a category, never an exception string
        "at",
    }
)

# Named so the prohibition is testable rather than merely documented.
FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "pin",
        "spoken_pin",
        "pin_hash",
        "password",
        "api_key",
        "openai_api_key",
        "authorization",
        "transcript",
        "utterance",
        "caller_number",
        "ani",
        "sip_from",
        "balance",
        "available_balance",
        "account_number",
        "reasoning",
        "chain_of_thought",
        "system_prompt",
    }
)


def safe_event(event: AuditEvent, **fields) -> dict:
    """Build one audit event, dropping anything not on the allow-list.

    Silently discarding an unexpected field is deliberate. The alternative —
    raising — would turn an observability mistake into a failed banking call,
    and the entire observability design says that must never happen.
    """
    payload = {"event": event.value}
    for name, value in fields.items():
        if name in ALLOWED_EVENT_FIELDS and value is not None:
            payload[name] = value
    return payload
