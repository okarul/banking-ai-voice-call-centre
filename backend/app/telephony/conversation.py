"""Everything one telephone call knows about itself, with names and types.

The conversation used to be spread across three untyped places: a `dict` on the
banking session, loose attributes on the bridge, and whatever the lifecycle
happened to be holding. Reading the state of a call meant knowing all three and
which one won. This is the one place to look.

**It wraps rather than copies.** Authentication, the pending enquiry and the
account or loan under discussion already live on the banking session, which is
what the authorization guards and the banking tools read. Copying them here
would create a second answer to "who is this caller", and the moment two
answers disagree the wrong one gets used to release money. So those are
properties that read the session; only the fields the telephone channel itself
owns — turn counting, response identity, playback and closing state — are
stored here.

**It is per call.** One instance per bridge, no module-level registry, nothing
keyed by customer. Two callers are two objects, and neither has a reference to
the other's.

**It never holds a PIN.** Not the spoken digits, not a hash, not a masked form.
The PIN is checked deterministically by `app.auth` and is not part of a
conversation's state; `describe()` is asserted to expose nothing sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    """How far along the call is. Coarse on purpose — this is for operators."""

    OPENING = "OPENING"
    IDENTIFYING = "IDENTIFYING"
    VERIFYING = "VERIFYING"
    SERVING = "SERVING"
    CLOSING = "CLOSING"
    ENDED = "ENDED"


@dataclass
class ConversationState:
    """The state of one telephone call.

    Stored fields are the telephone channel's own. Everything about the
    customer is a property that reads the banking session, so there is exactly
    one answer to who is on the call and it is the one the guards use.
    """

    provider_call_id: str
    banking_session_id: str
    session_manager: object = field(repr=False, default=None)

    # --- what this channel owns ---------------------------------------------

    stage: Stage = Stage.OPENING

    # One logical caller turn. Incremented when the caller starts speaking, and
    # the key that scopes duplicate suppression: the same question asked again
    # later is a new turn and must be answered again.
    turn_counter: int = 0
    last_completed_turn_id: int = 0

    # The model's identifier for the response currently being spoken. `None`
    # between responses. A second, different id arriving while one is active is
    # two assistants talking at once — see `app.telephony.bridge`.
    active_response_id: str | None = None

    # The model's identifier for the *output item* currently being spoken.
    #
    # A response is not the unit a caller hears; an item is. One response may
    # carry several output items, and every one of them shares the response's
    # id - so a guard that knows only the response admits all of them and the
    # caller is answered twice. Live call `56f012b6-2b4e-1240-4790-eaa5afddeeef`
    # was asked for its demo customer ID twice, six milliseconds apart, from one
    # response, the second arriving over the caller's answer. They hung up.
    #
    # Held beside the response id and cleared at the same boundaries, because
    # the two together are one owner: the first identifiable item of the active
    # response owns caller-visible playback until the turn genuinely ends.
    active_item_id: str | None = None

    # The question the caller has actually been asked, and what carried it.
    #
    # Phase 7.4E, second pass. The first pass marked "a credential prompt has
    # been delivered" whenever audio reached the line while a credential was
    # awaited, which is not the same fact and cannot be made into it: a
    # harmless preface was counted as the question and the real one was then
    # withheld, so the caller was asked nothing at all.
    #
    # These two sequences are identical at the media boundary -
    #
    #     preface  -> audio_end -> question          both must be heard
    #     question -> audio_end -> question again    exactly one must be heard
    #
    # - and they differ only in what the utterances mean, so no rule here can
    # separate them and comparing the words is not something this system does.
    # What can separate them is the bank knowing which utterance it asked for,
    # which `app.pending_credential` and `app.pending_clarification` record.
    #
    # So the kind is set only while the backend says a question is genuinely
    # outstanding, and the response and item are the ones that delivered it.
    # Cleared by a completed caller turn - the one event that authorises the
    # bank to speak to them again. A tool result is not such an event, and
    # neither is a model continuation, a duplicate callback or a status probe.
    delivered_question_kind: str | None = None
    delivered_question_response: str | None = None
    delivered_question_item: str | None = None

    # The response the *bank* created to ask a state-machine question, once the
    # server has confirmed its id.
    #
    # Phase 7.4F, and the fact that ends the preface-versus-duplicate
    # ambiguity. Until now the boundary had to infer which audio was the
    # question from "the first audio that arrived while one was outstanding",
    # and two shapes were indistinguishable under that rule:
    #
    #     one response, items [preface,  question]   item 2 must be heard
    #     one response, items [question, question]   item 2 must be dropped
    #
    # An owned response needs no inference: the backend created it, the server
    # echoed the backend's own token in `response.created`, and this is the id
    # that came back. Audio on any other response is not the question, whatever
    # it says - and nothing here reads what it says.
    #
    # Mirrored from `app.pending_credential` / `app.pending_clarification` by
    # `PhoneCallBridge._adopt_owned_question_response`, so the media path can
    # check identity without a session read on every frame.
    owned_question_kind: str | None = None
    owned_question_response: str | None = None

    # Times an owned question response carried more than one assistant item,
    # which the installed API contract says cannot happen while `tool_choice`
    # is "none" and `tools` is empty. Counted rather than accommodated: the
    # extra item is refused by Phase 7.4D exactly as any second item is, and a
    # non-zero value here means the provider's contract changed under us.
    owned_question_contract_violations: int = 0

    # How many caller-facing responses were withheld because the bank had
    # already asked and was still waiting. Counted apart from
    # `duplicate_responses_suppressed` so an operator can tell "the model said
    # the same thing twice" from "the model tried to re-ask a question the
    # caller was already answering".
    credential_prompts_suppressed: int = 0

    # Responses already refused on this turn. Without this, a suppressed
    # response resumes the moment the admitted one ends: its remaining audio
    # finds `active_response_id` back at None, gets adopted, and the caller
    # hears the tail of a second answer starting mid-sentence.
    rejected_response_ids: set = field(default_factory=set)

    caller_speaking: bool = False
    assistant_speaking: bool = False
    closing: bool = False
    # The caller said an explicit ending. Set from their transcript, before the
    # assistant has replied, and what allows that reply to end the call however
    # it happens to be worded.
    goodbye_armed: bool = False
    silence_timer_armed: bool = False

    # The last things said, for an operator reading a stuck call. Transcript
    # text, never audio, and never persisted from here — the transcript store
    # has exactly one writer and it is not this.
    last_user_turn: str | None = None
    last_agent_response: str | None = None
    last_agent_question: str | None = None

    # Counters an operator can act on.
    duplicate_responses_suppressed: int = 0
    duplicate_tools_suppressed: int = 0

    # --- what the banking session owns --------------------------------------

    def _session(self):
        if self.session_manager is None:
            return None
        return self.session_manager.get_session(self.banking_session_id)

    @property
    def authenticated(self) -> bool:
        """Whether the deterministic PIN check has passed. Never inferred."""
        session = self._session()
        return bool(session and session.authenticated)

    @property
    def customer_id_received(self) -> bool:
        """Whether the caller has *claimed* an id. Not that it was accepted."""
        session = self._session()
        return bool(session and session.candidate_customer_id)

    @property
    def customer_reference(self) -> str | None:
        """The verified customer, or None.

        Deliberately null until authentication passes. A claimed id is a claim,
        and returning it here would let anything reading this state treat a
        stranger's assertion as an identity.
        """
        session = self._session()
        if session and session.authenticated:
            return session.customer_id
        return None

    @property
    def current_domain(self) -> str | None:
        session = self._session()
        return getattr(session, "current_domain", None) if session else None

    @property
    def current_intent(self) -> str | None:
        session = self._session()
        return getattr(session, "previous_intent", None) if session else None

    @property
    def pending_request(self) -> dict | None:
        """The enquiry held across authentication, if any.

        Read from `app.pending_request`, which is what the realtime tools use to
        resume it — so this reports the same thing that will actually be run,
        rather than a copy that could drift.
        """
        from app import pending_request as pending

        session = self._session()
        held = pending.recall(session) if session else None
        return held.to_dict() if held else None

    @property
    def selected_account_type(self) -> str | None:
        session = self._session()
        if not session:
            return None
        return session.conversation_context.get("account_type")

    @property
    def selected_loan_type(self) -> str | None:
        session = self._session()
        if not session:
            return None
        return session.conversation_context.get("loan_type")

    @property
    def awaiting_credential(self) -> str | None:
        """Which credential this call is waiting for, or None.

        Derived, like every other customer-facing property here, so there is
        exactly one answer and it is the one the guards use. A hand-tracked
        phase would go stale the moment `submit_customer_id` or `submit_pin`
        changed the session underneath it.

        `None` once the caller is verified, and `None` when verification is
        locked - a locked call is not waiting for anything, it is over.
        """
        if self.authentication_locked or self.authenticated:
            return None
        return "PIN" if self.customer_id_received else "CUSTOMER_ID"

    def outstanding_question(self) -> str | None:
        """Which question the bank has put to this caller and not had answered.

        The single place the two kinds of waiting are read together, because
        the media boundary treats them identically: a question has been put, so
        nothing may put one again until the caller has had their turn.

        Both halves are backend-owned state, never anything about the audio.
        `app.pending_credential` records that the demo customer ID or the PIN
        has been asked for; `app.pending_clarification.question_asked` - which
        Phase 7.4C already established - records that the Savings-or-Current
        question has. Neither is set by the bank merely speaking.
        """
        from app import pending_clarification, pending_credential

        session = self._session()

        credential = pending_credential.outstanding(session)
        if credential is not None:
            return credential

        pending = pending_clarification.recall(session)
        if pending is not None and pending.question_asked and not pending.complete:
            return f"CLARIFY_{pending.domain.value}"
        return None

    def question_delivery_failed(self) -> str | None:
        """A turn ended and the caller never heard the question. Allow one more.

        The gap between the two facts this class keeps apart. A question being
        *cued* is recorded by `app.pending_credential` and by Phase 7.4C's
        `pending_clarification.question_asked`, both set when the cue is
        submitted to the model - before it has generated a word. A question
        being *heard* is `delivered_question_kind`, taken at the media boundary
        by the response that actually carried audio to the line.

        When a turn ends with the first true and the second still None, nothing
        reached the caller. Left alone that is permanent silence: the cue record
        makes `awaiting_question` return None, so the pump never asks again, and
        only a completed caller turn clears it - which a caller who was never
        actually asked anything has no reason to produce.

        **Deliberately narrow.** It fires only when a question was outstanding
        *and* unclaimed. This runs at the end of every model turn, including the
        great majority that are not about a question at all, and revoking a mark
        on one of those would re-ask a question the caller had already answered.
        A question that *was* delivered is left owned, because the caller is
        being waited on exactly as intended.

        Bounded by the owning module, not here - see
        `pending_credential.MAX_QUESTION_ATTEMPTS`. Returns what became owed
        again, or None.
        """
        if self.delivered_question_kind is not None:
            return None

        outstanding = self.outstanding_question()
        if outstanding is None:
            return None

        from app import pending_clarification, pending_credential

        session = self._session()

        if outstanding.startswith("CLARIFY_"):
            if pending_clarification.delivery_failed(
                session, manager=self.session_manager
            ):
                return outstanding
            return None

        return pending_credential.delivery_failed(
            session, manager=self.session_manager
        )

    @property
    def credential_prompt_owed(self) -> bool:
        """Whether a question has been delivered and is still awaiting a reply.

        Read from the delivery record rather than recomputed, so it stays true
        for exactly as long as the boundary is actually suppressing repeats.
        """
        return self.delivered_question_kind is not None

    @property
    def authentication_locked(self) -> bool:
        session = self._session()
        return bool(session and session.authentication_locked)

    # --- transitions ---------------------------------------------------------

    def begin_caller_turn(self) -> int:
        """A new logical caller turn. Returns its id."""
        self.turn_counter += 1
        self.rejected_response_ids.clear()
        self.caller_speaking = True
        self.assistant_speaking = False
        self.silence_timer_armed = False
        return self.turn_counter

    def caller_supplied_a_turn(self) -> None:
        """A caller turn completed, so the bank may speak to them again.

        Deliberately *not* called on speech onset. A caller drawing breath, or
        starting to say their customer ID, is not an answer - and re-asking
        over the top of somebody mid-word is exactly what the live failure did.
        Only a completed transcript releases the next caller-facing prompt.

        Both the delivery record and the backend's own mark are released,
        whether or not what the caller said was usable. The boundary stops
        suppressing, and the backend is free to put the question again - which
        is how an unanswerable reply earns an authorised re-ask rather than a
        silence.
        """
        self.delivered_question_kind = None
        self.delivered_question_response = None
        self.delivered_question_item = None
        # The bank's own question response belonged to the waiting period that
        # has just ended. Keeping its id would judge the *next* question against
        # the wrong response, and a re-ask is a new response with a new token.
        self.owned_question_kind = None
        self.owned_question_response = None

        from app import pending_clarification, pending_credential

        session = self._session()

        pending_credential.clear(session, manager=self.session_manager)

        # The clarification's waiting period ends here too, and for the same
        # reason - but its *question* is not discarded, only its delivery
        # bookkeeping. `reset_delivery_attempts` re-arms an unanswered question
        # so the bank may put it once more, and leaves a completed one alone.
        #
        # Deliberately not `pending_clarification.clear`: that would throw away
        # the enquiry the caller is part-way through answering, and a caller who
        # said "I don't know" would lose the question rather than be asked it
        # again.
        pending_clarification.reset_delivery_attempts(
            session, manager=self.session_manager
        )

    def complete_turn(self) -> None:
        self.last_completed_turn_id = self.turn_counter
        self.assistant_speaking = False
        # Both halves of the owner, together. Generation for this turn has
        # ended, so the next response - and the next item - is a legitimate
        # answer to whatever comes next.
        self.active_response_id = None
        self.active_item_id = None

    def advance_stage(self) -> None:
        """Move the stage to match what the banking session now says.

        Derived rather than tracked by hand: a stage set at the moment a tool
        was called goes stale the instant the session changes underneath it.
        """
        if self.closing:
            self.stage = Stage.CLOSING
        elif self.authenticated:
            self.stage = Stage.SERVING
        elif self.customer_id_received:
            self.stage = Stage.VERIFYING
        elif self.turn_counter > 0:
            self.stage = Stage.IDENTIFYING
        else:
            self.stage = Stage.OPENING

    def operational_summary(self) -> dict:
        """How the call is running, with nothing about who is on it.

        The media view an operator watches shows this. It deliberately omits
        the customer reference and the session ids: a board that shows how a
        call is progressing does not need to name the caller, and a view that
        never carries an identity cannot leak one.
        """
        self.advance_stage()
        return {
            "stage": self.stage.value,
            "turn_counter": self.turn_counter,
            "last_completed_turn_id": self.last_completed_turn_id,
            "caller_speaking": self.caller_speaking,
            "assistant_speaking": self.assistant_speaking,
            "closing": self.closing,
            "silence_timer_armed": self.silence_timer_armed,
            "duplicate_responses_suppressed": self.duplicate_responses_suppressed,
            "duplicate_tools_suppressed": self.duplicate_tools_suppressed,
        }

    def describe(self) -> dict:
        """Operator-safe view.

        No PIN in any form, no audio, no transcript of what was said — only
        whether something was said. `customer_reference` appears **only** once
        the deterministic check has passed.
        """
        self.advance_stage()
        return {
            "provider_call_id": self.provider_call_id,
            "banking_session_id": self.banking_session_id,
            "stage": self.stage.value,
            "authenticated": self.authenticated,
            "customer_id_received": self.customer_id_received,
            "customer_reference": self.customer_reference,
            "authentication_locked": self.authentication_locked,
            "current_domain": self.current_domain,
            "current_intent": self.current_intent,
            "pending_request": self.pending_request,
            "selected_account_type": self.selected_account_type,
            "selected_loan_type": self.selected_loan_type,
            "turn_counter": self.turn_counter,
            "last_completed_turn_id": self.last_completed_turn_id,
            "active_response_id": self.active_response_id,
            "caller_speaking": self.caller_speaking,
            "assistant_speaking": self.assistant_speaking,
            "closing": self.closing,
            "silence_timer_armed": self.silence_timer_armed,
            "has_last_user_turn": self.last_user_turn is not None,
            "has_last_agent_response": self.last_agent_response is not None,
            "has_last_agent_question": self.last_agent_question is not None,
            "duplicate_responses_suppressed": self.duplicate_responses_suppressed,
            "duplicate_tools_suppressed": self.duplicate_tools_suppressed,
        }
