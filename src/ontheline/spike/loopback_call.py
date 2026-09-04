"""Spike 2 of 3: does the telephony bridge work, without a phone number?

Runs a real Nova Sonic call through the exact adapters the carrier will use, substituting
a WAV file for the phone line. Your voice goes in as 8 kHz mu-law and the agent's reply
comes back as 8 kHz mu-law, written to a WAV you can listen to.

This is the spike worth doing before touching Amazon Connect: it isolates every problem
that is really about the codec, the frame size or barge-in from every problem that is
really about the carrier. When this sounds right, the only thing Connect adds is transport.

    # record yourself as the volunteer first (any tool, 8 kHz mono WAV)
    python -m ontheline.spike.loopback_call --input volunteer.wav --output agent_reply.wav

If the reply is intelligible and the call record shows a tool call, the bridge is proven.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import wave
from pathlib import Path

import numpy as np

from ..audio.codec import TELEPHONY_RATE, Resampler, mulaw_to_pcm16, pcm16_to_mulaw
from ..audio.telephony_io import (
    MULAW_FRAME_BYTES,
    TelephonyAudioInput,
    TelephonyAudioOutput,
)
from .call_agent import CallRecord, build_agent

logger = logging.getLogger(__name__)
FRAME_SECONDS = MULAW_FRAME_BYTES / TELEPHONY_RATE


def read_wav_as_mulaw_frames(path: Path) -> list[bytes]:
    """Load any mono 16-bit WAV and cut it into 20 ms mu-law frames at 8 kHz."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path} must be mono 16-bit PCM")
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    if rate == 16000:
        samples = Resampler(16000, 8000)(samples)
    elif rate != TELEPHONY_RATE:
        raise ValueError(f"{path} is {rate} Hz; use 8000 or 16000")

    payload = pcm16_to_mulaw(samples)
    return [
        payload[i : i + MULAW_FRAME_BYTES]
        for i in range(0, len(payload) - MULAW_FRAME_BYTES + 1, MULAW_FRAME_BYTES)
    ]


def write_mulaw_frames_as_wav(path: Path, frames: list[bytes]) -> None:
    """Write what the carrier would have played to the far end."""
    pcm = mulaw_to_pcm16(b"".join(frames))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TELEPHONY_RATE)
        wav.writeframes(pcm.tobytes())


async def run(input_wav: Path, output_wav: Path, region: str, org_name: str) -> CallRecord:
    frames = read_wav_as_mulaw_frames(input_wav)
    print(f"loaded {len(frames)} frames ({len(frames) * FRAME_SECONDS:.1f}s) from {input_wav.name}")

    record = CallRecord(
        org_name=org_name,
        fields_in_question={
            "schedule": "Wednesdays 9:00am-12:00pm",
            "phone": "(555) 010-4477",
        },
    )
    agent = build_agent(record, region=region, model_input_rate=8000)

    captured: list[bytes] = []
    audio_in = TelephonyAudioInput(model_input_rate=8000)
    audio_out = TelephonyAudioOutput(
        sink=captured.append,
        on_transcript=lambda e: record.transcript.append(
            {"role": e.get("role", "?"), "text": e.get("text", "")}
        ),
    )

    async def feed_the_line() -> None:
        """Push frames at wall-clock speed, as a carrier would."""
        for frame in frames:
            audio_in.push(frame)
            await asyncio.sleep(FRAME_SECONDS)
        # Let the agent finish its reply before we hang up.
        await asyncio.sleep(6.0)
        record.end_reason = record.end_reason or "media_exhausted"

    call = asyncio.create_task(agent.run(inputs=[audio_in], outputs=[audio_out]))
    feeder = asyncio.create_task(feed_the_line())
    try:
        await feeder
    finally:
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call
        await audio_out.stop()

    write_mulaw_frames_as_wav(output_wav, captured)
    print(f"wrote {len(captured)} frames ({len(captured) * FRAME_SECONDS:.1f}s) to {output_wav}")
    if audio_in.dropped_frames:
        print(f"WARNING: dropped {audio_in.dropped_frames} inbound frames -- agent fell behind")
    print("\n--- call record ---")
    print(record.to_json())
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="mono 16-bit WAV, 8k or 16k")
    parser.add_argument("--output", type=Path, default=Path("agent_reply.wav"))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--org", default="Riverside Community Pantry")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    asyncio.run(run(args.input, args.output, args.region, args.org))


if __name__ == "__main__":
    main()
