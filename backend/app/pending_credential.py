"""The credential question the bank owes a caller, and whether it has asked it.

The authentication counterpart of `app.pending_clarification`, and it exists for
exactly the reason that module does: *deciding* to ask a caller something and
the caller *hearing* the question are two different events, and until now only
the first was ever written down for a credential.

Phase 7.4E gave the telephone bridge a rule - while the bank is waiting for a
credential it has already asked for, nothing may ask for it again - and armed it
on "the bank spoke while a credential was awaited". That is not the same fact.
It cannot tell

    "Certainly, I can help you with that."       <- a harmless preface
    "May I have your demo customer ID, please?"  <- the actual question

apart, so a preface consumed the question and the *real* one was then withheld:
the caller was asked nothing at all and sat waiting. Over-suppression, which is
the worse direction of the two.

**Why no event-shape rule can fix that.** These two sequences are identical:

    preface  -> audio_end -> question          both must be heard
    question -> audio_end -> question again    exactly one must be heard

They differ only in what the utterances mean. Nothing at the media boundary can
separate them, and comparing the words is forbidden - it is the text-matching
this programme has refused at every phase, and it would break the moment the
model paraphrased itself. The only thing that can tell them apart is the bank
knowing which utterance it sent, which is what this module records.

**So the bank asks, through a response it owns.** Phase 7.4F: the backend issues
an explicit `response.create` for the question, carrying a correlation token in
its metadata, and learns the authoritative `response.id` back from
`response.created`. The wording still belongs to the agent -
`app.agents.speech.credential_question_instructions` says *what to ask*, not
what to say - but the identity of the question belongs to the bank, so nothing
downstream has to guess which utterance was the question. Audio on any other
response is not it, whatever it says, and nothing reads what it says.

What is stored carries no identity: a credential name and an attempt count. No
customer id, no PIN, nothing the caller said. It lives on the banking `Session`,
so it dies with the call and no other caller can reach it.

**The five states, and which of them each name means.** These were conflated
once already in this phase, at the cost of a caller being asked nothing at all,
so they are written out:

1. *owed* - a protected enquiry is held and the caller is not verified.
   `needed()`. Derived, so it cannot go stale.
2. *cue requested/sent* - the pump has submitted the cue to the model.
   `asked()` / `attempts_for()`, and Phase 7.4C's
   `pending_clarification.question_asked`. **Both of those names mean only
   this.** They are set when `send_message` returns, before the model has
   generated a word - which is deliberate: it is what makes a failed send retry
   on the next event. Neither says the caller heard anything.
3. *caller-visible delivery started* - audio for the question actually reached
   the line. `ConversationState.delivered_question_kind`, together with the
   response and item that carried it. Taken at the media boundary, and the only
   one of these five that is evidence about the caller.
4. *caller-visible delivery completed* - **not separately tracked.** The first
   admitted frame of the question's item takes ownership, and Phase 7.4D keeps
   the rest of that item flowing, so "started" and "completed" cannot diverge
   in a way any guard here would act on differently.
5. *awaiting the caller's answer* - `outstanding()`: owed, and cued for the
   credential that is owed. What the boundary suppresses repeats against.

State 2 without state 3 is the failure this module's `delivery_failed` exists
for: the bank believes it asked, the caller heard nothing, and without recovery
nobody would ever ask again. It is reached from
`ConversationState.question_delivery_failed`, called at the end of a model turn
from `PhoneCallBridge._generation_finished`.

One scheduling detail worth knowing, because it made these tests lie once:
`_generation_finished` is deferred through `PhoneCallBridge._schedule`, which
calls `coroutine.close()` and returns when no event loop is running. It drops
the work silently rather than raising - so a synchronous test that emits
`audio_end` never runs this recovery at all, and must drive the turn inside
`asyncio.run` to exercise it.
"""

from app.sessions import Session, SessionManager, SessionNotFoundError

CREDENTIAL_QUESTION_KEY = "credential_question_asked"

# Attempts already made at each credential question and revoked as undelivered.
#
# Kept apart from the record itself, because the record is *removed* when an
# attempt is revoked - that removal is what makes the question owed again - and a
# budget stored inside it would be thrown away with it. Without this the count
# would restart at one on every re-cue and `MAX_QUESTION_ATTEMPTS` would bound
# nothing at all.
_SPENT_KEY = "credential_question_attempts"

# The response the backend created for this question, and the token that proves
# it is ours.
#
# Phase 7.4F. Held in its own key rather than inside the cue record, because the
# record is removed when an attempt is revoked and the correlation must be
# removed with it - a stale token would let a response from an abandoned attempt
# claim ownership of the next one.
#
#     {"credential": "CUSTOMER_ID", "token": "<uuid4 hex>", "response_id": None}
#
# `response_id` is None until `response.created` arrives carrying our token, at
# which point it becomes the authoritative id the media boundary admits. Never
# assigned from event order: a token that does not match, or a session that does
# not match, is ignored.
#
# The token is a random hex string and carries nothing about the caller. It is
# safe to log, and it is the only thing sent as response metadata besides the
# question kind and the banking session id.
_OWNED_KEY = "credential_question_owned"

CUSTOMER_ID = "CUSTOMER_ID"
PIN = "PIN"

CREDENTIALS = frozenset({CUSTOMER_ID, PIN})

# How many times the bank may put one credential question to a caller who has
# not answered it.
#
# A bound is required in both directions. Without one, a cue that produces no
# caller-audible audio leaves the caller in silence for ever: the mark says the
# question was asked, `awaiting_question` therefore returns None, and only a
# completed caller turn clears it - which a caller who was never actually asked
# anything has no reason to produce. With an *unbounded* retry the opposite
# failure appears, and it is the one this whole phase exists to stop: a machine
# putting the same question on every model event, over the top of somebody
# trying to answer it.
#
# Two: the question, and one more attempt if the first never reached the line.
# Same reasoning as `lifecycle.MAX_SILENCE_DEFERRALS`, which is one deferral for
# the same class of problem - a question that did not get out must be recoverable
# without becoming a loop.
MAX_QUESTION_ATTEMPTS = 2


def _store(session: Session, manager: SessionManager | None, value) -> None:
    """Write the slot, through the manager when there is one."""
    context = dict(session.conversation_context)
    if value is None:
        context.pop(CREDENTIAL_QUESTION_KEY, None)
    else:
        context[CREDENTIAL_QUESTION_KEY] = value

    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return


def needed(session: Session | None) -> str | None:
    """Which credential this call must ask for, or None.

    Derived, never tracked, for the same reason `ConversationState.Stage` is:
    a hand-set value would go stale the moment `submit_customer_id` changed the
    session underneath it. There is exactly one answer and it is the one every
    guard reads.

    A credential is only *owed* when a protected enquiry is being held for this
    caller. Without one the bank has no reason to be asking for anything, and
    an unverified caller saying hello must be answered like anybody else - the
    first draft of the Phase 7.4E gate omitted this and silenced greetings and
    refusals on every unverified call.

    None once the caller is verified, and None when verification is locked: a
    locked call is not waiting for a credential, it is over.
    """
    if session is None:
        return None
    if session.authenticated or session.authentication_locked:
        return None

    from app import pending_request

    if pending_request.recall(session) is None:
        return None

    return PIN if session.candidate_customer_id else CUSTOMER_ID


def _record(session: Session | None) -> dict | None:
    """The stored record, or None. Tolerates the older bare-string shape.

    An earlier version of this module stored just the credential name. A session
    created under that shape can still be in memory during a deploy, and a call
    in progress must not fall over because its stored value is a string rather
    than a record.
    """
    if session is None:
        return None
    value = session.conversation_context.get(CREDENTIAL_QUESTION_KEY)
    if isinstance(value, str):
        return {"credential": value, "attempts": 1} if value in CREDENTIALS else None
    if not isinstance(value, dict):
        return None
    credential = value.get("credential")
    if credential not in CREDENTIALS:
        return None
    attempts = value.get("attempts")
    return {
        "credential": credential,
        "attempts": attempts if isinstance(attempts, int) and attempts > 0 else 1,
    }


def asked(session: Session | None) -> str | None:
    """Which credential has been *cued* to the model, or None.

    Named for what the bank did, not for what the caller experienced. This is
    set when the cue is submitted, which is before the model has generated a
    word - see the module docstring and `attempts_for`. Whether the caller
    actually heard anything is owned separately, by the media boundary.
    """
    record = _record(session)
    return None if record is None else record["credential"]


def attempts_for(session: Session | None, credential: str) -> int:
    """How many times this credential question has been cued to the model."""
    record = _record(session)
    if record is None or record["credential"] != credential:
        return 0
    return record["attempts"]


def delivery_failed(
    session: Session | None, *, manager: SessionManager | None = None
) -> str | None:
    """A turn ended without the caller hearing the question. Allow one more.

    The gap between "the cue was submitted" and "the caller heard it". If the
    model produced no caller-audible audio, or the response was cut off before
    any reached the line, then nothing was asked however much the record says it
    was - and because `awaiting_question` reads that record, nothing would ever
    ask again. Only a completed caller turn clears it, and a caller who was
    never asked anything has no reason to speak.

    So the mark is revoked, and the question becomes owed again - but only while
    the attempt budget lasts. Past `MAX_QUESTION_ATTEMPTS` the record stands, so
    a model that reliably fails to deliver cannot be driven round this loop on
    every event. The call then ends through the ordinary silence path rather
    than being re-prompted for ever.

    Returns the credential that may be asked again, or None if nothing changed.
    """
    record = _record(session)
    if record is None:
        return None

    if record["attempts"] >= MAX_QUESTION_ATTEMPTS:
        # Budget spent: the cue record stands, so nothing re-asks. But the
        # correlation must still go, and this early return used to skip it.
        #
        # The consequence was not theoretical. An exhausted attempt left its
        # adopted `response_id` in place, and the media boundary goes on
        # refusing every *other* response while an owned id is set - so a dead
        # response id would have muted the rest of the waiting period. Dropping
        # it here leaves the gate inert, which is the correct state for a
        # question nobody is going to ask again.
        forget_owned_question(session, manager=manager)
        return None

    credential = record["credential"]

    # Bank the attempt first, then revoke the mark. The order matters: removing
    # the record is what makes the question owed again, so a budget written
    # afterwards - or written into the record - would be discarded with it and
    # the retry would be unlimited.
    context = dict(session.conversation_context)
    spent = context.get(_SPENT_KEY)
    spent = dict(spent) if isinstance(spent, dict) else {}
    spent[credential] = record["attempts"]
    context[_SPENT_KEY] = spent
    context.pop(CREDENTIAL_QUESTION_KEY, None)
    # And the correlation for the attempt being abandoned. Its response may
    # still be in flight, and a `response.created` for it must not be able to
    # claim the next question - which is the "response belonging to an earlier
    # question attempt" case that ownership has to refuse.
    context.pop(_OWNED_KEY, None)

    if manager is None:
        session.conversation_context = context
    else:
        try:
            manager.update_session(
                session.session_id, conversation_context=context
            )
        except SessionNotFoundError:
            # The call ended mid-turn. Nothing left to ask, nobody to ask it.
            return None

    return credential


def awaiting_question(session: Session | None) -> str | None:
    """A credential the bank must ask for and has not yet asked for.

    None when nothing is owed, and None once it has been asked - the two cases
    in which there is no question to deliver. The pairing with `needed` is what
    makes the customer-id to PIN transition need no special case: once a
    candidate id arrives the owed credential becomes the PIN, the stale
    `CUSTOMER_ID` mark stops matching, and the PIN question is free to be put.
    """
    owed = needed(session)
    if owed is None:
        return None
    return None if asked(session) == owed else owed


def outstanding(session: Session | None) -> str | None:
    """A credential the caller has been asked for and not yet answered.

    What the media boundary reads. The bank has spoken the question; until a
    completed caller turn arrives, nothing may put it again.
    """
    owed = needed(session)
    if owed is None:
        return None
    return owed if asked(session) == owed else None


def mark_question_asked(
    session: Session | None,
    credential: str,
    *,
    manager: SessionManager | None = None,
) -> None:
    """Record that the question has been **cued to the model**.

    Not that the caller heard it. The two are different events and conflating
    them is what cost a caller their question earlier in this phase: a harmless
    preface was counted as the question and the real one was then withheld.

        this function / `asked()` / `attempts_for()`
            the cue was submitted. Set once `send_message` returns, before the
            model has generated a word.

        `ConversationState.delivered_question_kind`
            audio for the question actually reached the line. Taken at the media
            boundary, and the only state here that is evidence about the caller.

    Called after the *send* succeeds, which is the whole of the idempotency: the
    pump reaches this on every model event, so the stored value - not a counter
    at the call site - is what stops one decision becoming a stream of
    questions. Leaving it unset when a send raises is what lets the next event
    try again without any retry bookkeeping of its own.

    The count moves only when `delivery_failed` has revoked a previous cue, so
    it records attempts the caller did not hear rather than pump events.
    """
    if session is None or credential not in CREDENTIALS:
        return

    already = attempts_for(session, credential)
    if already:
        # Idempotent per delivery attempt, which is what stops one decision
        # becoming a stream of questions. The count only moves when
        # `delivery_failed` has revoked the previous mark, so it records
        # attempts the caller did not hear rather than pump events.
        return

    _store(
        session,
        manager,
        {"credential": credential, "attempts": _spent(session, credential) + 1},
    )


def _spent(session: Session | None, credential: str) -> int:
    """Attempts already made at this credential and revoked as undelivered."""
    if session is None:
        return 0
    spent = session.conversation_context.get(_SPENT_KEY)
    if not isinstance(spent, dict):
        return 0
    value = spent.get(credential)
    return value if isinstance(value, int) and value > 0 else 0


def issue_owned_question(
    session: Session | None,
    credential: str,
    *,
    manager: SessionManager | None = None,
) -> str | None:
    """Mint the correlation token for a backend-owned question response.

    Called immediately before the `response.create` is sent. Returns the token
    to put in the response metadata, or None if this credential is not one the
    bank asks for.

    A fresh token per attempt, deliberately. The previous attempt's response may
    still be in flight - it produced no audio, or was cut off - and its
    `response.created` must not be able to claim the new question.
    """
    if session is None or credential not in CREDENTIALS:
        return None

    import uuid

    token = uuid.uuid4().hex
    _write(
        session,
        manager,
        _OWNED_KEY,
        {"credential": credential, "token": token, "response_id": None},
    )
    return token


def owned_question(session: Session | None) -> dict | None:
    """The backend-owned question response for this call, or None.

    `{"credential", "token", "response_id"}`. `response_id` is None while the
    question has been requested but the server has not confirmed it yet.
    """
    if session is None:
        return None
    value = session.conversation_context.get(_OWNED_KEY)
    if not isinstance(value, dict):
        return None
    credential = value.get("credential")
    token = value.get("token")
    if credential not in CREDENTIALS or not isinstance(token, str) or not token:
        return None
    response_id = value.get("response_id")
    return {
        "credential": credential,
        "token": token,
        "response_id": response_id if isinstance(response_id, str) else None,
    }


def adopt_owned_response(
    session: Session | None,
    *,
    token: str | None,
    response_id: str | None,
    manager: SessionManager | None = None,
) -> str | None:
    """Bind `response.created`'s id to this question, if the token proves it ours.

    Refuses on every mismatch, and refuses silently rather than guessing:

    * no question was requested;
    * the token is absent, or belongs to an earlier attempt;
    * the response id is missing;
    * an id has already been adopted for this attempt.

    Ownership is never assigned from the order events arrive in. Two responses
    can be created in parallel - the SDK starts one of its own after every tool
    result - so "the next `response.created`" is not evidence of anything.

    Returns the adopted id, or None if nothing was adopted.
    """
    owned = owned_question(session)
    if owned is None or not token or not response_id:
        return None
    if owned["token"] != token:
        return None
    if owned["response_id"] is not None:
        return None

    _write(
        session,
        manager,
        _OWNED_KEY,
        {
            "credential": owned["credential"],
            "token": owned["token"],
            "response_id": response_id,
        },
    )
    return response_id


def forget_owned_question(
    session: Session | None, *, manager: SessionManager | None = None
) -> None:
    """Drop the correlation, because this attempt is over."""
    if session is None:
        return
    if _OWNED_KEY not in session.conversation_context:
        return
    _write(session, manager, _OWNED_KEY, None)


def _write(
    session: Session, manager: SessionManager | None, key: str, value
) -> None:
    """Write or remove one conversation-context key."""
    context = dict(session.conversation_context)
    if value is None:
        context.pop(key, None)
    else:
        context[key] = value

    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return


def clear(session: Session | None, *, manager: SessionManager | None = None) -> None:
    """Forget the question, because the caller has had their turn.

    A completed caller turn is the one event that authorises the bank to speak
    to them again - whether what they said was a usable credential or not. An
    unusable answer earns a re-ask; a tool result, a model continuation, a
    duplicate callback and a status probe earn nothing, and none of them reach
    this function.
    """
    if session is None:
        return

    context = dict(session.conversation_context)
    had_record = CREDENTIAL_QUESTION_KEY in context
    had_spent = _SPENT_KEY in context
    had_owned = _OWNED_KEY in context
    if not had_record and not had_spent and not had_owned:
        return

    context.pop(CREDENTIAL_QUESTION_KEY, None)
    # The owned response goes too. The caller has spoken, so whatever response
    # was created to ask them is finished business; keeping its id would let it
    # be admitted during the next waiting period.
    context.pop(_OWNED_KEY, None)
    # The undelivered-attempt budget goes with it. It is per *waiting period*,
    # not per call: it exists to stop a question that never reaches the line
    # being re-cued for ever, and a caller who has just spoken has ended that
    # wait whatever they said.
    #
    # Keeping it would make the count climb across turns, so a caller who gave
    # an unusable answer would earn their authorised re-ask with no retry budget
    # behind it - and the second unusable answer would have none at all. The
    # bound belongs to one silence, not to the whole conversation.
    context.pop(_SPENT_KEY, None)

    if manager is None:
        session.conversation_context = context
        return
    try:
        manager.update_session(session.session_id, conversation_context=context)
    except SessionNotFoundError:
        # The call ended mid-turn. There is nothing left to remember it for.
        return
