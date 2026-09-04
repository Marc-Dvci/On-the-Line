"""The deterministic tier boundary is the product's central safety contract."""

from ontheline.domain import ExtractionResult, ProposedFieldChange, Tier
from ontheline.policy import classify_extraction, overall_tier


def change(**overrides):
    values = {
        "field": "schedule", "new_value": "WE 10:00-13:00",
        "quote": "Yes, ten to one is correct.", "confidence": 0.99,
        "explicitly_confirmed": True, "read_back_confirmed": True,
    }
    values.update(overrides)
    return ProposedFieldChange(**values)


def result(item: ProposedFieldChange) -> ExtractionResult:
    return ExtractionResult(
        changes=[item], summary="test", outcome="complete", disclosure_spoken=True
    )


def test_explicit_simple_change_is_tier_one():
    assert overall_tier(classify_extraction(result(change()))) == Tier.SAFE_CHANGE


def test_service_removal_always_requires_a_person():
    decisions = classify_extraction(result(change(field="status", new_value="inactive", removes_service=True)))
    assert overall_tier(decisions) == Tier.HUMAN_DECISION


def test_eligibility_change_always_requires_a_person():
    assert overall_tier(classify_extraction(result(change(field="eligibility")))) == Tier.HUMAN_DECISION


def test_low_confidence_never_auto_applies():
    assert overall_tier(classify_extraction(result(change(confidence=0.89)))) == Tier.HUMAN_DECISION


def test_missing_readback_never_auto_applies():
    assert overall_tier(classify_extraction(result(change(read_back_confirmed=False)))) == Tier.HUMAN_DECISION


def test_noncanonical_schedule_never_auto_applies():
    extraction = result(change(new_value="Wednesday from ten until one"))
    assert overall_tier(classify_extraction(extraction)) == Tier.HUMAN_DECISION


def test_stop_request_is_tier_three_even_without_changes():
    extraction = ExtractionResult(
        summary="stop", outcome="stop_requested", disclosure_spoken=True
    )
    assert overall_tier(classify_extraction(extraction)) == Tier.SAFETY_STOP


def test_missing_ai_disclosure_blocks_every_write():
    extraction = result(change())
    extraction.disclosure_spoken = False
    assert overall_tier(classify_extraction(extraction)) == Tier.SAFETY_STOP


def test_confirmation_only_is_tier_zero():
    extraction = ExtractionResult(
        confirmed_fields=["schedule"], summary="confirmed", outcome="complete",
        disclosure_spoken=True,
    )
    assert overall_tier(classify_extraction(extraction)) == Tier.CONFIRMATION


def test_invalid_clock_time_never_auto_applies():
    extraction = result(change(new_value="WE 29:99-31:00"))
    assert overall_tier(classify_extraction(extraction)) == Tier.HUMAN_DECISION


def test_non_e164_phone_never_auto_applies():
    extraction = result(change(field="phone", new_value="555-0100"))
    assert overall_tier(classify_extraction(extraction)) == Tier.HUMAN_DECISION


def test_partial_verification_requires_review():
    extraction = ExtractionResult(
        confirmed_fields=["schedule"], summary="Only hours were checked", outcome="complete",
        disclosure_spoken=True,
    )
    decisions = classify_extraction(extraction, ["schedule", "phone"])
    assert overall_tier(decisions) == Tier.HUMAN_DECISION
