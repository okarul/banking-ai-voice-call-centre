"""Phase 7.4E: the bank asks for a credential once, and then waits.

Fail-before-fix evidence for a live acceptance failure at commit 1112932.

Provider call `ca88679d-2c32-1240-4790-eaa5afddeeef`:

    CUSTOMER  "I want to know my account balance."
    AGENT     "Certainly. Before I access your banking information,
               may I have your demo customer ID, please?"
    TOOL      get_authentication_status  -> OK
    AGENT     "Certainly. Before I access your banking information,
               may I have your demo customer ID, please?"

No caller turn between the two prompts. The caller heard both.

**Why Phase 7.4D does not cover this.** That phase made the owner of a turn's
caller-visible audio the pair `(response_id, item_id)`, which stops a *second
output item of one response* reaching the line. Here there is a tool call in
between, and a tool result is a legitimate reason for the model to start a new
response - so the second prompt arrives as a genuinely new `response_id`, after
the first turn has genuinely ended. Every guard in 7.4D is working correctly and
none of them has anything to say about it.

The gap is that nothing in the backend records *that the bank has already asked
and is now waiting*. `ConversationState.Stage` cannot: it is derived, never
tracked - `advance_stage` computes it from `closing / authenticated /
customer_id_received / turn_counter` - so `IDENTIFYING` is equally true before
the question is asked and after. It is a coarse operator-facing summary, and its
own docstring says a hand-set stage goes stale immediately. So whether the
caller is asked twice is decided by whether the model volunteers the question
again after a tool result, which is the same class of defect as Phase 7.3, 7.4C
and 7.4D: a caller-visible action owned by the model rather than by the bank.

The invariant these tests pin:

    WHILE THE BANK IS WAITING FOR A CREDENTIAL IT HAS ALREADY ASKED FOR,
    NOTHING MAY ASK FOR IT AGAIN

until a new completed caller turn supplies a value, or a defined retry policy
authorises a repeat. No tool result, model continuation, duplicate response,
duplicate item, delayed callback or status probe is such an event.

Ownership, never text: nothing here compares utterances.

Both orderings of the live sequence are encoded, because whether `audio_end`
fires between the prompt and the tool decides whether 7.4D's ownership guard
happens to be holding at that moment - and the invariant must not depend on it.

**Fail-before-fix, recorded here so it cannot be lost.** Run against commit
1112932 - the 7.4D tree, with this phase's production change absent - the
faithful reproduction scored:

    4 failed, 5 passed

The split is the evidence, not the total. The four that failed all share one
shape:

    bank asks -> audio_end -> tool result / continuation -> asks again

`audio_end` had legitimately ended the turn and cleared `(response_id,
item_id)`, so the repeat arrived as a genuinely new response that 7.4D had no
reason to refuse.

The five that passed did so for an equally important reason: without
`audio_end`, the first turn still *owned* the audio, and Phase 7.4D correctly
suppressed the second response. That half was already right. This phase adds
the between-turns ownership that the other half needs, and must not disturb it
- which is why both orderings stay in this file permanently.
"""

import pytest

from app.agents import speech
from app.sessions import session_manager
from app.telephony.bridge import PhoneCallBridge
from app.telephony.media import LoopbackMediaTransport

# One distinct frame per prompt, so the number of frames that reached the
# caller *is* the number of prompts they heard.
FRAME_ID = b"\x01" * 160
FRAME_ID_AGAIN = b"\x02" * 160
FRAME_PIN = b"\x03" * 160
FRAME_PIN_AGAIN = b"\x04" * 160


@pytest.fixture(autouse=True)
def clean():
    yield
    session_manager.clear()


class Payload:
    """The inner `audio` payload, where `response_id` lives."""

    def __init__(self, data, response_id):
        self.type = "audio"
        self.data = data
        self.response_id = response_id


class Event:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class Content:
    def __init__(self, transcript):
        self.transcript = transcript
        self.text = None


class Item:
    def __init__(self, item_id, text, role="assistant", status="completed"):
        self.item_id = item_id
        self.role = role
        self.type = "message"
        self.status = status
        self.content = [Content(text)]


def audio(response_id, item_id, data=FRAME_ID):
    """One audio event, shaped as `agents.realtime.events.RealtimeAudio` is."""
    return Event("audio", audio=Payload(data, response_id), item_id=item_id)


def tool_start(name, arguments="{}"):
    """A tool invocation, in the shape `test_telephony_conversation` uses."""
    return Event("tool_start", tool=Event("t", name=name), arguments=arguments)


class FakeRealtime:
    def __init__(self):
        self.sent = []

    async def send_message(self, session_id, text):
        self.sent.append(text)

    async def send_audio(self, session_id, data):
        return None


def build_bridge(call_id="call-auth-own"):
    session = session_manager.create_session()
    return PhoneCallBridge(
        provider_call_id=call_id,
        banking_session_id=session.session_id,
        transport=LoopbackMediaTransport(),
        realtime_manager=FakeRealtime(),
        outbound_max_frames=200,
    )


def _classify(bridge, text):
    """What `RealtimeManager._pump_events` does before the bridge sees a turn.

    The bridge never classifies anything. Skipping this produces a session that
    looks like it awaits a credential but holds no protected enquiry, which is
    not the state the live failure happened in.
    """
    from app.realtime.turn_gate import record_turn

    record_turn(session_manager.get_session(bridge.banking_session_id), text)


def caller_says(bridge, text):
    """One completed caller turn, as the realtime pump delivers it.

    Both halves, because production does both and the difference is not
    cosmetic. `RealtimeManager._pump_events` classifies the turn through
    `turn_gate.record_turn` *and* hands the event to the bridge; the bridge
    itself never classifies anything. Driving only the bridge produces a
    session that looks like it is awaiting a credential but holds no enquiry -
    and an earlier version of this file did exactly that, which made these
    tests pass against a guard that was arming on every unverified call rather
    than on a question the bank had actually asked.
    """
    from app.realtime.turn_gate import record_turn

    sid = bridge.banking_session_id
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
        ),
    )
    # What the pump does before the bridge sees the transcript: classify the
    # turn, which is what holds a protected enquiry across authentication.
    record_turn(session_manager.get_session(sid), text)
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event(
                "input_audio_transcription_completed",
                transcript=text,
                item_id=f"caller-{bridge.conversation.turn_counter}",
            ),
        ),
    )


def agent_prompt(bridge, response_id, item_id, text, frame):
    """One assistant prompt: its history entry and one frame of audio.

    When the prompt being simulated *is* a credential question, this stands in
    for the realtime pump. In production `RealtimeManager._ask_for_credential`
    sends the cue and marks the question asked **before** the model generates a
    word, and that ordering is what the media boundary depends on: a question
    is already outstanding by the time its own audio arrives, so the delivering
    response is the one that takes ownership and everything after it is
    refused.

    The mapping from a simulated sentence to a credential lives here, in the
    harness, because the test is the thing that knows which utterance it means
    to be the question. **No production code compares text** - that is the
    whole point of `app.pending_credential`, and modelling the pump with a
    lookup here is what lets these tests exercise the real ownership path
    rather than a boundary rule that guesses.
    """
    sid = bridge.banking_session_id

    credential = {
        speech.AUTHENTICATION_REQUEST: "CUSTOMER_ID",
        speech.PIN_REQUEST: "PIN",
    }.get(text)
    if credential is not None:
        from app import pending_credential

        pending_credential.mark_question_asked(
            session_manager.get_session(sid),
            credential,
            manager=session_manager,
        )

    bridge.on_realtime_event(sid, Event("history_added", item=Item(item_id, text)))
    bridge.on_realtime_event(sid, audio(response_id, item_id, frame))


ASK_ID = speech.AUTHENTICATION_REQUEST
ASK_PIN = speech.PIN_REQUEST


# === the live failure of record =============================================


def test_a_status_probe_after_the_id_prompt_does_not_ask_again():
    """Call ca88679d, offline and deterministic, with the turn still open.

    The exact live ordering: the caller asks for a balance, the bank asks for
    the demo customer ID, `get_authentication_status` runs, and the model
    continues with the same question. No caller turn in between.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    assert len(bridge.outbound) == 1, "the first question must reach the caller"

    bridge.on_realtime_event(sid, tool_start("get_authentication_status"))
    agent_prompt(bridge, "resp_2", "item_2", ASK_ID, FRAME_ID_AGAIN)

    assert len(bridge.outbound) == 1, (
        "the caller was asked for their demo customer ID twice with no turn of "
        "their own in between - live call ca88679d-2c32-1240-4790-eaa5afddeeef"
    )


def test_a_status_probe_after_a_completed_id_prompt_does_not_ask_again():
    """The same, with the first turn properly ended before the tool.

    `audio_end` clears Phase 7.4D's `(response_id, item_id)` ownership, which is
    correct - generation for that turn really has finished. So this ordering is
    the one where 7.4D provably cannot help, and the invariant has to come from
    somewhere else.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))

    bridge.on_realtime_event(sid, tool_start("get_authentication_status"))
    agent_prompt(bridge, "resp_2", "item_2", ASK_ID, FRAME_ID_AGAIN)

    assert len(bridge.outbound) == 1, (
        "after the bank had asked and the turn had ended, a tool result let it "
        "ask the same question again"
    )


def test_repeated_status_probes_never_re_ask():
    """D. A model that probes three times still asks once."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)

    for n in range(3):
        bridge.on_realtime_event(sid, Event("audio_end"))
        bridge.on_realtime_event(sid, tool_start("get_authentication_status"))
        agent_prompt(bridge, f"resp_p{n}", f"item_p{n}", ASK_ID, FRAME_ID_AGAIN)

    assert len(bridge.outbound) == 1


def test_a_slow_caller_saying_their_id_is_not_re_prompted():
    """H/I. Speech onset without a completed transcript is not a new turn.

    The caller has begun saying their ID. Nothing about that may release the
    question again - which is what the caller on ca88679d experienced.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))

    # They start speaking, but no transcript has completed yet.
    bridge.on_realtime_event(
        sid,
        Event(
            "raw_model_event",
            data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
        ),
    )
    bridge.on_realtime_event(sid, tool_start("get_authentication_status"))
    agent_prompt(bridge, "resp_2", "item_2", ASK_ID, FRAME_ID_AGAIN)

    assert len(bridge.outbound) == 1, (
        "the bank asked again while the caller was part-way through answering"
    )


# === the PIN side of the same invariant =====================================


def test_a_status_probe_after_the_pin_prompt_does_not_ask_again():
    """PIN-B. The same protection, the other credential."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))

    caller_says(bridge, "DEMO001")
    # Actually supplied, not merely spoken. `candidate_customer_id` is set only
    # by `submit_customer_id`, so without this the backend never leaves the
    # customer-id stage and this test never reaches the PIN one it is named
    # for. It passed anyway until the ownership became precise, because the old
    # marker recorded whichever credential was awaited and suppressed the
    # repeat as a *customer-id* repeat.
    submit_customer_id(sid, "DEMO001")
    agent_prompt(bridge, "resp_2", "item_2", ASK_PIN, FRAME_PIN)
    spoken = len(bridge.outbound)
    bridge.on_realtime_event(sid, Event("audio_end"))

    bridge.on_realtime_event(sid, tool_start("get_authentication_status"))
    agent_prompt(bridge, "resp_3", "item_3", ASK_PIN, FRAME_PIN_AGAIN)

    assert len(bridge.outbound) == spoken, (
        "the caller was asked for their PIN twice with no turn of their own in "
        "between"
    )


# === over-suppression: the equally important half ===========================


def test_the_first_id_prompt_always_speaks():
    """13. Suppression must never silence the question itself."""
    bridge = build_bridge()

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)

    assert len(bridge.outbound) == 1, "the bank never asked at all"


def test_a_new_caller_turn_releases_the_next_prompt():
    """L. The caller answered; the bank may speak again.

    This is the boundary that makes the invariant safe rather than a way of
    going mute: a completed caller turn is exactly what authorises the next
    caller-facing prompt.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))

    caller_says(bridge, "DEMO001")
    agent_prompt(bridge, "resp_2", "item_2", ASK_PIN, FRAME_PIN)

    assert len(bridge.outbound) == 2, (
        "the caller answered and the bank was silenced instead of moving on"
    )


def test_an_invalid_id_may_be_asked_for_again_after_a_caller_turn():
    """K/L. A retry is authorised by the caller speaking, not by a tool."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))

    # The caller says something that is not a usable id.
    caller_says(bridge, "um, I'm not sure")
    agent_prompt(bridge, "resp_2", "item_2", ASK_ID, FRAME_ID_AGAIN)

    assert len(bridge.outbound) == 2, (
        "the caller gave an unusable id and the bank could not ask again"
    )


def test_a_banking_answer_after_verification_still_speaks():
    """13. The ordinary business answer is not a credential prompt."""
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))
    caller_says(bridge, "DEMO001")
    agent_prompt(bridge, "resp_2", "item_2", ASK_PIN, FRAME_PIN)
    bridge.on_realtime_event(sid, Event("audio_end"))
    caller_says(bridge, "Four eight two one.")

    agent_prompt(
        bridge, "resp_3", "item_3",
        "Your savings balance is 12,450 dollars and 75 cents.", FRAME_PIN_AGAIN,
    )

    assert len(bridge.outbound) == 3, "the banking answer was suppressed"


def test_a_caller_turn_delivered_by_history_also_releases_the_prompt():
    """The caller's words do not always arrive as a transcription event.

    They can come as a `history_added` user item, or as a `history_updated`
    snapshot with no `history_added` at all - `_on_caller_text` exists
    precisely because of that, and says so. A release wired to one
    representation would mute the bank for a caller who answered by another
    route, which is worse than the duplicate this phase fixes.
    """
    bridge = build_bridge()
    sid = bridge.banking_session_id

    caller_says(bridge, "I want to know my account balance.")
    agent_prompt(bridge, "resp_1", "item_1", ASK_ID, FRAME_ID)
    bridge.on_realtime_event(sid, Event("audio_end"))
    assert bridge.conversation.credential_prompt_owed is True

    # The caller answers, and the SDK delivers it as a history item.
    bridge.on_realtime_event(
        sid,
        Event("history_added", item=Item("u1", "DEMO001", role="user")),
    )

    assert bridge.conversation.credential_prompt_owed is False, (
        "a caller turn delivered by history did not release the bank to speak"
    )

    agent_prompt(bridge, "resp_2", "item_2", ASK_PIN, FRAME_PIN)
    assert len(bridge.outbound) == 2, "the bank stayed mute after the caller answered"


# === §6-§10: the matrices, the flows, and the soak ==========================
#
# Everything below measures what the *caller* would hear. A prompt counts only
# if it increased the outbound queue, so these are utterance counts rather than
# tool counts or final-state checks - a flow that ends verified having asked for
# the PIN twice is a failed flow.


from app.auth.authentication import MAX_AUTHENTICATION_ATTEMPTS, submit_customer_id, submit_pin
from app.config import settings

REAL_PIN = "4821"
WRONG_PIN = "0000"
CUSTOMER = "DEMO001"


class Heard:
    """One simulated call, counting what reached the caller, by kind."""

    def __init__(self, call_id="flow"):
        self.bridge = build_bridge(call_id)
        self.counts = {}
        self._n = 0
        # Items whose audio reached the caller. A prompt is a distinct item,
        # not a frame: one sentence is hundreds of frames, and a duplicated SDK
        # callback re-delivers the same item, which Phase 7.4D admits on
        # purpose - "the same answer delivered twice is still one answer".
        # Counting frames would score that as two prompts and call correct
        # behaviour a defect.
        self._items_heard = set()

    @property
    def sid(self):
        return self.bridge.banking_session_id

    def caller(self, text):
        caller_says(self.bridge, text)

    def says_id(self, customer=CUSTOMER):
        """The caller speaks their id, and the model submits it."""
        self.caller(f"my customer id is {customer}")
        return submit_customer_id(self.sid, customer)

    def says_pin(self, pin=REAL_PIN):
        self.caller("my pin is " + " ".join(pin))
        return submit_pin(self.sid, pin)

    def agent(self, kind, *, response=None, item=None):
        """The agent speaks one item. Returns whether the caller heard it."""
        self._n += 1
        response = response or f"resp_{self._n}"
        item = item or f"item_{self._n}"
        before = len(self.bridge.outbound)
        agent_prompt(self.bridge, response, item, kind, bytes([self._n % 251 or 1]) * 160)
        heard = len(self.bridge.outbound) > before
        if heard and item not in self._items_heard:
            self._items_heard.add(item)
            self.counts[kind] = self.counts.get(kind, 0) + 1
        return heard

    def generation_ended(self):
        self.bridge.on_realtime_event(self.sid, Event("audio_end"))

    def tool(self, name):
        self.bridge.on_realtime_event(self.sid, tool_start(name))

    def heard(self, kind):
        return self.counts.get(kind, 0)

    # --- the three ways the caller's words actually arrive -------------------
    #
    # `_on_caller_text` is the single funnel precisely because the SDK picks
    # one of these and we do not get a say. Each must release the bank.

    def caller_via_history_added(self, text):
        """A `history_added` user item."""
        self._n += 1
        _classify(self.bridge, text)
        self.bridge.on_realtime_event(
            self.sid,
            Event("history_added", item=Item(f"u-{self._n}", text, role="user")),
        )

    def caller_via_history_updated(self, text):
        """A `history_updated` snapshot, with no `history_added` at all.

        The commonest real shape: the server creates the conversation item when
        speech starts and fills the transcript in later, so the change arrives
        as a whole-conversation snapshot.
        """
        self._n += 1
        _classify(self.bridge, text)
        self.bridge.on_realtime_event(
            self.sid,
            Event(
                "history_updated",
                history=[Item(f"h-{self._n}", text, role="user")],
            ),
        )

    def speech_starts(self):
        """Onset only. Not an answer, and must release nothing."""
        self.bridge.on_realtime_event(
            self.sid,
            Event(
                "raw_model_event",
                data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
            ),
        )

    def barge_in(self):
        """The caller talks over the bank."""
        self.bridge.on_realtime_event(self.sid, Event("audio_interrupted"))

    @property
    def phase(self):
        """Which question the caller has actually been asked, or None."""
        return self.bridge.conversation.delivered_question_kind

    @property
    def owed(self):
        return self.bridge.conversation.credential_prompt_owed


ANSWER = "Your savings balance is 12,450 dollars and 75 cents."
CLARIFY = "Which account would you like, Savings or Current?"
GOODBYE = "Thank you for calling. Goodbye."


# --- §6 customer-id matrix --------------------------------------------------


def test_6a_a_banking_request_is_answered_with_one_id_prompt():
    call = Heard("m-6a")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    assert call.heard(ASK_ID) == 1


def test_6b_an_auth_status_probe_before_the_prompt_still_asks_once():
    call = Heard("m-6b")
    call.caller("I want to know my account balance.")
    call.tool("get_authentication_status")
    call.agent(ASK_ID)
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_ID)
    assert call.heard(ASK_ID) == 1


def test_6e_a_duplicate_model_response_does_not_re_ask():
    call = Heard("m-6e")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID, response="r1", item="i1")
    call.generation_ended()
    call.agent(ASK_ID, response="r2", item="i2")
    assert call.heard(ASK_ID) == 1


def test_6f_a_second_output_item_does_not_re_ask():
    call = Heard("m-6f")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID, response="r1", item="i1")
    call.agent(ASK_ID, response="r1", item="i2")
    assert call.heard(ASK_ID) == 1


def test_6g_a_delayed_callback_does_not_re_ask():
    call = Heard("m-6g")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID, response="r1", item="i1")
    call.generation_ended()
    call.tool("get_authentication_status")
    call.tool("get_authentication_status")
    call.agent(ASK_ID, response="r-late", item="i-late")
    assert call.heard(ASK_ID) == 1


def test_6j_a_valid_id_advances_to_the_pin_question():
    call = Heard("m-6j")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    assert call.agent(ASK_PIN) is True, "the PIN question was suppressed"
    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1


# --- §7 PIN matrix ----------------------------------------------------------


def test_7a_the_pin_is_asked_exactly_once():
    call = Heard("m-7a")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN)
    call.generation_ended()
    call.agent(ASK_PIN)
    assert call.heard(ASK_PIN) == 1


def test_7bc_tool_continuation_and_duplicates_never_re_ask_the_pin():
    call = Heard("m-7bc")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN, response="p1", item="pi1")
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_PIN, response="p2", item="pi2")
    call.agent(ASK_PIN, response="p2", item="pi3")
    assert call.heard(ASK_PIN) == 1


def test_7e_a_correct_pin_verifies_once_and_the_answer_speaks():
    call = Heard("m-7e")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN)
    call.generation_ended()
    result = call.says_pin()
    assert result["success"] is True
    assert call.agent(ANSWER) is True, "the banking answer was suppressed"
    assert call.heard(ANSWER) == 1


def test_7f_a_wrong_pin_allows_exactly_one_authorised_retry():
    call = Heard("m-7f")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN)
    call.generation_ended()

    wrong = call.says_pin(WRONG_PIN)
    assert wrong["success"] is False
    # The caller spoke, so the bank may ask again - once.
    assert call.agent(ASK_PIN) is True
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_PIN)

    assert call.heard(ASK_PIN) == 2, "a wrong PIN must authorise one retry, not more"


def test_7g_the_per_call_attempt_limit_is_unchanged():
    """Three wrong guesses inside one call, per MAX_AUTHENTICATION_ATTEMPTS."""
    call = Heard("m-7g")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()

    outcomes = [call.says_pin(WRONG_PIN) for _ in range(MAX_AUTHENTICATION_ATTEMPTS)]

    assert all(o["success"] is False for o in outcomes)
    session = session_manager.get_session(call.sid)
    assert session.authentication_locked is True
    assert MAX_AUTHENTICATION_ATTEMPTS == 3


def test_7h_the_persistent_lockout_threshold_is_unchanged():
    """The cross-call lock, which is the five-attempt one."""
    assert settings.pin_lockout_max_attempts == 5
    assert settings.pin_lockout_minutes == 15


def test_7d_a_slow_pin_utterance_is_not_interrupted():
    call = Heard("m-7d")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN)
    call.generation_ended()

    # The caller begins saying the PIN; no transcript has completed.
    call.bridge.on_realtime_event(
        call.sid,
        Event(
            "raw_model_event",
            data=Event("x", data={"type": "input_audio_buffer.speech_started"}),
        ),
    )
    call.tool("get_authentication_status")
    call.agent(ASK_PIN)

    assert call.heard(ASK_PIN) == 1, "the bank spoke over a caller mid-PIN"


# --- §8 / §9 full business flows, counted by what the caller heard ----------


def test_flow_1_generic_balance_clarified():
    call = Heard("f1")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()
    call.agent(CLARIFY); call.generation_ended()
    call.caller("Savings")
    call.agent(ANSWER); call.generation_ended()
    call.caller("that's all, thank you")
    call.agent(GOODBYE)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(CLARIFY) == 1
    assert call.heard(ANSWER) == 1
    assert call.heard(GOODBYE) == 1


def test_flow_2_direct_savings_balance():
    call = Heard("f2")
    call.caller("what is my savings balance")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()
    call.agent(ANSWER); call.generation_ended()
    call.caller("goodbye")
    call.agent(GOODBYE)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(ANSWER) == 1
    assert call.heard(GOODBYE) == 1


def test_flow_4_one_wrong_pin_then_correct():
    call = Heard("f4")
    call.caller("what is my savings balance")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin(WRONG_PIN)
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()
    call.agent(ANSWER)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 2, "one prompt, one authorised retry"
    assert call.heard(ANSWER) == 1


def test_flow_8_tool_continuation_immediately_after_the_id_prompt():
    call = Heard("f8")
    call.caller("what is my savings balance")
    call.agent(ASK_ID)
    call.tool("get_authentication_status")
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_ID)
    call.says_id()
    call.agent(ASK_PIN)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1


def test_flow_9_duplicate_sdk_callbacks_around_auth():
    call = Heard("f9")
    call.caller("what is my savings balance")
    call.agent(ASK_ID, response="r1", item="i1")
    call.agent(ASK_ID, response="r1", item="i1")   # duplicated callback
    call.generation_ended()
    call.agent(ASK_ID, response="r2", item="i2")   # new response
    call.says_id()
    call.agent(ASK_PIN, response="r3", item="i3")
    call.agent(ASK_PIN, response="r3", item="i3")  # duplicated callback

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1


# --- §10 soak ---------------------------------------------------------------


def test_soak_one_hundred_authentication_flows():
    """100 sequential simulated calls, mixing every hazard this phase found.

    Deterministic and offline: no provider, no paid call, no sleeping. What is
    asserted is what the caller would have heard on each one.
    """
    duplicates = []
    missing = []

    for n in range(100):
        variant = n % 5
        call = Heard(f"soak-{n}")
        call.caller("I want to know my account balance.")

        call.agent(ASK_ID)
        if variant == 0:
            call.generation_ended(); call.tool("get_authentication_status")
            call.agent(ASK_ID)
        elif variant == 1:
            call.agent(ASK_ID, response="dup", item="dup-i")
        elif variant == 2:
            call.bridge.on_realtime_event(
                call.sid,
                Event("raw_model_event",
                      data=Event("x", data={"type": "input_audio_buffer.speech_started"})),
            )
            call.agent(ASK_ID)
        call.generation_ended()

        call.says_id()
        call.agent(ASK_PIN)
        if variant == 3:
            call.generation_ended(); call.tool("get_authentication_status")
            call.agent(ASK_PIN)
        call.generation_ended()

        if variant == 4:
            call.says_pin(WRONG_PIN)
            call.agent(ASK_PIN)
            call.generation_ended()
        call.says_pin()
        call.agent(ANSWER)

        expected_pin = 2 if variant == 4 else 1
        if call.heard(ASK_ID) != 1:
            duplicates.append((n, "ID", call.heard(ASK_ID)))
        if call.heard(ASK_PIN) != expected_pin:
            duplicates.append((n, "PIN", call.heard(ASK_PIN)))
        if call.heard(ANSWER) != 1:
            missing.append((n, "ANSWER", call.heard(ANSWER)))

        session_manager.destroy_session(call.sid)

    assert duplicates == [], f"duplicate or missing credential prompts: {duplicates[:5]}"
    assert missing == [], f"missing banking answers: {missing[:5]}"


def test_soak_leaves_no_state_between_sessions():
    """A fresh call must never inherit another call's awaiting state."""
    first = Heard("leak-1")
    first.caller("I want to know my account balance.")
    first.agent(ASK_ID)
    assert first.bridge.conversation.credential_prompt_owed is True

    second = Heard("leak-2")
    assert second.bridge.conversation.credential_prompt_owed is False
    second.caller("I want to know my account balance.")
    assert second.agent(ASK_ID) is True, "a fresh call was muted by another call"


# === §1 completion: the customer-id cases not already named above ============
#
# A, B, E, F, G and J are the `test_6*` tests above. C, D, H, I, K and L are
# named here so the matrix is complete and permanent rather than implied.

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import pending_clarification
from app.auth import lockout
from app.auth.authentication import LOCK_SCOPE_PERSISTENT, LOCK_SCOPE_SESSION
from app.database.connection import session_scope
from app.database.models import CustomerAuthLock

GREETING = "Thank you for calling the bank. How may I help you today?"
# A harmless acknowledgement. Not a question, and it must never be mistaken for
# one - see `tests/test_prompt_delivery_ownership.py`.
PREFACE = "Certainly, I can help you with that."
REFUSAL = "I'm sorry, I can't help with card replacements on this line."
CURRENT_ANSWER = "Your current account balance is 3,820 dollars and 10 cents."
RECOVERY = "Your savings balance is 12,450 dollars and 75 cents, as I was saying."
LOCKED_LINE = "I'm sorry, I can't verify you on this call."


def test_6c_prompt_then_audio_end_then_tool_continuation_does_not_re_ask():
    """C. The exact live shape, counted as the caller would hear it."""
    call = Heard("m-6c")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_ID)
    assert call.heard(ASK_ID) == 1


def test_6d_repeated_auth_status_results_do_not_re_ask():
    """D. Probing does not buy the model another question."""
    call = Heard("m-6d")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    for _ in range(4):
        call.generation_ended()
        call.tool("get_authentication_status")
        call.agent(ASK_ID)
    assert call.heard(ASK_ID) == 1


def test_6h_a_slow_caller_beginning_their_id_is_not_re_prompted():
    """H. Speaking over somebody mid-answer is the live complaint itself."""
    call = Heard("m-6h")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    call.speech_starts()
    call.agent(ASK_ID)
    assert call.heard(ASK_ID) == 1


def test_6i_speech_onset_alone_does_not_release_prompt_ownership():
    """I. Onset is not a turn. Asserted on the state, not only the audio."""
    call = Heard("m-6i")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    assert call.owed is True

    call.speech_starts()

    assert call.owed is True, "a caller drawing breath released the question"
    assert call.phase == "CUSTOMER_ID"


def test_6k_an_invalid_id_on_a_new_caller_turn_authorises_one_retry():
    """K. A completed turn carrying an unusable value is a real event."""
    call = Heard("m-6k")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()

    call.caller("um, I don't have it in front of me")
    assert call.agent(ASK_ID) is True, "the bank could not ask again"

    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_ID)

    assert call.heard(ASK_ID) == 2, "one prompt, one authorised retry, no more"


def test_6l_a_later_caller_turn_is_never_permanently_suppressed():
    """L. The gate must not be a one-way door."""
    call = Heard("m-6l")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()

    for n in range(3):
        call.caller(f"sorry, what was that, {n}")
        assert call.agent(ASK_ID) is True, f"the bank went mute on turn {n}"
        call.generation_ended()

    assert call.heard(ASK_ID) == 4


# === §2 completion: the PIN matrix ==========================================


def test_pin_c_a_duplicate_model_response_does_not_re_ask():
    call = Heard("p-c")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN, response="p1", item="pi1")
    call.generation_ended()
    call.agent(ASK_PIN, response="p2", item="pi2")
    assert call.heard(ASK_PIN) == 1


def test_pin_d_a_second_output_item_does_not_re_ask():
    call = Heard("p-d")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN, response="p1", item="pi1")
    call.agent(ASK_PIN, response="p1", item="pi2")
    assert call.heard(ASK_PIN) == 1


def test_pin_e_a_delayed_callback_does_not_re_ask():
    call = Heard("p-e")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN, response="p1", item="pi1")
    call.generation_ended()
    call.tool("get_authentication_status")
    call.agent(ASK_PIN, response="p-late", item="pi-late")
    assert call.heard(ASK_PIN) == 1


def test_pin_i_the_per_call_limit_is_three_and_locks_this_call_only():
    """I. The per-call mechanism, tested apart from the persistent one.

    Three wrong guesses inside one call end the call's attempts. The claimed id
    has only three failures against it, which is below the persistent
    threshold, so the lock reported must be the *session* one. If this ever
    reports PERSISTENT it means the two mechanisms have been merged.
    """
    assert MAX_AUTHENTICATION_ATTEMPTS == 3

    call = Heard("p-i")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()

    outcomes = [call.says_pin(WRONG_PIN) for _ in range(MAX_AUTHENTICATION_ATTEMPTS)]

    assert all(o["success"] is False for o in outcomes)
    assert outcomes[-1]["reason"] == "AUTHENTICATION_LOCKED"
    assert outcomes[-1]["lock_scope"] == LOCK_SCOPE_SESSION
    assert session_manager.get_session(call.sid).authentication_locked is True
    assert lockout.is_locked(CUSTOMER) is None, "the id itself must not be locked yet"


def test_pin_j_the_persistent_threshold_is_five_across_calls():
    """J. The cross-call mechanism, using the existing lockout helper only."""
    assert settings.pin_lockout_max_attempts == 5

    for n in range(settings.pin_lockout_max_attempts - 1):
        state = lockout.record_failure(CUSTOMER)
        assert state.locked is False, f"locked early, after {n + 1}"

    assert lockout.record_failure(CUSTOMER).locked is True


def test_pin_k_the_fifth_wrong_pin_across_calls_locks_the_id():
    """K. Through the real authentication path, one guess per call."""
    results = []
    for _ in range(settings.pin_lockout_max_attempts):
        session = session_manager.create_session()
        submit_customer_id(session.session_id, CUSTOMER)
        results.append(submit_pin(session.session_id, WRONG_PIN))
        session_manager.destroy_session(session.session_id)

    assert results[-1]["lock_scope"] == LOCK_SCOPE_PERSISTENT
    assert lockout.is_locked(CUSTOMER) is not None

    # And the redial finds the door shut, correct PIN or not.
    again = session_manager.create_session()
    submit_customer_id(again.session_id, CUSTOMER)
    blocked = submit_pin(again.session_id, REAL_PIN)
    assert blocked["reason"] == "AUTHENTICATION_LOCKED"
    assert blocked["authenticated"] is False


def test_pin_l_the_persistent_lock_is_shared_across_channels():
    """L. Burned on the browser channel, enforced on the telephone one.

    The phone session here is a real `PhoneCallBridge` session rather than a
    plain one, so this is the telephone path and not a restatement of the
    browser test.
    """
    for _ in range(settings.pin_lockout_max_attempts):
        browser = session_manager.create_session()
        submit_customer_id(browser.session_id, CUSTOMER)
        submit_pin(browser.session_id, WRONG_PIN)
        session_manager.destroy_session(browser.session_id)

    phone = Heard("p-l-phone")
    phone.caller("I want to know my account balance.")
    phone.agent(ASK_ID)
    phone.generation_ended()
    submit_customer_id(phone.sid, CUSTOMER)
    result = submit_pin(phone.sid, REAL_PIN)

    assert result["reason"] == "AUTHENTICATION_LOCKED"
    assert result["lock_scope"] == LOCK_SCOPE_PERSISTENT
    assert result["authenticated"] is False


def test_pin_m_the_lock_lasts_fifteen_minutes():
    """M. Duration proved by injected time, never by sleeping."""
    assert settings.pin_lockout_minutes == 15

    now = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    for _ in range(settings.pin_lockout_max_attempts):
        lockout.record_failure(CUSTOMER, now=now)

    assert lockout.is_locked(CUSTOMER, now=now) is not None
    inside = now + timedelta(minutes=settings.pin_lockout_minutes - 1)
    assert lockout.is_locked(CUSTOMER, now=inside) is not None, "released early"
    outside = now + timedelta(minutes=settings.pin_lockout_minutes + 1)
    assert lockout.is_locked(CUSTOMER, now=outside) is None, "never released"


def test_pin_n_authentication_succeeds_again_once_the_lock_expires():
    """N. Recovery, using the existing expiry idiom rather than a new one."""
    # Deliberately a past date. The failures are recorded through an injected
    # clock, but `submit_pin` reads the real one - so a base date of "today"
    # leaves `locked_until` in the future and the caller stays locked no matter
    # how the row is wound back. That is what the first draft of this test did,
    # and it failed for a reason that had nothing to do with the product.
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    for _ in range(settings.pin_lockout_max_attempts):
        lockout.record_failure(CUSTOMER, now=now)

    with session_scope() as db:
        row = db.scalars(
            select(CustomerAuthLock).where(CustomerAuthLock.customer_id == CUSTOMER)
        ).one()
        row.locked_until = now - timedelta(minutes=1)
        row.last_failed_at = now - timedelta(hours=2)

    call = Heard("p-n")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()

    assert call.says_pin()["authenticated"] is True
    assert call.agent(ANSWER) is True, "a recovered caller was left in silence"


# === §3 completion: the remaining business flows ============================


def test_flow_3_current_account_balance():
    call = Heard("f3")
    call.caller("what is my current account balance")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()
    call.agent(CURRENT_ANSWER)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(CURRENT_ANSWER) == 1


def test_flow_5_invalid_customer_id_then_a_valid_one():
    call = Heard("f5")
    call.caller("what is my savings balance")
    call.agent(ASK_ID); call.generation_ended()

    call.caller("it's, er, one two three")          # not a usable id
    assert call.agent(ASK_ID) is True
    call.generation_ended()

    call.says_id()
    assert call.agent(ASK_PIN) is True
    call.generation_ended()
    call.says_pin()
    call.agent(ANSWER)

    assert call.heard(ASK_ID) == 2, "one prompt, one authorised retry"
    assert call.heard(ASK_PIN) == 1
    assert call.heard(ANSWER) == 1


def test_flow_6_barge_in_during_a_business_answer_recovers():
    """Barge-in is not a credential situation, and must not become one."""
    call = Heard("f6")
    call.caller("what is my savings balance")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()

    call.agent(ANSWER)
    call.barge_in()
    call.caller("sorry, say that again")

    assert call.agent(RECOVERY) is True, "the recovery answer was suppressed"
    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1


def test_flow_7_a_slow_spoken_customer_id_gets_no_duplicate_prompt():
    call = Heard("f7")
    call.caller("what is my savings balance")
    call.agent(ASK_ID)
    call.generation_ended()
    for _ in range(3):
        call.speech_starts()
        call.agent(ASK_ID)
    call.says_id()
    call.agent(ASK_PIN)

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1


def test_flow_10_clarification_is_asked_exactly_once():
    """Phase 7.4C's question, counted at the caller rather than in state."""
    call = Heard("f10")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()

    call.agent(CLARIFY)
    call.generation_ended()
    call.caller("Savings")
    call.agent(ANSWER)

    assert call.heard(CLARIFY) == 1
    assert call.heard(ANSWER) == 1


def _flow_by_delivery(call, deliver):
    """One whole authentication, with the caller's words arriving one way."""
    deliver(call, "I want to know my account balance.")
    call.agent(ASK_ID)
    call.generation_ended()
    assert call.owed is True, "the bank did not record that it had asked"

    deliver(call, "my customer id is DEMO001")
    assert call.owed is False, "this delivery path did not release the bank"
    submit_customer_id(call.sid, CUSTOMER)

    assert call.agent(ASK_PIN) is True, "the bank stayed mute after the answer"
    call.generation_ended()

    deliver(call, "my pin is four eight two one")
    submit_pin(call.sid, REAL_PIN)
    assert call.agent(ANSWER) is True

    assert call.heard(ASK_ID) == 1
    assert call.heard(ASK_PIN) == 1
    assert call.heard(ANSWER) == 1


def test_flow_11_caller_text_via_raw_transcription():
    _flow_by_delivery(Heard("f11"), lambda c, t: c.caller(t))


def test_flow_12_caller_text_via_history_added():
    _flow_by_delivery(Heard("f12"), lambda c, t: c.caller_via_history_added(t))


def test_flow_13_caller_text_via_history_updated_snapshot():
    _flow_by_delivery(Heard("f13"), lambda c, t: c.caller_via_history_updated(t))


# === §5 the over-suppression matrix =========================================
#
# The fix is rejected if it silences legitimate speech. The first 7.4E draft
# armed on every unauthenticated call and muted greetings and refusals; nine
# tests in `test_telephony_conversation` caught it. These make that permanent
# here too, in caller-visible terms.


def test_a_fresh_unauthenticated_call_with_no_held_enquiry_is_unrestricted():
    """The exact shape the first draft broke."""
    call = Heard("os-fresh")
    assert call.agent(GREETING) is True, "the greeting was suppressed"
    call.generation_ended()
    call.caller("hello there")
    assert call.agent("How may I help you today?") is True
    call.generation_ended()
    call.caller("can I replace my card")
    assert call.agent(REFUSAL) is True, "a refusal was suppressed"


def test_every_legitimate_caller_facing_utterance_still_speaks():
    """One call, walked end to end, asserting each step was heard."""
    call = Heard("os-walk")

    assert call.agent(GREETING) is True, "greeting"
    call.generation_ended()

    call.caller("I want to know my account balance.")
    assert call.agent(ASK_ID) is True, "first customer-ID question"
    call.generation_ended()

    call.caller("I'm not sure what that is")
    assert call.agent(ASK_ID) is True, "authorised invalid-ID retry"
    call.generation_ended()

    call.says_id()
    assert call.agent(ASK_PIN) is True, "first PIN question"
    call.generation_ended()

    call.says_pin(WRONG_PIN)
    assert call.agent(ASK_PIN) is True, "authorised wrong-PIN retry"
    call.generation_ended()

    call.says_pin()
    assert call.agent("Thank you, you're verified.") is True, "verified continuation"
    call.generation_ended()

    assert call.agent(CLARIFY) is True, "account clarification"
    call.generation_ended()
    call.caller("Savings")
    assert call.agent(ANSWER) is True, "banking result"
    call.barge_in()
    call.caller("sorry, again please")
    assert call.agent(RECOVERY) is True, "barge-in recovery"
    call.generation_ended()
    call.caller("that's all, thank you")
    assert call.agent(GOODBYE) is True, "goodbye"


def test_a_locked_call_can_still_tell_the_caller_so():
    """Being locked out is not a reason to go silent on somebody."""
    call = Heard("os-locked")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    for _ in range(MAX_AUTHENTICATION_ATTEMPTS):
        call.says_pin(WRONG_PIN)

    assert call.agent(LOCKED_LINE) is True, "a locked-out caller heard nothing"


# === §7 state cleanup: the phase must not leak ==============================


def test_the_phase_clears_on_a_completed_caller_turn():
    call = Heard("lk-turn")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    assert call.phase == "CUSTOMER_ID"
    call.caller("DEMO001 I think")
    assert call.phase is None


def test_the_phase_does_not_survive_the_id_to_pin_transition():
    call = Heard("lk-transition")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()

    assert call.phase is None
    assert call.owed is False, "a stale CUSTOMER_ID marker blocked the PIN question"
    call.agent(ASK_PIN)
    assert call.phase == "PIN"


def test_the_phase_is_irrelevant_once_authenticated():
    call = Heard("lk-auth")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin()

    assert call.bridge.conversation.awaiting_credential is None
    assert call.owed is False


def test_the_phase_does_not_strand_a_failed_retry():
    call = Heard("lk-retry")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    call.says_pin(WRONG_PIN)

    assert call.owed is False, "a wrong PIN left the bank unable to ask again"
    assert call.agent(ASK_PIN) is True


def test_the_phase_does_not_strand_a_locked_call():
    call = Heard("lk-locked")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.says_id()
    call.agent(ASK_PIN); call.generation_ended()
    for _ in range(MAX_AUTHENTICATION_ATTEMPTS):
        call.says_pin(WRONG_PIN)

    assert call.bridge.conversation.awaiting_credential is None
    assert call.owed is False


def test_barge_in_does_not_strand_the_phase():
    call = Heard("lk-barge")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID)
    call.barge_in()
    call.caller("DEMO001")

    assert call.phase is None
    assert call.owed is False


def test_closing_still_lets_the_goodbye_be_heard():
    """Closing on an unverified call.

    The marker is deliberately text-free - it records that the bank spoke while
    a credential was awaited, and cannot know that this particular sentence was
    a goodbye rather than a question. So the phase *is* set here, and that is
    the design rather than a leak: what matters is that the caller hears the
    goodbye, and that nothing carries into the next call (see
    `test_a_new_call_after_teardown_starts_clean`).

    An earlier draft of this test asserted the phase was cleared, which
    contradicted the design it was meant to protect.
    """
    call = Heard("lk-close")
    call.caller("I want to know my account balance.")
    call.agent(ASK_ID); call.generation_ended()
    call.caller("actually, goodbye")

    assert call.agent(GOODBYE) is True, "the caller never heard the goodbye"
    assert call.heard(GOODBYE) == 1


def test_a_new_call_after_teardown_starts_clean():
    first = Heard("lk-teardown-1")
    first.caller("I want to know my account balance.")
    first.agent(ASK_ID)
    assert first.owed is True
    session_manager.destroy_session(first.sid)

    second = Heard("lk-teardown-2")
    assert second.phase is None
    assert second.owed is False
    second.caller("I want to know my account balance.")
    assert second.agent(ASK_ID) is True, "a new call inherited a finished one's silence"


# === §6 the mixed soak ======================================================


def cue_like_the_pump(call, credential):
    """Record a credential question as cued, without speaking a word.

    What `RealtimeManager._ask_for_credential` does: send the cue, then mark.
    Used by the soak flows where the point is that **no audio follows** - the
    model produced none, or the response was abandoned - so `Heard.agent` must
    not be called, because that would deliver the question this flow is about
    the caller never hearing.
    """
    from app import pending_credential

    pending_credential.mark_question_asked(
        session_manager.get_session(call.sid),
        credential,
        manager=session_manager,
    )


def question_owed(call):
    """Which credential question the bank may still put, or None."""
    from app import pending_credential

    return pending_credential.awaiting_question(
        session_manager.get_session(call.sid)
    )


def own_question_like_the_pump(call, kind, response_id):
    """Issue, mark and confirm a backend-owned question response.

    Phase 7.4F, and everything `RealtimeManager._ask_for_credential` does bar
    the socket: mint the correlation token, record the cue as submitted, then
    hand the bridge the `response.created` the server sends back. Returns the
    owned response id, which is the only response the caller-facing audio may
    arrive on.

    Defined here rather than imported from `test_owned_question_regressions`,
    which imports *this* module - the other direction would be a cycle.
    """
    from app import pending_credential

    token = pending_credential.issue_owned_question(
        session_manager.get_session(call.sid), kind, manager=session_manager
    )
    pending_credential.mark_question_asked(
        session_manager.get_session(call.sid), kind, manager=session_manager
    )
    call.bridge.on_realtime_event(
        call.sid,
        Event(
            "raw_model_event",
            data=Event(
                "raw_server_event",
                data={
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "metadata": {
                            "bank_question": kind,
                            "token": token,
                            "session": call.sid,
                        },
                    },
                },
            ),
        ),
    )
    return response_id


def owned_response_id(call):
    """The owned question response this call has adopted, or None."""
    return call.bridge.conversation.owned_question_response


def orphaned_owned_correlation(call):
    """Whether a finished waiting period left an owned response id behind."""
    from app import pending_credential

    return (
        pending_credential.owned_question(session_manager.get_session(call.sid))
        is not None
    )


def end_turn_in_a_loop(call):
    """`audio_end`, actually executed rather than merely emitted.

    The bridge defers `_generation_finished` through `_schedule`, which calls
    `coroutine.close()` and returns when no event loop is running - it drops the
    work silently rather than raising. This soak is synchronous, so emitting
    `audio_end` directly would never run the undelivered-question recovery, and
    a flow asserting about that recovery would be asserting about nothing.
    """
    import asyncio

    async def turn():
        call.bridge.on_realtime_event(call.sid, Event("audio_end"))
        # Let the scheduled transition run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(turn())


def test_soak_one_hundred_mixed_flows():
    """100 sequential calls across every hazard this phase catalogued.

    Deterministic, offline, no provider and no sleeping. Aggregates are
    asserted to be zero and printed so the run reports real numbers.
    """
    SCENARIOS = (
        "generic", "savings", "current", "slow_id", "history_added",
        "history_updated", "wrong_pin", "invalid_id", "repeated_status",
        "delayed_callback", "duplicate_response", "duplicate_item",
        "tool_continuation", "clarification", "barge_in",
        # The two residual gaps, so neither can come back unnoticed.
        "preface", "clarification_continuation",
        # And the delivery-robustness cases: a cue that produced no audio, a
        # question cut off before the caller heard it, and slow credentials.
        "no_audio", "interrupted_question", "slow_id_delivery",
        "slow_pin_delivery",
        # Phase 7.4F: the bank's own question response, the foreign response
        # racing it, and an owned response that produced nothing.
        "owned_question", "owned_foreign_race", "owned_no_audio",
    )
    totals = {
        "duplicate_id_prompts": 0,
        "duplicate_pin_prompts": 0,
        "duplicate_clarification_prompts": 0,
        "lost_required_prompts": 0,
        "duplicate_business_answers": 0,
        "incorrect_auth_transitions": 0,
        "state_leaks": 0,
        "unexpected_lockouts": 0,
        "orphaned_prompt_ownership": 0,
        # A caller left with no question and no way for the bank to ask again.
        # The worst outcome in this whole phase: not a duplicate, a dead call.
        "permanent_silence": 0,
        # A fresh call that arrived already owning somebody else's question
        # response, which would mean ownership had escaped its session.
        "cross_session_ownership_leaks": 0,
        # A finished waiting period that left an owned response id behind. A
        # stale id keeps refusing every other response, so it mutes the call.
        "orphaned_owned_response_ids": 0,
        "flows": 0,
    }
    failures = []

    for n in range(100):
        scenario = SCENARIOS[n % len(SCENARIOS)]
        call = Heard(f"mix-{n}")
        if call.phase is not None:
            totals["state_leaks"] += 1
        if owned_response_id(call) is not None:
            totals["cross_session_ownership_leaks"] += 1

        say = call.caller
        if scenario == "history_added":
            say = call.caller_via_history_added
        elif scenario == "history_updated":
            say = call.caller_via_history_updated

        opening = {
            "savings": "what is my savings balance",
            "current": "what is my current account balance",
        }.get(scenario, "I want to know my account balance.")
        say(opening)

        if scenario == "preface":
            # A harmless acknowledgement first. It must be heard, and it must
            # not consume the question that follows it.
            if not call.agent(PREFACE):
                totals["lost_required_prompts"] += 1
            call.generation_ended()

        if scenario == "no_audio":
            # The cue reached the model and nothing came back: no audio, no
            # ownership, and the bank believing it has asked. Without recovery
            # this caller waits for ever.
            cue_like_the_pump(call, "CUSTOMER_ID")
            end_turn_in_a_loop(call)
            if question_owed(call) is None:
                totals["permanent_silence"] += 1
            call.agent(ASK_ID)
        elif scenario == "interrupted_question":
            # Generation began and was abandoned before a word reached the
            # line. Ownership must not stay armed, or the retry is withheld.
            cue_like_the_pump(call, "CUSTOMER_ID")
            call.barge_in()
            if call.phase is not None:
                totals["orphaned_prompt_ownership"] += 1
            if not call.agent(ASK_ID):
                totals["permanent_silence"] += 1
        elif scenario == "owned_question":
            owned = own_question_like_the_pump(call, "CUSTOMER_ID", f"own-{n}")
            if not call.agent(ASK_ID, response=owned, item=f"oq-{n}"):
                totals["permanent_silence"] += 1
        elif scenario == "owned_foreign_race":
            # A foreign response tries to ask the same question. If it were
            # admitted the caller would hear the question twice, and the
            # `expected_id` check below catches that through the same machinery
            # every other duplicate scenario uses.
            owned = own_question_like_the_pump(call, "CUSTOMER_ID", f"ownr-{n}")
            call.agent(ASK_ID, response=f"foreign-{n}", item=f"f-{n}")
            if not call.agent(ASK_ID, response=owned, item=f"oq-{n}"):
                totals["permanent_silence"] += 1
        elif scenario == "owned_no_audio":
            # The bank's own response was created and produced nothing. The
            # bounded recovery must give the question back.
            own_question_like_the_pump(call, "CUSTOMER_ID", f"ownn-{n}")
            end_turn_in_a_loop(call)
            if question_owed(call) is None:
                totals["permanent_silence"] += 1
            retry = own_question_like_the_pump(call, "CUSTOMER_ID", f"ownn2-{n}")
            if not call.agent(ASK_ID, response=retry, item=f"oq2-{n}"):
                totals["permanent_silence"] += 1
        else:
            call.agent(ASK_ID)

        if scenario == "slow_id":
            call.speech_starts(); call.agent(ASK_ID)
        elif scenario == "slow_id_delivery":
            # The caller is part-way through saying it, and the model keeps
            # trying. Exactly one question may reach them.
            for _ in range(3):
                call.speech_starts(); call.agent(ASK_ID)
        elif scenario == "repeated_status":
            call.generation_ended()
            call.tool("get_authentication_status"); call.agent(ASK_ID)
            call.tool("get_authentication_status"); call.agent(ASK_ID)
        elif scenario == "delayed_callback":
            call.generation_ended(); call.agent(ASK_ID, response="late", item="late-i")
        elif scenario == "duplicate_response":
            call.agent(ASK_ID, response="dup", item="dup-i")
        elif scenario == "duplicate_item":
            call.agent(ASK_ID, response="r1", item="r1-i2")
        elif scenario == "tool_continuation":
            call.generation_ended(); call.tool("get_authentication_status")
            call.agent(ASK_ID)
        call.generation_ended()

        expected_id = 1
        if scenario == "invalid_id":
            say("I'm not sure")
            call.agent(ASK_ID)
            call.generation_ended()
            expected_id = 2

        say("my customer id is DEMO001")
        submit_customer_id(call.sid, CUSTOMER)
        if call.owed:
            totals["orphaned_prompt_ownership"] += 1

        if not call.agent(ASK_PIN):
            totals["lost_required_prompts"] += 1
        if scenario == "slow_pin_delivery":
            for _ in range(3):
                call.speech_starts(); call.agent(ASK_PIN)
        call.generation_ended()

        expected_pin = 1
        if scenario == "wrong_pin":
            say("my pin is zero zero zero zero")
            if submit_pin(call.sid, WRONG_PIN)["success"] is not False:
                totals["incorrect_auth_transitions"] += 1
            call.agent(ASK_PIN)
            call.generation_ended()
            expected_pin = 2

        say("my pin is four eight two one")
        # The caller has answered, so the waiting period is over and no owned
        # response id may survive it: a stale one refuses everything else.
        if orphaned_owned_correlation(call):
            totals["orphaned_owned_response_ids"] += 1
        verified = submit_pin(call.sid, REAL_PIN)
        if verified.get("authenticated") is not True:
            totals["incorrect_auth_transitions"] += 1
            if verified.get("reason") == "AUTHENTICATION_LOCKED":
                totals["unexpected_lockouts"] += 1

        if scenario in ("clarification", "clarification_continuation"):
            # A real clarification, opened the way the tool layer opens one and
            # marked asked the way Phase 7.4C marks it, so the boundary is
            # reading backend state rather than a sentence this test made up.
            session = session_manager.get_session(call.sid)
            pending_clarification.open_for(
                session,
                tool="get_account_balance",
                reason=pending_clarification.ACCOUNT_TYPE_REQUIRED,
                choices=("Savings", "Current"),
                manager=session_manager,
            )
            pending_clarification.mark_question_asked(
                session, manager=session_manager
            )

            if not call.agent(CLARIFY):
                totals["lost_required_prompts"] += 1
            call.generation_ended()

            if scenario == "clarification_continuation":
                call.tool("get_account_balance")
                call.agent(CLARIFY)          # must not reach the caller again

            say("Savings")
            pending_clarification.complete_with(
                session_manager.get_session(call.sid),
                pending_clarification.Domain.ACCOUNT,
                "Savings",
                manager=session_manager,
            )
            if call.heard(CLARIFY) != 1:
                totals["duplicate_clarification_prompts"] += 1

        answer = CURRENT_ANSWER if scenario == "current" else ANSWER
        if not call.agent(answer):
            totals["lost_required_prompts"] += 1
        if scenario == "barge_in":
            call.barge_in()
            say("sorry, again")
            if not call.agent(RECOVERY):
                totals["lost_required_prompts"] += 1

        if call.heard(ASK_ID) != expected_id:
            totals["duplicate_id_prompts"] += 1
            failures.append((n, scenario, "ID", call.heard(ASK_ID), expected_id))
        if call.heard(ASK_PIN) != expected_pin:
            totals["duplicate_pin_prompts"] += 1
            failures.append((n, scenario, "PIN", call.heard(ASK_PIN), expected_pin))
        if call.heard(answer) != 1:
            totals["duplicate_business_answers"] += 1
            failures.append((n, scenario, "ANSWER", call.heard(answer), 1))

        totals["flows"] += 1
        session_manager.destroy_session(call.sid)

    print("\n7.4E mixed soak aggregate:", totals)

    assert totals["flows"] == 100
    for key in (
        "duplicate_id_prompts", "duplicate_pin_prompts",
        "duplicate_clarification_prompts", "lost_required_prompts",
        "duplicate_business_answers", "incorrect_auth_transitions", "state_leaks",
        "unexpected_lockouts", "orphaned_prompt_ownership",
        # Asserted, not merely printed. Counting a failure mode and then leaving
        # it out of this tuple is how a soak reports a reassuring zero that was
        # never measured.
        "permanent_silence",
        "cross_session_ownership_leaks",
        "orphaned_owned_response_ids",
    ):
        assert totals[key] == 0, f"{key}={totals[key]} first: {failures[:5]}"
