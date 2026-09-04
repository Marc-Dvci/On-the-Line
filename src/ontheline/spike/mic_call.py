"""Spike 3 of 3: talk to it.

Runs the verification-call agent on your microphone and speakers using the Strands
``BidiAudioIO``. This is the one to record for the demo video before real telephony
exists, and the fastest way to feel whether the call script sounds like a person.

    pip install -e ".[mic]"
    python -m ontheline.spike.mic_call --region us-east-1

Note that this path needs PortAudio (via pyaudio), which is why it is an optional extra:
``strands.experimental.bidi.io`` imports it at package level, so the headless container
that will eventually run the telephony bridge must not import that package at all.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .call_agent import CallRecord, build_agent


async def run(region: str, org_name: str, voice: str) -> CallRecord:
    try:
        from strands.experimental.bidi.io import BidiAudioIO
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise SystemExit(
            "microphone spike needs PortAudio: pip install -e '.[mic]'"
        ) from exc

    record = CallRecord(
        org_name=org_name,
        fields_in_question={
            "schedule": "Wednesdays 9:00am-12:00pm",
            "phone": "(555) 010-4477",
            "eligibility_description": "No ID or proof of address required",
        },
    )
    # 16 kHz here: this is a microphone, not a phone line, so there is no reason to
    # hand the model the degraded 8 kHz path it will get from the carrier.
    agent = build_agent(record, region=region, model_input_rate=16000, voice=voice)
    audio_io = BidiAudioIO()

    print(f"--- speak to {org_name} as the volunteer who answered. Ctrl-C to hang up. ---")
    try:
        await agent.run(inputs=[audio_io.input()], outputs=[audio_io.output()])
    except (KeyboardInterrupt, asyncio.CancelledError):
        record.end_reason = record.end_reason or "abandoned"

    print("\n--- call record ---")
    print(record.to_json())
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--org", default="Riverside Community Pantry")
    parser.add_argument("--voice", default="matthew", help="Nova Sonic voice id")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    asyncio.run(run(args.region, args.org, args.voice))


if __name__ == "__main__":
    main()
