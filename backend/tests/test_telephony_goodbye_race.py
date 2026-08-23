"""Phase 6.4: the caller's goodbye that arrives after the goodbye was spoken.

Phase 6.3 made the caller's END_CALL intent arm closure, and the call then ends
through the ordinary completion path — generation ends, playback drains, the
lifecycle hangs up. That works whenever the intent is known before the assistant
replies.

It is not always known first. Transcription is a separate pass, and the model
answers from the audio without waiting for it, so the completed transcript can
arrive *after* the reply it prompted has finished playing. By then generation has
ended and the queue has drained; nothing further will happen on that call. The
arming lands in CLOSING with no event left to complete it, and the line stays up
until the caller hangs up themselves.

These tests pin the ordering down explicitly. They drive the lifecycle directly,
because the point is which transitions arrive in which order, and a fake model
session would only obscure that.
"""

import asyncio

import pytest

from app.telephony.lifecycle import CallLifecycle, CallState, EndReason


def run(coro):
    return asyncio.run(coro)


def build(*, silence=30.0):
    """A lifecycle with its endings recorded and no timer in the way."""
    ended = []

    async def speak():
        return None

    async def hang_up(reason):
        ended.append(reason)

    return CallLifecycle("race", speak=speak, hang_up=hang_up, silence_seconds=silence), ended


async def a_completed_turn(lifecycle):
    """One assistant reply, generated and heard in full."""
    await lifecycle.on_assistant_audio()
    await lifecycle.on_generation_ended()
    await lifecycle.on_playback_drained()


# === 1: the race, reproduced ================================================


def test_a_late_end_call_after_a_completed_reply_still_ends_the_call():
    """The regression. Fails before the fix with the call left in CLOSING.

    The exact live ordering: the caller says "that's all, thank you, goodbye",
    the assistant answers and finishes, the queue drains — and only then does
    the transcript arrive and classify as END_CALL. There is no later
    `on_generation_ended` or `on_playback_drained` to complete the closure.
    """

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()   # the caller starts talking
        await a_completed_turn(lifecycle)            # the goodbye is spoken in full
        assert lifecycle.state is CallState.WAITING_FOR_CALLER

        await lifecycle.arm_goodbye()                # the transcript, late
        await asyncio.sleep(0.05)

        state = lifecycle.state
        await lifecycle.close()
        return ended, state

    ended, state = run(scenario())

    assert ended == [EndReason.CALLER_GOODBYE], (
        "the call was left in %s with nothing left to complete it" % state
    )
    assert state is CallState.CLOSED


def test_the_late_transcript_event_does_not_undo_the_completed_reply():
    """The live ordering in full, including the event that used to spoil it.

    Transcription completing is itself a turn boundary and lands on
    `on_caller_speech_started`. If that were treated as the caller starting to
    talk, it would mark a reply owed again — moments after the reply had been
    delivered — and the arming that follows would wait for a completion that
    has already happened.
    """

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        # The transcription pass finishes late, after the answer it prompted.
        await lifecycle.on_caller_speech_started(speech_started=False)
        await lifecycle.arm_goodbye()
        await asyncio.sleep(0.05)

        state = lifecycle.state
        await lifecycle.close()
        return ended, state

    ended, state = run(scenario())

    assert ended == [EndReason.CALLER_GOODBYE]
    assert state is CallState.CLOSED


def test_a_late_end_call_hangs_up_exactly_once():
    """Two endings must not both fire. Arming twice is one hang-up."""

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        await lifecycle.arm_goodbye()
        await lifecycle.arm_goodbye()
        await lifecycle.on_playback_drained()
        await lifecycle.on_generation_ended()
        await asyncio.sleep(0.05)
        await lifecycle.close()
        return ended

    assert run(scenario()) == [EndReason.CALLER_GOODBYE]


# === 3: everything that must still wait =====================================


def test_an_end_call_before_the_reply_still_waits_for_the_goodbye_audio():
    """The ordinary case, unchanged. The caller is owed their goodbye."""

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await lifecycle.arm_goodbye()          # the transcript, in good time
        await asyncio.sleep(0.05)
        premature = list(ended)

        await lifecycle.on_assistant_audio()   # now the goodbye is spoken
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return premature, ended, lifecycle.state

    premature, ended, state = run(scenario())

    assert premature == [], "hung up before the goodbye was spoken"
    assert ended == [EndReason.CALLER_GOODBYE]
    assert state is CallState.CLOSED


def test_a_goodbye_after_the_greeting_waits_for_the_reply():
    """The regression the new flag could most easily have caused.

    After the greeting has played, a reply is complete — and if that were still
    standing when the caller says goodbye, the call would close on the spot and
    the bank's closing line would never be spoken at all.
    """

    async def scenario():
        lifecycle, ended = build()
        await a_completed_turn(lifecycle)          # the greeting, in full

        await lifecycle.on_caller_speech_started()  # "that's all, goodbye"
        await lifecycle.arm_goodbye()
        await asyncio.sleep(0.05)
        premature = list(ended)

        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return premature, ended

    premature, ended = run(scenario())

    assert premature == [], "closed before the bank had said goodbye"
    assert ended == [EndReason.CALLER_GOODBYE]


def test_arming_while_the_reply_is_still_generating_waits():
    """Audio has started but the model has not finished. Nothing is cut off."""

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await lifecycle.on_assistant_audio()       # generating
        await lifecycle.arm_goodbye()
        await asyncio.sleep(0.05)
        mid_generation = list(ended)

        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return mid_generation, ended, lifecycle.state

    mid_generation, ended, state = run(scenario())

    assert mid_generation == [], "cut off audio that was still being generated"
    assert ended == [EndReason.CALLER_GOODBYE]
    assert state is CallState.CLOSED


def test_arming_while_audio_is_still_queued_waits():
    """Generation finished, playback has not. Queued audio must still play."""

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()      # generated, not yet heard
        await lifecycle.arm_goodbye()
        await asyncio.sleep(0.05)
        still_queued = list(ended)

        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return still_queued, ended

    still_queued, ended = run(scenario())

    assert still_queued == [], "hung up with the goodbye still queued"
    assert ended == [EndReason.CALLER_GOODBYE]


def test_generation_end_alone_never_hangs_up():
    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await lifecycle.arm_goodbye()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await asyncio.sleep(0.05)
        await lifecycle.close()
        return ended

    assert run(scenario()) == []


def test_playback_drain_alone_never_hangs_up():
    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await lifecycle.arm_goodbye()
        await lifecycle.on_assistant_audio()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        await lifecycle.close()
        return ended

    assert run(scenario()) == []


def test_a_barge_in_before_the_goodbye_still_waits_for_the_reply():
    """The caller cuts in to say they are finished. A reply is owed again."""

    async def scenario():
        lifecycle, ended = build()
        await a_completed_turn(lifecycle)

        await lifecycle.on_assistant_audio()
        await lifecycle.on_caller_speech_started()   # barge-in
        await lifecycle.on_assistant_interrupted()
        await lifecycle.arm_goodbye()
        await asyncio.sleep(0.05)
        premature = list(ended)

        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return premature, ended

    premature, ended = run(scenario())

    assert premature == [], "barge-in turned into an immediate hang-up"
    assert ended == [EndReason.CALLER_GOODBYE]


# === the endings that already worked ========================================


def test_the_silence_path_is_unaffected():
    """Ten seconds of nothing still closes as CALLER_SILENT, after the line."""

    async def scenario():
        lifecycle, ended = build(silence=0.05)
        spoken = []

        async def speak():
            spoken.append(1)

        lifecycle._speak = speak

        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)           # back to waiting
        await asyncio.sleep(0.2)                    # the timer fires
        prompted = lifecycle.silence_prompts
        mid = list(ended)

        # The closing line plays out in full before anything hangs up.
        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return prompted, mid, ended, spoken

    prompted, mid, ended, spoken = run(scenario())

    assert prompted == 1
    assert mid == [], "the silence line was cut off"
    assert ended == [EndReason.CALLER_SILENT]
    assert len(spoken) == 1


def test_the_silence_closing_line_is_not_truncated_by_the_new_flag():
    """A silent close must still wait for generation *and* playback."""

    async def scenario():
        lifecycle, ended = build(silence=0.05)
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)
        await asyncio.sleep(0.2)

        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await asyncio.sleep(0.05)
        before_drain = list(ended)

        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return before_drain, ended

    before_drain, ended = run(scenario())

    assert before_drain == [], "hung up before the silence line finished playing"
    assert ended == [EndReason.CALLER_SILENT]


def test_a_manual_disconnect_after_a_late_arm_stays_idempotent():
    """Both endings racing. Whichever wins, the call ends exactly once."""

    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        await lifecycle.arm_goodbye()          # closes immediately
        await lifecycle.on_caller_disconnected()
        await lifecycle.on_caller_disconnected()
        await asyncio.sleep(0.05)
        return ended

    assert run(scenario()) == [EndReason.CALLER_GOODBYE]


def test_a_caller_who_hangs_up_first_is_unaffected():
    async def scenario():
        lifecycle, ended = build()
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)
        await lifecycle.on_caller_disconnected()
        await asyncio.sleep(0.05)
        return ended, lifecycle.state

    ended, state = run(scenario())

    assert ended == [EndReason.CALLER_DISCONNECTED]
    assert state is CallState.CLOSED


def test_two_calls_keep_their_closing_state_separate():
    """No shared state. One caller leaving late must not touch the other."""

    async def scenario():
        leaving, leaving_ended = build()
        staying, staying_ended = build()

        for lifecycle in (leaving, staying):
            await lifecycle.on_caller_speech_started()
            await a_completed_turn(lifecycle)

        await leaving.arm_goodbye()
        await asyncio.sleep(0.05)
        result = (leaving_ended, staying_ended, staying.state)
        await staying.close()
        return result

    leaving_ended, staying_ended, staying_state = run(scenario())

    assert leaving_ended == [EndReason.CALLER_GOODBYE]
    assert staying_ended == [], "one call's goodbye ended another"
    assert staying_state is CallState.WAITING_FOR_CALLER


# === a transcript is not a voice ============================================
#
# Transcription completing is a turn boundary, and it is where the caller's
# words become readable. It is not the caller starting to talk — that already
# happened, and for a late transcript it happened long enough ago that the reply
# has been delivered.
#
# The distinction matters in both directions. Treated as a fresh speech start, a
# late *ordinary* transcript cancels the silence timer and moves a waiting call
# into CALLER_SPEAKING — waiting for a turn that has already been taken and an
# answer that has already been given. Nothing further arrives, and the one thing
# that would have rescued the call, the silence timeout, has just been switched
# off. The caller sits on an open line for ever.


def test_a_late_ordinary_transcript_does_not_cancel_the_silence_timer():
    """The regression this rule exists to prevent.

    An ordinary question, answered in full. The transcript arrives afterwards
    and classifies as nothing in particular. The call must stay exactly as it
    was — waiting for the caller, with the timer still running.
    """

    async def scenario():
        lifecycle, ended = build(silence=0.05)
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)
        assert lifecycle.state is CallState.WAITING_FOR_CALLER

        # The transcription pass finishes. No new speech has started.
        await lifecycle.on_caller_speech_started(speech_started=False)
        state_after = lifecycle.state
        timer_running = lifecycle._silence_task is not None

        await asyncio.sleep(0.2)
        return state_after, timer_running, lifecycle.silence_prompts

    state_after, timer_running, prompted = run(scenario())

    assert state_after is CallState.WAITING_FOR_CALLER, (
        "a late transcript moved the call to %s" % state_after
    )
    assert timer_running is True, "a late transcript cancelled the silence timer"
    assert prompted == 1, "the silence timer never fired; the call was stranded"


def test_a_late_ordinary_transcript_does_not_strand_the_call_in_caller_speaking():
    """CALLER_SPEAKING is a claim about the caller, and it would be false."""

    async def scenario():
        lifecycle, ended = build(silence=30.0)
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        await lifecycle.on_caller_speech_started(speech_started=False)
        state = lifecycle.state
        await lifecycle.close()
        return state

    assert run(scenario()) is CallState.WAITING_FOR_CALLER


def test_a_late_ordinary_transcript_still_reaches_caller_silent():
    """End to end: the existing silence close must survive a late transcript."""

    async def scenario():
        lifecycle, ended = build(silence=0.05)
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        await lifecycle.on_caller_speech_started(speech_started=False)
        await asyncio.sleep(0.2)          # the timer fires and the line is asked for

        # The closing line plays out, and only then does the call end.
        await lifecycle.on_assistant_audio()
        await lifecycle.on_generation_ended()
        await lifecycle.on_playback_drained()
        await asyncio.sleep(0.05)
        return ended, lifecycle.state

    ended, state = run(scenario())

    assert ended == [EndReason.CALLER_SILENT], (
        "the call never reached the silence timeout: %s" % ended
    )
    assert state is CallState.CLOSED


def test_a_real_speech_start_still_cancels_the_silence_timer():
    """The behaviour that must be preserved: a voice does stop the wait."""

    async def scenario():
        lifecycle, ended = build(silence=0.05)
        await lifecycle.on_caller_speech_started()
        await a_completed_turn(lifecycle)

        await lifecycle.on_caller_speech_started()     # the caller speaks again
        state = lifecycle.state
        timer_running = lifecycle._silence_task is not None

        await asyncio.sleep(0.2)
        prompted = lifecycle.silence_prompts
        await lifecycle.close()
        return state, timer_running, prompted

    state, timer_running, prompted = run(scenario())

    assert state is CallState.CALLER_SPEAKING
    assert timer_running is False, "the wait was not cancelled by real speech"
    assert prompted == 0, "prompted a caller who was talking"


def test_a_late_transcript_does_not_reopen_a_closing_call():
    """The CLOSING guard still holds for the transcript path."""

    async def scenario():
        lifecycle, ended = build(silence=30.0)
        await lifecycle.on_caller_speech_started()
        await lifecycle.arm_goodbye()                  # armed, reply owed
        assert lifecycle.state is CallState.CLOSING

        await lifecycle.on_caller_speech_started(speech_started=False)
        state = lifecycle.state
        await lifecycle.close()
        return state

    assert run(scenario()) is CallState.CLOSING
