"""Deterministic safety boundary between model output and directory writes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ontheline.domain import ExtractionResult, ProposedFieldChange, Tier

SAFETY_OUTCOMES = {"stop_requested", "reached_client", "distress", "hostile"}
HIGH_JUDGMENT_FIELDS = {"status", "eligibility"}
SAFE_FIELDS = {"name", "description", "schedule", "phone", "fees", "application_process", "alert", "address"}
CANONICAL_SCHEDULE = re.compile(
    r"((?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:MO|TU|WE|TH|FR|SA|SU))*) "
    r"(\d{2}):(\d{2})[-–](\d{2}):(\d{2})"
)
E164_PHONE = re.compile(r"\+[1-9]\d{7,14}(?:\s+(?:x|ext\.?)[ ]?\d{1,8})?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    tier: Tier
    reason: str
    change: ProposedFieldChange | None = None


def canonical_schedule(value: str) -> bool:
    match = CANONICAL_SCHEDULE.fullmatch(value)
    if not match:
        return False
    days, open_hour, open_minute, close_hour, close_minute = match.groups()
    day_list = days.split(",")
    return (
        len(day_list) == len(set(day_list))
        and int(open_hour) < 24
        and int(close_hour) < 24
        and int(open_minute) < 60
        and int(close_minute) < 60
    )


def classify_change(change: ProposedFieldChange) -> PolicyDecision:
    """Classify one proposed mutation. The model never gets a vote here."""
    if change.removes_service or change.new_value is None:
        return PolicyDecision(Tier.HUMAN_DECISION, "A removal can strand a help-seeker.", change)
    if change.field in HIGH_JUDGMENT_FIELDS:
        return PolicyDecision(Tier.HUMAN_DECISION, f"{change.field} changes require human judgment.", change)
    if change.ambiguous or change.confidence < 0.9:
        return PolicyDecision(Tier.HUMAN_DECISION, "The provider response was ambiguous or low confidence.", change)
    if change.field == "schedule" and not canonical_schedule(change.new_value or ""):
        return PolicyDecision(Tier.HUMAN_DECISION, "The schedule is not in canonical HSDS form.", change)
    if change.field == "phone" and not E164_PHONE.fullmatch(change.new_value or ""):
        return PolicyDecision(Tier.HUMAN_DECISION, "The phone number is not in canonical E.164 form.", change)
    if change.field not in SAFE_FIELDS:
        return PolicyDecision(Tier.HUMAN_DECISION, "The field is outside the safe auto-apply allowlist.", change)
    if not (change.explicitly_confirmed and change.read_back_confirmed):
        return PolicyDecision(Tier.HUMAN_DECISION, "The change was not explicitly stated and read back.", change)
    return PolicyDecision(Tier.SAFE_CHANGE, "Explicit simple change confirmed on readback.", change)


def classify_extraction(
    result: ExtractionResult, required_fields: list[str] | tuple[str, ...] = ()
) -> list[PolicyDecision]:
    if not result.disclosure_spoken:
        return [PolicyDecision(Tier.SAFETY_STOP, "AI disclosure was not confirmed; no write is allowed.")]
    if result.outcome in SAFETY_OUTCOMES:
        return [PolicyDecision(Tier.SAFETY_STOP, f"Call ended for safety: {result.outcome}.")]

    decisions = [classify_change(change) for change in result.changes]
    decisions.extend(
        PolicyDecision(Tier.CONFIRMATION, f"Provider confirmed {field} unchanged.")
        for field in sorted(set(result.confirmed_fields))
    )
    covered = set(result.confirmed_fields).union(change.field for change in result.changes)
    missing = sorted(set(required_fields).difference(covered))
    removal_reported = any(change.removes_service for change in result.changes)
    if result.outcome == "complete" and missing and not removal_reported:
        decisions.append(PolicyDecision(
            Tier.HUMAN_DECISION,
            f"Verification ended without evidence for required fields: {', '.join(missing)}.",
        ))
    if not decisions:
        decisions.append(PolicyDecision(Tier.HUMAN_DECISION, "No reliable verification outcome was extracted."))
    return decisions


def overall_tier(decisions: list[PolicyDecision]) -> Tier:
    return max((decision.tier for decision in decisions), default=Tier.HUMAN_DECISION)
