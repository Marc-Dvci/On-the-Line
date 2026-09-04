"""Spike 1 of 3: does the model work at all?

Runs the verification-call agent in text mode against the real Nova Sonic endpoint. No
microphone, no phone number, no carrier. If this prints a disclosure and a tool call, the
model access and the call script are proven and every remaining problem is audio plumbing.

    python -m ontheline.spike.text_call --region us-east-1

Talk to it as if you were the volunteer who answered the phone. Try:
    "yeah we're still open Wednesdays but it's 10 to 1 now, not 9 to 12"
    "actually we stopped the Saturday one back in March"
    "take us off your list please"
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .call_agent import CallRecord, build_agent
from .console_io import ConsoleTextInput, ConsoleTextOutput


async def run(region: str, org_name: str, directory_name: str) -> CallRecord:
    record = CallRecord(
        org_name=org_name,
        fields_in_question={
            "schedule": "Wednesdays 9:00am-12:00pm",
            "phone": "(555) 010-4477",
            "eligibility_description": "No ID or proof of address required",
        },
    )
    agent = build_agent(record, directory_name=directory_name, region=region)

    print(f"--- text-mode verification call to {org_name} ---")
    print("Type as the person who answered. Ctrl-C to abandon the call.\n")
    try:
        await agent.run(inputs=[ConsoleTextInput()], outputs=[ConsoleTextOutput()])
    except (KeyboardInterrupt, asyncio.CancelledError):
        record.end_reason = record.end_reason or "abandoned"

    print("\n--- call record ---")
    print(record.to_json())
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1", help="Bedrock region for Nova Sonic")
    parser.add_argument("--org", default="Riverside Community Pantry")
    parser.add_argument("--directory", default="the county resource directory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(args.region, args.org, args.directory))


if __name__ == "__main__":
    main()
