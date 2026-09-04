from ontheline.domain import Tier
from ontheline.policy import classify_extraction, overall_tier
from ontheline.spike.call_agent import CallRecord
from ontheline.telephony import call_record_to_extraction


def test_voice_change_without_confirmed_readback_cannot_auto_apply():
    record = CallRecord(org_name="Pantry", fields_in_question={"schedule": "Wednesday"})
    record.disclosure_spoken = True
    record.end_reason = "complete"
    record.proposed_changes.append({
        "field": "schedule", "new_value": "Thursday", "quote": "Maybe Thursday",
        "read_back_confirmed": False,
    })
    assert overall_tier(classify_extraction(call_record_to_extraction(record))) == Tier.HUMAN_DECISION


def test_voice_service_closure_cannot_auto_apply_even_after_readback():
    record = CallRecord(org_name="Pantry", fields_in_question={"status": "active"})
    record.disclosure_spoken = True
    record.end_reason = "complete"
    record.proposed_changes.append({
        "field": "status", "new_value": "inactive", "quote": "The program ended",
        "read_back_confirmed": True,
    })
    assert overall_tier(classify_extraction(call_record_to_extraction(record))) == Tier.HUMAN_DECISION


def test_voice_call_without_disclosure_is_blocked():
    record = CallRecord(org_name="Pantry", fields_in_question={"schedule": "Wednesday"})
    record.end_reason = "complete"
    record.confirmed_unchanged.append("schedule")
    assert overall_tier(classify_extraction(call_record_to_extraction(record))) == Tier.SAFETY_STOP
