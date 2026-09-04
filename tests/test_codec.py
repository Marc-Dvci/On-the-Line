"""Verification for the telephony codec.

The mu-law tests check this implementation bit-for-bit against the stdlib ``audioop``
reference. That module is deprecated in 3.12 and gone in 3.13, which is why we carry our
own implementation -- but while an interpreter still ships it, it is the ground truth we
can hold ourselves to.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from ontheline.audio.codec import (
    Framer,
    Resampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import audioop  # type: ignore[import-not-found]
    except ImportError:  # Python 3.13+
        audioop = None

requires_audioop = pytest.mark.skipif(audioop is None, reason="audioop removed in Python 3.13+")


# --------------------------------------------------------------------------- mu-law


@requires_audioop
def test_decode_matches_reference_for_every_byte():
    payload = bytes(range(256))
    expected = np.frombuffer(audioop.ulaw2lin(payload, 2), dtype="<i2")
    np.testing.assert_array_equal(mulaw_to_pcm16(payload), expected)


@requires_audioop
def test_encode_matches_reference_across_the_whole_int16_range():
    samples = np.arange(-32768, 32768, dtype=np.int16)
    expected = audioop.lin2ulaw(samples.tobytes(), 2)
    assert pcm16_to_mulaw(samples) == expected


def test_encode_decode_is_stable_under_repeat_application():
    """mu-law is lossy, but quantised values must survive a second round trip."""
    once = mulaw_to_pcm16(pcm16_to_mulaw(np.arange(-32768, 32768, dtype=np.int16)))
    twice = mulaw_to_pcm16(pcm16_to_mulaw(once))
    np.testing.assert_array_equal(once, twice)


def test_round_trip_error_stays_within_mulaw_quantisation():
    rng = np.random.default_rng(0)
    original = rng.integers(-32768, 32767, size=20_000, dtype=np.int16)
    recovered = mulaw_to_pcm16(pcm16_to_mulaw(original))
    # Companding error grows with amplitude; 8 % of full scale bounds the top segment.
    assert np.max(np.abs(recovered.astype(int) - original.astype(int))) < 0.08 * 32768


def test_silence_encodes_to_the_carrier_idle_byte():
    assert pcm16_to_mulaw(np.zeros(4, dtype=np.int16)) == b"\xff\xff\xff\xff"


def test_decode_returns_one_sample_per_byte():
    assert len(mulaw_to_pcm16(bytes(160))) == 160


# ------------------------------------------------------------------------ resampling


def _tone(freq_hz: float, rate: int, samples: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(samples) / rate
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)


def _band_energy(signal: np.ndarray, rate: int, low: float, high: float) -> float:
    spectrum = np.abs(np.fft.rfft(signal.astype(np.float64)))
    freqs = np.fft.rfftfreq(len(signal), 1 / rate)
    band = (freqs >= low) & (freqs <= high)
    return float(np.sum(spectrum[band] ** 2))


def test_downsampling_suppresses_out_of_band_energy():
    """A 6 kHz tone at 16 kHz must not fold back to 2 kHz at 8 kHz."""
    signal = _tone(6000, rate=16000, samples=16000)
    out = Resampler(16000, 8000)(signal)
    alias = _band_energy(out, 8000, 1800, 2200)
    speech = _band_energy(_tone(1000, 8000, 8000), 8000, 800, 1200)
    assert alias < speech * 1e-3


def test_speech_band_survives_a_full_round_trip():
    signal = _tone(1000, rate=8000, samples=8000)
    up = Resampler(8000, 16000)(signal)
    down = Resampler(16000, 8000)(up)
    assert len(down) == len(signal)
    # Compare in the band of interest rather than sample-wise: the FIR delays the signal.
    original_energy = _band_energy(signal, 8000, 900, 1100)
    recovered_energy = _band_energy(down, 8000, 900, 1100)
    assert 0.5 < recovered_energy / original_energy < 2.0


def test_upsampling_doubles_and_downsampling_halves_frame_length():
    frame = np.zeros(160, dtype=np.int16)  # one 20 ms telephony frame
    assert len(Resampler(8000, 16000)(frame)) == 320
    assert len(Resampler(16000, 8000)(np.zeros(320, dtype=np.int16))) == 160


def test_frame_by_frame_matches_whole_signal_processing():
    """The stateful tail is the point: framed output must equal one-shot output.

    Filtering each frame independently would leave a discontinuity at every boundary,
    which is audible as a 50 Hz buzz on a 20 ms frame clock.
    """
    signal = _tone(1200, rate=16000, samples=3200)
    one_shot = Resampler(16000, 8000)(signal)

    framed_resampler = Resampler(16000, 8000)
    framed = np.concatenate([framed_resampler(chunk) for chunk in np.split(signal, 10)])

    np.testing.assert_array_equal(framed, one_shot)


def test_reset_clears_the_tail_between_calls():
    resampler = Resampler(16000, 8000)
    resampler(_tone(1000, 16000, 320))
    resampler.reset()
    after_reset = resampler(np.zeros(320, dtype=np.int16))
    np.testing.assert_array_equal(after_reset, np.zeros(160, dtype=np.int16))


def test_rejects_conversions_the_bridge_does_not_need():
    with pytest.raises(ValueError, match="unsupported conversion"):
        Resampler(44100, 8000)


def test_identity_conversion_is_a_passthrough():
    frame = _tone(1000, 8000, 160)
    np.testing.assert_array_equal(Resampler(8000, 8000)(frame), frame)


# --------------------------------------------------------------------------- framing


def test_framer_emits_whole_frames_and_holds_the_remainder():
    framer = Framer(frame_bytes=160)
    assert framer.push(bytes(100)) == []
    assert framer.pending == 100
    frames = framer.push(bytes(300))
    assert len(frames) == 2
    assert all(len(f) == 160 for f in frames)
    assert framer.pending == 80


def test_framer_pads_the_final_partial_frame_with_silence():
    framer = Framer(frame_bytes=160)
    framer.push(b"\x01" * 10)
    tail = framer.flush()
    assert tail is not None
    assert len(tail) == 160
    assert tail[:10] == b"\x01" * 10
    assert tail[10:] == b"\xff" * 150  # mu-law silence, not zero bytes
    assert framer.flush() is None


def test_framer_preserves_byte_order_across_pushes():
    framer = Framer(frame_bytes=4)
    frames = framer.push(b"abcd") + framer.push(b"efgh")
    assert frames == [b"abcd", b"efgh"]


def test_framer_rejects_a_nonsense_frame_size():
    with pytest.raises(ValueError):
        Framer(frame_bytes=0)
