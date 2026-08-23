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

    def complete_turn(self) -> None:
        self.last_completed_turn_id = self.turn_counter
        self.assistant_speaking = False
        self.active_response_id = None

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
