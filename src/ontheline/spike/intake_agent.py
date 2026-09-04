"""Nova 2 Sonic intake agent backed by On the Line's transactional referral tools."""

from __future__ import annotations

import json
from dataclasses import dataclass

from strands import ToolContext
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
from strands.tools.decorator import tool

from ontheline.database import Store
from ontheline.privacy import redact_pii

INTAKE_DISCLOSURE = (
    "Hi, I'm On the Line, an automated assistant for {directory_name}. "
    "I can help find current community services without collecting your name. What do you need?"
)

INTAKE_SCRIPT = """You are the intake assistant for {directory_name}, a community resource
directory. Help callers find an active service and create a referral receipt they can use to
report inaccurate information.

OPEN WITH EXACTLY THIS, before anything else:
"{disclosure}"

Use `search_services` after learning the type of help and optional postal code. Briefly describe
the best matches without promising availability or eligibility. When the caller chooses one,
call `accept_referral` and read back the service phone, address, schedule, and application steps.
If the caller reports a previous referral failed, call `report_referral_failure`; this immediately
creates a priority verification task. Ask only for the service, failure category, and what happened.

HARD RULES:
- Never request a name, email address, date of birth, government identifier, medical detail, or
  immigration status.
- Never promise availability, eligibility, an appointment, or an outcome.
- If the caller describes immediate danger, tell them to contact local emergency services and end.
- Keep responses short and usable over a telephone connection.
- End with `end_intake` when the caller has the information they need.
"""

FAILURE_REASONS = {"wrong_hours", "disconnected", "closed", "ineligible", "moved", "other"}


@dataclass
class IntakeRecord:
    last_referral_id: str | None = None
    end_reason: str | None = None


def build_intake_tools(store: Store, record: IntakeRecord):
    @tool
    def search_services(need: str, postal_code: str = "") -> str:
        """Find active community services, ranked by relevance and assurance freshness.

        Args:
            need: A short service need such as food, shelter, or legal help.
            postal_code: Optional postal code used for local ranking.
        """
        if not 2 <= len(need.strip()) <= 120 or len(postal_code) > 20:
            return json.dumps({"error": "search terms are invalid"})
        results = store.search_referrals(need.strip(), postal_code.strip(), 5)
        safe_fields = (
            "id", "name", "organization_name", "description", "phone", "byday",
            "opens_at", "closes_at",
            "address_1", "city", "region", "postal_code", "eligibility", "fees",
            "application_process", "assured_date",
        )
        return json.dumps([
            {field: item.get(field) for field in safe_fields if field in item}
            for item in results
        ], ensure_ascii=False)

    @tool
    def accept_referral(service_id: str, need: str, postal_code: str = "") -> str:
        """Create a referral receipt after the caller chooses an active service.

        Args:
            service_id: Exact service identifier returned by search_services.
            need: The short service need used for this referral.
            postal_code: Optional postal code used in the search.
        """
        service = store.service(service_id)
        if not service or service["status"] != "active":
            return json.dumps({"error": "service is not active"})
        if not 2 <= len(need.strip()) <= 120 or len(postal_code) > 20:
            return json.dumps({"error": "referral details are invalid"})
        referral_id = store.record_referral(
            service_id, redact_pii(need), postal_code.strip(), channel="voice"
        )
        record.last_referral_id = referral_id
        return json.dumps({"referral_id": referral_id, "service_id": service_id})

    @tool
    def report_referral_failure(
        service_id: str, reason: str, details: str = "", referral_id: str = ""
    ) -> str:
        """Report inaccurate service information and create a priority verification task.

        Args:
            service_id: Exact service identifier from a referral.
            reason: wrong_hours, disconnected, closed, ineligible, moved, or other.
            details: Brief account of the inaccurate field, without caller identity.
            referral_id: Referral receipt; defaults to the receipt created in this call.
        """
        if reason not in FAILURE_REASONS or len(details) > 1000:
            return json.dumps({"error": "failure report is invalid"})
        receipt = referral_id or record.last_referral_id
        try:
            report_id, task_id = store.report_failure(
                service_id, reason, redact_pii(details), receipt
            )
        except (KeyError, ValueError) as error:
            return json.dumps({"error": str(error)})
        return json.dumps({"report_id": report_id, "verification_task_id": task_id})

    @tool(context=True)
    def end_intake(reason: str, tool_context: ToolContext) -> str:
        """End intake after referral, caller decline, or emergency redirection.

        Args:
            reason: referral_complete, caller_declined, no_match, or emergency_redirect.
        """
        allowed = {"referral_complete", "caller_declined", "no_match", "emergency_redirect"}
        record.end_reason = reason if reason in allowed else "caller_declined"
        tool_context.agent.request_state["stop_event_loop"] = True
        return "Intake ended."

    return [search_services, accept_referral, report_referral_failure, end_intake]


def build_intake_agent(
    store: Store,
    record: IntakeRecord,
    directory_name: str,
    region: str,
    model_input_rate: int = 8000,
    voice: str = "matthew",
) -> BidiAgent:
    disclosure = INTAKE_DISCLOSURE.format(directory_name=directory_name)
    model = BidiNovaSonicModel(
        provider_config={
            "audio": {
                "input_rate": model_input_rate,
                "output_rate": 16000,
                "channels": 1,
                "format": "pcm",
                "voice": voice,
            },
            "turn_detection": {"endpointingSensitivity": "LOW"},
        },
        client_config={"region": region},
    )
    return BidiAgent(
        model=model,
        tools=build_intake_tools(store, record),
        system_prompt=INTAKE_SCRIPT.format(
            directory_name=directory_name,
            disclosure=disclosure,
        ),
    )
