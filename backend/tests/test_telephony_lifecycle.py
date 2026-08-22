"""Phase 6: when a telephone call speaks, listens, waits and ends.

Channel 1 makes these decisions in the browser page. A telephone has no page,
so Channel 2 makes them here, and these tests are the specification:

* silence is measured from **conversation state**, never from packet absence
* the ten-second wait starts only when the assistant has finished speaking
* a caller's voice cancels it immediately
* a closing line is played **to completion** before anything is torn down
* there is **no** maximum call duration
* every timer and every piece of state belongs to one call

The clock is compressed — a 0.05 s window instead of ten — because what is
under test is the ordering of transitions, not Python's ability to sleep. The
real window is asserted separately as a constant.
"""

import asyncio

import pytest

from app.agents import speech
from app.telephony.lifecycle import (
    SILENCE_SECONDS,
    CallLifecycle,
    CallState,
    EndReason,
)

FAST = 0.05


class Recorder:
    """Captures what the lifecycle asked its owner to do."""

    def __init__(self, *, speak_fails: bool = False) -> None:
        self.spoke = 0
        self.ended: list[EndReason] = []
        self._speak_fails = speak_fails

    async def speak(self) -> None:
        if self._speak_fails:
            raise RuntimeError("session gone")
        self.spoke += 1

    async def hang_up(self, reason: EndReason) -> None:
        self.ended.append(reason)


def build(*, silence: float = FAST, speak_fails: bool = False):
    recorder = Recorder(speak_fails=speak_fails)
    lifecycle = CallLifecycle(
        "call-1",
        speak=recorder.speak,
        hang_up=recorder.hang_up,
        silence_seconds=silence,
    )
    return lifecycle, recorder


def run(coro):
    return asyncio.run(coro)


async def finish_speaking(lifecycle: CallLifecycle) -> None:
    """The assistant produced audio, generation ended, and the queue drained."""
    await lifecycle.on_assistant_audio()
    await lifecycle.on_generation_ended()
    await lifecycle.on_playback_drained()


# === the canonical lines ====================================================


def test_the_three_canonical_lines_are_exact():
    """Each is quoted verbatim in the brief and listened for by the line."""
    assert speech.GOODBYE_SPEECH == (
        "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."
    )
    assert speech.YOU_ARE_WELCOME_SPEECH == (
        "You're most welcome. Is there anything else I can help you with today?"
    )
    assert speech.SILENCE_CLOSING_SPEECH == (
        "I do not hear anything from you. Thank you."
    )


def test_the_model_is_instructed_to_say_them_verbatim():
    """A paraphrased closing line is a call that never hangs up."""
    from app.realtime.banking_realtime import INSTRUCTIONS

    assert speech.GOODBYE_SPEECH in INSTRUCTIONS
    assert speech.YOU_ARE_WELCOME_SPEECH in INSTRUCTIONS
    assert speech.SILENCE_CLOSING_SPEECH in INSTRUCTIONS
    assert "word for word" in INSTRUCTIONS


def test_a_courtesy_is_not_a_goodbye():
    """"Thank you for your service" must leave the call open."""
    assert speech.is_closing_line(speech.YOU_ARE_WELCOME_SPEECH) is False
    assert speech.is_closing_line("Thank you for your service.") is False
    assert speech.is_closing_line("Thank you.") is False


def test_the_closing_line_is_recognised():
    assert speech.is_closing_line(speech.GOODBYE_SPEECH) is True


def test_the_greeting_is_not_mistaken_for_a_closing_line():
    """The opening says "thank you for calling" and must not end the call."""
    assert speech.is_closing_line(speech.WELCOME_SPEECH) is False


def test_a_banking_answer_is_not_a_closing_line():
    for answer in (
        "Your savings balance is 12,450.75 SGD.",
        "Your next instalment is 1,985.40 SGD on 5 September.",
        "Thank you. Your identity has been verified.",
    ):
        assert speech.is_closing_line(answer) is False, answer


# === the silence window =====================================================


def test_the_configured_window_is_ten_seconds():
    assert SILENCE_SECONDS == 10.0


def test_no_wait_is_armed_while_the_assistant_is_speaking():
    """A caller listening to the bank is not a silent caller."""

    async def scenario():
        lifecycle, recorder = build()
        await lifecycle.on_assistant_audio()
        await asyncio.sleep(FAST * 4)
        return lifecycle.state, recorder.spoke, recorder.ended

    state, spoke, ended = run(scenario())

    assert state is CallState.ASSISTANT_SPEAKING
    assert spoke == 0
    assert ended == []


def test_the_wait_begins_only_once_playback_has_finished():
    async def scenario():
        lifecycle, _ = build()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        before = lifecycle.state
        await lifecycle.on_playback_drained()
        return before, lifecycle.state

    before, after = run(scenario())

    # Generation ending is not the caller having heard it.
    assert before is CallState.ASSISTANT_SPEAKING
    assert after is CallState.WAITING_FOR_CALLER


def test_a_silent_caller_is_told_once_and_the_call_ends():
    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await asyncio.sleep(FAST * 3)
        # The closing line plays out, and only then does the call end.
        during = list(recorder.ended)
        await finish_speaking(lifecycle)
        return recorder, during

    recorder, during = run(scenario())

    assert recorder.spoke == 1, "the caller should be told exactly once"
    assert during == [], "the call ended before the line finished playing"
    assert recorder.ended == [EndReason.CALLER_SILENT]


def test_there_is_no_second_wait_after_the_silence_line():
    """Channel 1 asks again; Channel 2 says its line and goes."""

    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await asyncio.sleep(FAST * 3)
        await finish_speaking(lifecycle)          # the closing line plays out
        await asyncio.sleep(FAST * 6)             # far longer than a second wait
        return recorder.spoke, recorder.ended, lifecycle.state

    spoke, ended, state = run(scenario())

    assert spoke == 1
    assert ended == [EndReason.CALLER_SILENT]
    assert state is CallState.CLOSED


def test_a_caller_speaking_cancels_the_wait_immediately():
    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await asyncio.sleep(FAST / 2)
        await lifecycle.on_caller_speech_started()
        await asyncio.sleep(FAST * 5)
        return lifecycle.state, recorder.spoke, recorder.ended

    state, spoke, ended = run(scenario())

    assert state is CallState.CALLER_SPEAKING
    assert spoke == 0, "a caller who spoke was told they were silent"
    assert ended == []


def test_the_wait_is_rearmed_after_each_assistant_turn():
    """Silence is per turn, not once per call."""

    async def scenario():
        lifecycle, recorder = build()
        for _ in range(3):
            await finish_speaking(lifecycle)
            await asyncio.sleep(FAST / 3)
            await lifecycle.on_caller_speech_started()
        return lifecycle.turns_completed, recorder.spoke

    turns, spoke = run(scenario())

    assert turns == 3
    assert spoke == 0


def test_barge_in_cancels_the_wait_and_stops_the_turn():
    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await lifecycle.on_assistant_interrupted()
        await asyncio.sleep(FAST * 4)
        return lifecycle.state, recorder.spoke

    state, spoke = run(scenario())

    assert state is CallState.CALLER_SPEAKING
    assert spoke == 0


# === closing ================================================================


def test_a_goodbye_does_not_hang_up_until_the_line_has_played():
    """Hanging up on the closing line truncates the sentence that matters."""

    async def scenario():
        lifecycle, recorder = build()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_goodbye_spoken()
        during = (lifecycle.state, list(recorder.ended))
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        return during, recorder.ended

    (state_during, ended_during), ended_after = run(scenario())

    assert state_during is CallState.CLOSING
    assert ended_during == [], "hung up before the goodbye finished playing"
    assert ended_after == [EndReason.CALLER_GOODBYE]


def test_a_caller_speaking_over_the_closing_line_does_not_reopen_the_call():
    """Otherwise the bank has said goodbye and nothing ever ends the call."""

    async def scenario():
        lifecycle, recorder = build()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_goodbye_spoken()
        await lifecycle.on_caller_speech_started()
        state = lifecycle.state
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        return state, recorder.ended

    state, ended = run(scenario())

    assert state is CallState.CLOSING
    assert ended == [EndReason.CALLER_GOODBYE]


def test_a_goodbye_is_acted_on_once():
    async def scenario():
        lifecycle, recorder = build()
        await lifecycle.on_assistant_audio()
        for _ in range(4):
            await lifecycle.on_goodbye_spoken()
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        return recorder.ended

    assert run(scenario()) == [EndReason.CALLER_GOODBYE]


def test_a_caller_hanging_up_ends_the_call_at_once():
    """Nothing to play out to somebody who has gone."""

    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await lifecycle.on_caller_disconnected()
        await asyncio.sleep(FAST * 4)
        return lifecycle.state, recorder.ended, recorder.spoke

    state, ended, spoke = run(scenario())

    assert state is CallState.CLOSED
    assert ended == [EndReason.CALLER_DISCONNECTED]
    assert spoke == 0


def test_a_closing_line_that_cannot_be_delivered_still_ends_the_call():
    """Otherwise the call waits for a line that will never play."""

    async def scenario():
        lifecycle, recorder = build(speak_fails=True)
        await finish_speaking(lifecycle)
        await asyncio.sleep(FAST * 4)
        return lifecycle.state, recorder.ended

    state, ended = run(scenario())

    assert state is CallState.CLOSED
    assert ended == [EndReason.CALLER_SILENT]


def test_ending_twice_hangs_up_once():
    async def scenario():
        lifecycle, recorder = build()
        await lifecycle.on_caller_disconnected()
        await lifecycle.on_caller_disconnected()
        await finish_speaking(lifecycle)
        return recorder.ended

    assert run(scenario()) == [EndReason.CALLER_DISCONNECTED]


def test_closing_the_lifecycle_cancels_the_wait_without_hanging_up():
    """Used when something else is tearing the call down."""

    async def scenario():
        lifecycle, recorder = build()
        await finish_speaking(lifecycle)
        await lifecycle.close()
        await asyncio.sleep(FAST * 4)
        return lifecycle.closed, recorder.spoke, recorder.ended

    closed, spoke, ended = run(scenario())

    assert closed is True
    assert spoke == 0
    assert ended == []


# === no maximum duration ====================================================


def test_there_is_no_maximum_call_duration():
    """A bank does not hang up on a customer who is still transacting.

    Channel 1 caps a browser call at fifteen minutes. Channel 2 must not, and
    this asserts no such timer exists rather than trusting that none was added.
    """
    import inspect

    from app.telephony import bridge, lifecycle, service

    for module in (lifecycle, bridge, service):
        source = inspect.getsource(module)
        for capped in ("MAX_CALL", "MAX_DURATION", "max_call_seconds", "15 * 60"):
            assert capped not in source, f"{module.__name__} caps call duration"


def test_a_long_active_call_is_never_ended_by_the_lifecycle():
    """Many turns, far longer than any browser cap, and still open."""

    async def scenario():
        lifecycle, recorder = build()
        for _ in range(50):
            await finish_speaking(lifecycle)
            await lifecycle.on_caller_speech_started()
        return lifecycle.state, lifecycle.turns_completed, recorder.ended

    state, turns, ended = run(scenario())

    assert state is CallState.CALLER_SPEAKING
    assert turns == 50
    assert ended == []


# === concurrency safety =====================================================


def test_two_calls_keep_separate_state_and_separate_timers():
    """The property Phase 7 will lean on: nothing here is shared."""

    async def scenario():
        first_recorder = Recorder()
        second_recorder = Recorder()
        first = CallLifecycle(
            "call-a", speak=first_recorder.speak,
            hang_up=first_recorder.hang_up, silence_seconds=FAST,
        )
        second = CallLifecycle(
            "call-b", speak=second_recorder.speak,
            hang_up=second_recorder.hang_up, silence_seconds=FAST * 20,
        )

        await finish_speaking(first)
        await finish_speaking(second)
        await asyncio.sleep(FAST * 3)

        # The first has timed out; the second is still waiting.
        state = (first.state, second.state, first_recorder.spoke, second_recorder.spoke)
        await second.close()
        return state

    first_state, second_state, first_spoke, second_spoke = run(scenario())

    assert first_state is CallState.CLOSING
    assert second_state is CallState.WAITING_FOR_CALLER
    assert first_spoke == 1
    assert second_spoke == 0


def test_five_concurrent_calls_time_out_independently():
    async def scenario():
        recorders = [Recorder() for _ in range(5)]
        calls = [
            CallLifecycle(
                f"call-{n}", speak=r.speak, hang_up=r.hang_up, silence_seconds=FAST
            )
            for n, r in enumerate(recorders)
        ]
        # Two callers speak; three go quiet.
        await asyncio.gather(*(finish_speaking(call) for call in calls))
        await calls[1].on_caller_speech_started()
        await calls[3].on_caller_speech_started()
        await asyncio.sleep(FAST * 4)
        return [r.spoke for r in recorders], [call.state for call in calls]

    spoke, states = run(scenario())

    assert spoke == [1, 0, 1, 0, 1], "a silent caller's timer fired on a talking one"
    assert states[1] is CallState.CALLER_SPEAKING
    assert states[3] is CallState.CALLER_SPEAKING


def test_no_module_level_call_state_exists():
    """Per call, not per process."""
    import app.telephony.lifecycle as module

    for name, value in vars(module).items():
        if name.startswith("_") or name.isupper():
            continue
        assert not isinstance(value, (dict, list, set)), f"{name} is shared state"


def test_the_lifecycle_description_carries_no_speech_or_identity():
    async def scenario():
        lifecycle, _ = build()
        await finish_speaking(lifecycle)
        described = lifecycle.describe()
        await lifecycle.close()
        return described

    described = run(scenario())

    assert described["state"] == CallState.WAITING_FOR_CALLER.value
    for forbidden in ("customer_id", "transcript", "audio", "pin", "text"):
        assert forbidden not in described, forbidden
