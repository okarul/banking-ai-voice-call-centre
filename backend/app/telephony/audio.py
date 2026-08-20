"""Converting between what a telephone carries and what the model expects.

These are not the same thing, and assuming they were would produce a call that
connects perfectly and sounds like static.

    telephone   G.711 µ-law, 8000 Hz, mono, 20 ms frames (160 bytes)
    model       PCM16 little-endian, 24000 Hz, mono

Both halves of that mismatch have to be undone in each direction:

    caller  ->  µ-law decode  ->  PCM16 @ 8k  ->  upsample x3  ->  PCM16 @ 24k
    model   ->  PCM16 @ 24k   ->  downsample /3 ->  PCM16 @ 8k  ->  µ-law encode

**Why µ-law at 8 kHz.** That is what the public telephone network carries, and
what a SIP trunk offers by default. It is not a choice this application gets to
make — the caller's audio has already been through it before it arrives.

**Why 24 kHz PCM16.** `app.realtime.banking_realtime.model_settings` asks for
`pcm16` in both directions, which is 24 kHz for this model. OpenAI Realtime can
also accept `g711_ulaw` directly, which would make this module unnecessary —
but the browser channel already runs on `pcm16` and both channels must reach
the *same* agent with the same settings. A per-channel audio format would be a
second configuration to keep in step, and the first thing to drift.

Pure Python on purpose. `audioop` would be faster, but it is deprecated and
removed in 3.13, and this is 8000 samples per second per call: at the five-call
target that is 40 000 samples a second, which Python does comfortably. A
dependency on a C extension for that would be paying in portability for
headroom nobody needs.

Nothing here logs, and nothing here retains a frame. Audio is the most
sensitive thing this application touches — it is the customer's voice, and it
may contain a spoken PIN — so it passes through and is not kept.
"""

from __future__ import annotations

import array

# --- G.711 µ-law -------------------------------------------------------------
#
# ITU-T G.711. The encoding is a sign bit, a 3-bit exponent and a 4-bit
# mantissa, stored inverted — a logarithmic companding that gives small samples
# (most of speech) more resolution than large ones.

_BIAS = 0x84

TELEPHONY_SAMPLE_RATE = 8000
MODEL_SAMPLE_RATE = 24000

# 20 ms of µ-law at 8 kHz. One RTP packet, and the unit a SIP trunk speaks in.
FRAME_MS = 20
ULAW_FRAME_BYTES = TELEPHONY_SAMPLE_RATE * FRAME_MS // 1000  # 160


def _build_decode_table() -> tuple[int, ...]:
    """Every µ-law byte's linear value, computed once at import."""
    table = []
    for byte in range(256):
        inverted = ~byte & 0xFF
        magnitude = ((inverted & 0x0F) << 3) + _BIAS
        magnitude <<= (inverted & 0x70) >> 4
        magnitude -= _BIAS
        table.append(-magnitude if inverted & 0x80 else magnitude)
    return tuple(table)


_DECODE = _build_decode_table()

# Segment boundaries for the encoder, from G.711's reference implementation.
# The encoder works on a 14-bit magnitude, not the full 16-bit sample: µ-law
# carries roughly 13 bits of magnitude plus a sign, so the sample is shifted
# down by two before anything else happens. Getting that wrong produces audio
# that decodes without error and sounds like noise.
_SEGMENT_ENDS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)

_ENCODE_BIAS = 33  # 0x84 >> 2, in the same 14-bit domain
_ENCODE_CLIP = 8159


def _native_is_little_endian() -> bool:
    return array.array("h", [1]).tobytes()[0] == 1


def _segment(magnitude: int) -> int:
    for index, boundary in enumerate(_SEGMENT_ENDS):
        if magnitude <= boundary:
            return index
    return 8


def ulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode µ-law bytes to little-endian PCM16, sample rate unchanged."""
    samples = array.array("h", [_DECODE[byte] for byte in payload])
    # The wire is little-endian and so is every platform this runs on, but say
    # so rather than inherit it: a big-endian host would otherwise emit audio
    # that is silently byte-swapped, which sounds like noise, not like a bug.
    if not _native_is_little_endian():  # pragma: no cover - big-endian
        samples.byteswap()
    return samples.tobytes()


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Encode little-endian PCM16 to µ-law, sample rate unchanged.

    The exact inverse of `ulaw_to_pcm16`, and byte-for-byte identical to the
    reference G.711 encoder — which is asserted by test against `audioop`
    rather than assumed. An encoder that merely *looks* symmetric with the
    decoder is the kind of mistake that survives review and arrives as static
    in a customer's ear.
    """
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not _native_is_little_endian():  # pragma: no cover - big-endian
        samples.byteswap()

    out = bytearray(len(samples))
    for index, sample in enumerate(samples):
        magnitude = sample >> 2  # 16-bit sample to 14-bit magnitude domain
        if magnitude < 0:
            magnitude = -magnitude
            mask = 0x7F
        else:
            mask = 0xFF
        if magnitude > _ENCODE_CLIP:
            magnitude = _ENCODE_CLIP
        magnitude += _ENCODE_BIAS

        segment = _segment(magnitude)
        if segment >= 8:
            out[index] = 0x7F ^ mask
        else:
            mantissa = (magnitude >> (segment + 1)) & 0x0F
            out[index] = ((segment << 4) | mantissa) ^ mask
    return bytes(out)


# --- sample rate -------------------------------------------------------------


def resample_pcm16(pcm: bytes, *, source_rate: int, target_rate: int) -> bytes:
    """Change the sample rate of little-endian PCM16 audio.

    Two cases are handled separately because the telephone ones are exact:

    * **Upsampling** (8k -> 24k) interpolates linearly between neighbours.
      Repeating each sample three times would be cheaper and would add a buzz,
      because a stepped waveform is a waveform with harmonics that were not in
      the speech.

    * **Downsampling** (24k -> 8k) averages each group of samples rather than
      taking every third one. Plain decimation folds everything above 4 kHz
      back into the audible band as aliasing — the model's `s` and `f` sounds
      would come out as tones. Averaging is a crude low-pass, but it is the
      difference between intelligible and not.
    """
    if source_rate == target_rate or not pcm:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return b""

    if target_rate > source_rate and target_rate % source_rate == 0:
        factor = target_rate // source_rate
        out = array.array("h", bytes(2 * len(samples) * factor))
        for index in range(len(samples)):
            start = samples[index]
            end = samples[index + 1] if index + 1 < len(samples) else start
            for step in range(factor):
                out[index * factor + step] = int(start + (end - start) * step / factor)
    elif source_rate > target_rate and source_rate % target_rate == 0:
        factor = source_rate // target_rate
        count = len(samples) // factor
        out = array.array("h", bytes(2 * count))
        for index in range(count):
            window = samples[index * factor : (index + 1) * factor]
            out[index] = sum(window) // factor
    else:
        # Any other ratio: nearest-neighbour. Not used by the telephone path,
        # which is always an exact 1:3, but present so an odd provider rate
        # degrades in quality rather than raising.
        count = max(1, len(samples) * target_rate // source_rate)
        out = array.array("h", bytes(2 * count))
        for index in range(count):
            out[index] = samples[min(len(samples) - 1, index * source_rate // target_rate)]

    return out.tobytes()


# --- the two directions, named ----------------------------------------------


def telephony_to_model(payload: bytes) -> bytes:
    """One frame from the caller, ready for the model."""
    return resample_pcm16(
        ulaw_to_pcm16(payload),
        source_rate=TELEPHONY_SAMPLE_RATE,
        target_rate=MODEL_SAMPLE_RATE,
    )


def model_to_telephony(pcm: bytes) -> bytes:
    """One chunk from the model, ready for the caller."""
    return pcm16_to_ulaw(
        resample_pcm16(
            pcm, source_rate=MODEL_SAMPLE_RATE, target_rate=TELEPHONY_SAMPLE_RATE
        )
    )
