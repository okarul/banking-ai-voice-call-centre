"""Audio conversion, and the queues that stop one caller exhausting memory.

Two things are being defended.

The first is that the conversion is *correct*, not merely symmetric. A codec
that decodes its own output perfectly and disagrees with the rest of the world
produces a call that connects, reports no error, and sounds like static — so
the encoder and decoder are checked against `audioop`'s reference G.711 rather
than against each other.

The second is that audio queues are bounded. A telephone sends fifty frames a
second whether or not anything is reading them, so an unbounded queue is a way
for one caller to take down the process.
"""

import array
import asyncio
import math
import warnings

import pytest

from app.telephony import audio
from app.telephony.media import BoundedAudioQueue, LoopbackMediaTransport


def tone(*, samples: int = 800, hz: int = 440, rate: int = 8000) -> bytes:
    """A sine wave as little-endian PCM16 — something with structure to lose."""
    return array.array(
        "h", [int(12000 * math.sin(2 * math.pi * hz * n / rate)) for n in range(samples)]
    ).tobytes()


def reference():
    """`audioop`, deprecated but still the reference implementation here."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop

    return audioop


# === G.711, against the reference ===========================================


def test_the_encoder_matches_the_reference_byte_for_byte():
    """Not "close enough": µ-law is exactly specified."""
    pcm = tone()

    assert audio.pcm16_to_ulaw(pcm) == reference().lin2ulaw(pcm, 2)


def test_the_decoder_matches_the_reference_byte_for_byte():
    encoded = audio.pcm16_to_ulaw(tone())

    assert audio.ulaw_to_pcm16(encoded) == reference().ulaw2lin(encoded, 2)


def test_every_possible_byte_decodes_the_same_as_the_reference():
    """All 256, because the edges are where a table is wrong."""
    every_byte = bytes(range(256))

    assert audio.ulaw_to_pcm16(every_byte) == reference().ulaw2lin(every_byte, 2)


def test_a_round_trip_only_loses_quantisation():
    pcm = tone()
    restored = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm))

    original = array.array("h")
    original.frombytes(pcm)
    result = array.array("h")
    result.frombytes(restored)

    worst = max(abs(a - b) for a, b in zip(original, result))
    # µ-law is lossy by design; the error should be a few percent, not a signal.
    assert worst < 0.05 * 12000


def test_silence_and_extremes_survive():
    for sample in (0, 32767, -32768, 1, -1):
        pcm = array.array("h", [sample] * 8).tobytes()
        assert audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm)) is not None


def test_an_empty_payload_is_not_an_error():
    assert audio.pcm16_to_ulaw(b"") == b""
    assert audio.ulaw_to_pcm16(b"") == b""


def test_an_odd_length_pcm_buffer_does_not_raise():
    """A truncated frame is a network event, not a reason to drop a call."""
    assert audio.pcm16_to_ulaw(tone()[:101]) is not None


# === sample rate ============================================================


def test_a_telephone_frame_becomes_the_right_amount_of_model_audio():
    """20 ms is 20 ms: 160 µ-law bytes in, 960 PCM16 bytes out at 24 kHz."""
    frame = audio.pcm16_to_ulaw(tone(samples=audio.ULAW_FRAME_BYTES))
    assert len(frame) == 160

    converted = audio.telephony_to_model(frame)

    # 160 samples at 8k -> 480 samples at 24k -> 960 bytes.
    assert len(converted) == 960


def test_model_audio_becomes_the_right_amount_of_telephone_audio():
    model_audio = tone(samples=480, rate=24000)

    converted = audio.model_to_telephony(model_audio)

    assert len(converted) == 160


def test_a_full_round_trip_preserves_duration():
    frame = audio.pcm16_to_ulaw(tone(samples=160))

    assert len(audio.model_to_telephony(audio.telephony_to_model(frame))) == len(frame)


def test_upsampling_interpolates_rather_than_repeating():
    """A stepped waveform is a waveform with harmonics that were not spoken."""
    pcm = array.array("h", [0, 300]).tobytes()

    result = array.array("h")
    result.frombytes(audio.resample_pcm16(pcm, source_rate=8000, target_rate=24000))

    assert list(result)[:3] == [0, 100, 200]


def test_downsampling_averages_rather_than_decimating():
    """Plain decimation folds everything above 4 kHz back in as aliasing."""
    pcm = array.array("h", [0, 300, 600]).tobytes()

    result = array.array("h")
    result.frombytes(audio.resample_pcm16(pcm, source_rate=24000, target_rate=8000))

    assert list(result) == [300]


def test_resampling_to_the_same_rate_changes_nothing():
    pcm = tone()

    assert audio.resample_pcm16(pcm, source_rate=8000, target_rate=8000) == pcm


def test_conversion_keeps_the_signal_recognisable():
    """End to end: a 440 Hz tone is still a 440 Hz tone after the round trip.

    Measured by zero crossings, which is crude but catches the failures that
    matter — a byte-swap, a wrong sample rate, or a broken encoder all destroy
    the crossing count.
    """
    frames = b"".join(
        audio.pcm16_to_ulaw(tone(samples=160, hz=440)) for _ in range(10)
    )
    restored = audio.model_to_telephony(audio.telephony_to_model(frames))

    def crossings(pcm_bytes):
        samples = array.array("h")
        samples.frombytes(audio.ulaw_to_pcm16(pcm_bytes))
        return sum(
            1
            for a, b in zip(samples, samples[1:])
            if (a >= 0) != (b >= 0)
        )

    before = crossings(frames)
    after = crossings(restored)
    assert abs(before - after) <= before * 0.1, (before, after)


# === bounded queues =========================================================


def test_a_queue_never_grows_past_its_ceiling():
    queue = BoundedAudioQueue(max_frames=10)

    for index in range(500):
        queue.put(bytes([index % 256]))

    assert len(queue) == 10
    assert queue.dropped == 490


def test_a_full_queue_drops_the_oldest_not_the_newest():
    """Dropping the newest would grow the delay instead of the loss."""
    queue = BoundedAudioQueue(max_frames=3)

    for frame in (b"1", b"2", b"3", b"4", b"5"):
        queue.put(frame)

    async def drain():
        return [await queue.get() for _ in range(3)]

    assert asyncio.run(drain()) == [b"3", b"4", b"5"]


def test_a_reader_waiting_on_an_empty_queue_is_woken_by_a_frame():
    async def scenario():
        queue = BoundedAudioQueue(max_frames=4)
        reader = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        queue.put(b"hello")
        return await asyncio.wait_for(reader, timeout=1)

    assert asyncio.run(scenario()) == b"hello"


def test_closing_a_queue_releases_a_waiting_reader():
    """Otherwise a cleanup would hang on a pump that never returns."""

    async def scenario():
        queue = BoundedAudioQueue(max_frames=4)
        reader = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        queue.close()
        return await asyncio.wait_for(reader, timeout=1)

    assert asyncio.run(scenario()) is None


def test_a_closed_queue_accepts_nothing_further():
    queue = BoundedAudioQueue(max_frames=4)
    queue.close()
    queue.put(b"late")

    assert len(queue) == 0


def test_clearing_a_queue_leaves_it_usable():
    """Barge-in empties the queue; it must not end the call."""
    queue = BoundedAudioQueue(max_frames=4)
    queue.put(b"stale")
    queue.clear()
    queue.put(b"fresh")

    assert asyncio.run(queue.get()) == b"fresh"


def test_the_loopback_transport_carries_frames_in_order():
    async def scenario():
        transport = LoopbackMediaTransport()
        await transport.on_call_started()
        transport.feed(b"one")
        transport.feed(b"two")
        transport.hang_up()
        return [await transport.receive_audio() for _ in range(3)]

    assert asyncio.run(scenario()) == [b"one", b"two", None]


@pytest.mark.parametrize("frames", [1, 50, 200])
def test_queue_bounds_are_configurable(frames):
    queue = BoundedAudioQueue(max_frames=frames)
    for _ in range(frames + 25):
        queue.put(b"x")

    assert len(queue) == frames
