"""Verification for the telephony <-> BidiAgent adapters.

These tests do not need AWS credentials or a phone line. They check the two things that
can silently break a live call: that the adapters really satisfy the Strands IO protocols
(so ``BidiAgent.run`` will accept them), and that audio survives the round trip through
the carrier's mu-law encoding at the right frame size.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest
from strands.experimental.bidi.types.events import (
    BidiAudioStreamEvent,
    BidiInterruptionEvent,
    BidiTranscriptStreamEvent,
)
from strands.experimental.bidi.types.io import BidiInput, BidiOutput

from ontheline.audio.codec import mulaw_to_pcm16, pcm16_to_mulaw
from ontheline.audio.telephony_io import (
    MULAW_FRAME_BYTES,
    TelephonyAudioInput,
    TelephonyAudioOutput,
)


def _pcm_event(samples: np.ndarray) -> BidiAudioStreamEvent:
    return BidiAudioStreamEvent(
        audio=base64.b64encode(samples.astype("<i2").tobytes()).decode(),
        format="pcm",
        sample_rate=16000,
        channels=1,
    )


def _tone(freq_hz: float, rate: int, samples: int) -> np.ndarray:
    t = np.arange(samples) / rate
    return (np.sin(2 * np.pi * freq_hz * t) * 0.5 * 32767).astype(np.int16)


# ------------------------------------------------------- protocol conformance


def test_adapters_satisfy_the_strands_io_protocols():
    """If this fails, BidiAgent.run rejects them and nothing else matters."""
    assert isinstance(TelephonyAudioInput(), BidiInput)
    assert isinstance(TelephonyAudioOutput(sink=lambda frame: None), BidiOutput)


# ------------------------------------------------------------------- inbound


async def test_inbound_frame_reaches_the_model_as_linear_pcm():
    audio_in = TelephonyAudioInput(model_input_rate=8000)
    original = _tone(1000, 8000, MULAW_FRAME_BYTES)
    audio_in.push(pcm16_to_mulaw(original))

    event = await audio_in()

    assert event["type"] == "bidi_audio_input"
    assert event["format"] == "pcm"
    assert event["sample_rate"] == 8000
    assert event["channels"] == 1
    decoded = np.frombuffer(base64.b64decode(event["audio"]), dtype="<i2")
    # mu-law is lossy, so compare against the companded original, not the pure tone.
    np.testing.assert_array_equal(decoded, mulaw_to_pcm16(pcm16_to_mulaw(original)))


async def test_inbound_resamples_when_the_model_is_configured_for_16k():
    audio_in = TelephonyAudioInput(model_input_rate=16000)
    audio_in.push(pcm16_to_mulaw(_tone(1000, 8000, MULAW_FRAME_BYTES)))

    event = await audio_in()

    assert event["sample_rate"] == 16000
    decoded = np.frombuffer(base64.b64decode(event["audio"]), dtype="<i2")
    assert len(decoded) == MULAW_FRAME_BYTES * 2


async def test_inbound_drops_stale_frames_instead_of_growing_without_bound():
    audio_in = TelephonyAudioInput(max_queued_frames=3)
    for marker in range(6):
        audio_in.push(bytes([marker]) * MULAW_FRAME_BYTES)

    assert audio_in.dropped_frames == 3
    # The queue should hold the newest frames; the oldest were discarded.
    event = await audio_in()
    first_sample = np.frombuffer(base64.b64decode(event["audio"]), dtype="<i2")[0]
    assert first_sample == mulaw_to_pcm16(bytes([3]))[0]


# ------------------------------------------------------------------ outbound


async def test_outbound_emits_exact_20ms_mulaw_frames():
    sent: list[bytes] = []
    audio_out = TelephonyAudioOutput(sink=sent.append)

    # 640 samples at 16 kHz -> 320 at 8 kHz -> exactly two 160-byte frames.
    await audio_out(_pcm_event(_tone(1000, 16000, 640)))

    assert len(sent) == 2
    assert all(len(frame) == MULAW_FRAME_BYTES for frame in sent)
    assert audio_out.frames_sent == 2


async def test_outbound_holds_a_partial_frame_until_it_is_whole():
    sent: list[bytes] = []
    audio_out = TelephonyAudioOutput(sink=sent.append)

    await audio_out(_pcm_event(_tone(1000, 16000, 200)))  # -> 100 bytes, under one frame
    assert sent == []

    await audio_out(_pcm_event(_tone(1000, 16000, 200)))  # -> 200 bytes total
    assert len(sent) == 1


async def test_stop_flushes_the_final_syllable():
    sent: list[bytes] = []
    audio_out = TelephonyAudioOutput(sink=sent.append)

    await audio_out(_pcm_event(_tone(1000, 16000, 100)))
    assert sent == []

    await audio_out.stop()
    assert len(sent) == 1
    assert len(sent[0]) == MULAW_FRAME_BYTES


async def test_barge_in_discards_audio_not_yet_on_the_wire():
    """When the caller talks over the agent, buffered speech must not still play."""
    sent: list[bytes] = []
    audio_out = TelephonyAudioOutput(sink=sent.append)

    await audio_out(_pcm_event(_tone(1000, 16000, 200)))  # partial frame, buffered
    await audio_out(BidiInterruptionEvent(reason="user_speech"))
    await audio_out.stop()

    assert sent == []  # the buffered partial frame was dropped, not flushed


async def test_barge_in_notifies_the_call_record():
    reasons: list[str] = []
    audio_out = TelephonyAudioOutput(sink=lambda f: None, on_interruption=reasons.append)

    await audio_out(BidiInterruptionEvent(reason="user_speech"))

    assert reasons == ["user_speech"]


async def test_transcripts_are_forwarded_for_the_call_record():
    captured: list[dict] = []
    audio_out = TelephonyAudioOutput(sink=lambda f: None, on_transcript=captured.append)

    await audio_out(
        BidiTranscriptStreamEvent(
            delta={"text": "still Wednesdays"},
            text="still Wednesdays",
            role="assistant",
            is_final=True,
        )
    )

    assert len(captured) == 1
    assert captured[0]["text"] == "still Wednesdays"


async def test_outbound_accepts_an_async_sink():
    sent: list[bytes] = []

    async def async_sink(frame: bytes) -> None:
        sent.append(frame)

    audio_out = TelephonyAudioOutput(sink=async_sink)
    await audio_out(_pcm_event(_tone(1000, 16000, 640)))

    assert len(sent) == 2


async def test_unwired_output_rate_is_rejected_loudly():
    with pytest.raises(ValueError, match="24 kHz"):
        TelephonyAudioOutput(sink=lambda f: None, model_output_rate=24000)


async def test_a_full_leg_round_trips_recognisable_speech_band_audio():
    """Carrier -> agent -> carrier: the 1 kHz tone must still be there at the far end."""
    audio_in = TelephonyAudioInput(model_input_rate=16000)
    audio_in.push(pcm16_to_mulaw(_tone(1000, 8000, MULAW_FRAME_BYTES)))
    event = await audio_in()

    sent: list[bytes] = []
    audio_out = TelephonyAudioOutput(sink=sent.append)
    await audio_out(
        BidiAudioStreamEvent(audio=event["audio"], format="pcm", sample_rate=16000, channels=1)
    )
    await audio_out.stop()

    returned = mulaw_to_pcm16(b"".join(sent)).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(returned))
    peak_hz = np.fft.rfftfreq(len(returned), 1 / 8000)[np.argmax(spectrum)]
    assert 900 < peak_hz < 1100
