"""The real-time provider verification call agent.

The voice turn keeps an in-memory call record so the model has no database authority.
After the call, :mod:`ontheline.telephony` converts that evidence into validated structured
output and sends it through the same deterministic policy and transactional store as the
text workflow. The separation is deliberate: the call proposes; policy publishes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from strands import ToolContext
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
from strands.tools.decorator import tool

ALLOWED_CHANGE_FIELDS = {
    "name", "description", "status", "schedule", "phone", "eligibility",
    "eligibility_description", "fees", "application_process", "alert", "address",
}
ALLOWED_END_REASONS = {
    "complete", "voicemail", "no_answer", "wrong_number", "stop_requested",
    "reached_client", "distress", "hostile", "ambiguous", "not_a_good_time",
}

# The first sentence of every call. Not configurable, not skippable, and asserted in the
# test suite -- an agent that phones people has to say what it is before it asks anything.
DISCLOSURE = (
    "Hi, I'm an automated assistant calling on behalf of {directory_name}. "
    "This call is about keeping your listing accurate. Is now an okay time for a quick check?"
)

CALL_SCRIPT = """You are a verification assistant for {directory_name}, a community
resource directory that people in need call for help finding food, shelter and services.

You are calling {org_name} to check whether the details we publish about them are still
correct. Your entire job is to leave that listing more accurate than you found it.

OPEN THE CALL WITH EXACTLY THIS, before anything else:
"{disclosure}"

Then:
- Confirm the specific fields we are unsure about, one at a time. Do not read the whole
  record at them; ask about what we flagged.
- Read back any change you hear, in your own words, and get a yes before recording it.
- Call `propose_change` for each field that changed, and `confirm_unchanged` for each
  field they confirm is still right.
- If they say a service has ended or is suspended, record it with `propose_change` and
  say a person from the directory will follow up. Never treat it as routine.

HARD RULES:
- If they ask you to stop calling, say "Of course, I'll take you off our list" and call
  `end_call` immediately with reason "stop_requested". Do not argue or ask why.
- If the person you reach is someone looking for help rather than staff, do not run the
  script. Tell them how to reach a human at the directory, then `end_call` with reason
  "reached_client".
- If you are unsure what they meant, say so and leave it for a person. A field you did
  not change is recoverable; a field you changed wrongly sends someone to a closed door.
- Never promise a service, an appointment, eligibility or a callback time.
- Keep turns short. This is a phone call to a busy volunteer, not an interview.

When the fields are covered, thank them by name if you have it and call `end_call` with
reason "complete".
"""


@dataclass
class CallRecord:
    """Everything the spike needs to prove the call did what it looked like it did."""

    org_name: str
    fields_in_question: dict[str, str]
    proposed_changes: list[dict[str, Any]] = field(default_factory=list)
    confirmed_unchanged: list[str] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    end_reason: str | None = None
    disclosure_spoken: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "org_name": self.org_name,
                "proposed_changes": self.proposed_changes,
                "confirmed_unchanged": self.confirmed_unchanged,
                "end_reason": self.end_reason,
                "disclosure_spoken": self.disclosure_spoken,
                "turns": len(self.transcript),
            },
            indent=2,
        )


def build_call_tools(record: CallRecord):
    """Build the tool set bound to one call's record."""

    @tool
    def lookup_record() -> str:
        """Get the fields this call is meant to verify, with what we currently publish."""
        return json.dumps(
            {"organization": record.org_name, "fields_to_verify": record.fields_in_question}
        )

    @tool
    def confirm_unchanged(field_name: str) -> str:
        """Record that the contact confirmed a field is still correct.

        Args:
            field_name: The field they confirmed, e.g. "schedule" or "phone".
        """
        if field_name not in record.fields_in_question:
            return f"Rejected: {field_name} was not one of the fields requested for verification."
        if any(change["field"] == field_name for change in record.proposed_changes):
            return f"Rejected: {field_name} already has a proposed change; resolve the contradiction."
        if field_name not in record.confirmed_unchanged:
            record.confirmed_unchanged.append(field_name)
        return f"Recorded: {field_name} confirmed unchanged."

    @tool
    def propose_change(
        field_name: str,
        new_value: str,
        verbatim_quote: str,
        read_back_confirmed: bool = False,
    ) -> str:
        """Record a change the contact stated, for review against the tier policy.

        Args:
            field_name: The field that changed, e.g. "schedule".
            new_value: The new value, as you read it back to them.
            verbatim_quote: What they actually said, so a human can check your reading.
            read_back_confirmed: True only after the provider confirms the exact readback.
        """
        if field_name not in ALLOWED_CHANGE_FIELDS:
            return f"Rejected: {field_name} is not a supported directory field."
        if not new_value.strip() or len(new_value) > 2000:
            return "Rejected: the proposed value must contain 1 to 2000 characters."
        if not verbatim_quote.strip() or len(verbatim_quote) > 1000:
            return "Rejected: the evidence quote must contain 1 to 1000 characters."
        if field_name in record.confirmed_unchanged:
            return f"Rejected: {field_name} was already confirmed unchanged; resolve the contradiction."
        proposal = {
            "field": field_name,
            "new_value": new_value,
            "quote": verbatim_quote,
            "read_back_confirmed": read_back_confirmed,
        }
        existing = next(
            (index for index, change in enumerate(record.proposed_changes) if change["field"] == field_name),
            None,
        )
        if existing is None:
            record.proposed_changes.append(proposal)
        else:
            record.proposed_changes[existing] = proposal
        return f"Recorded proposed change to {field_name}. It will be reviewed before publishing."

    @tool(context=True)
    def end_call(reason: str, tool_context: ToolContext) -> str:
        """End the call.

        Args:
            reason: One of "complete", "stop_requested", "reached_client", "not_a_good_time".
        """
        record.end_reason = reason if reason in ALLOWED_END_REASONS else "ambiguous"
        tool_context.agent.request_state["stop_event_loop"] = True
        return "Call ended."

    return [lookup_record, confirm_unchanged, propose_change, end_call]


def build_agent(
    record: CallRecord,
    directory_name: str = "the county resource directory",
    region: str = "us-east-1",
    model_input_rate: int = 8000,
    voice: str = "matthew",
) -> BidiAgent:
    """Assemble the verification-call agent.

    Args:
        record: The call record the tools write into.
        directory_name: Who the agent says it is calling for.
        region: Bedrock region. Nova 2 Sonic is not available everywhere -- see
            ``scripts/preflight.py``.
        model_input_rate: 8000 to feed telephony audio natively, 16000 to resample up.
        voice: Nova Sonic voice id.
    """
    disclosure = DISCLOSURE.format(directory_name=directory_name)
    model = BidiNovaSonicModel(
        provider_config={
            "audio": {
                "input_rate": model_input_rate,
                "output_rate": 16000,
                "channels": 1,
                "format": "pcm",
                "voice": voice,
            },
            # Volunteers pause mid-sentence while they check a noticeboard. Give them room.
            "turn_detection": {"endpointingSensitivity": "LOW"},
        },
        client_config={"region": region},
    )
    return BidiAgent(
        model=model,
        tools=build_call_tools(record),
        system_prompt=CALL_SCRIPT.format(
            directory_name=directory_name,
            org_name=record.org_name,
            disclosure=disclosure,
        ),
    )
