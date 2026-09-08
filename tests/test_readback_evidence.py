"""The voice agent is not a witness to its own call.

The live provider path used to take the `read_back_confirmed` flag from the
`propose_change` tool at face value. That flag comes from the model running the
conversation, and the deterministic tier policy treats it as evidence: with it
set, a low-judgment allowlisted field publishes without a curator ever seeing it.
So one incorrect tool call was enough to publish, and the strongest claim this
product makes -- that we publish only what the provider actually said and
confirmed -- rested on the model's account of itself.

Two things now sit in between, and these tests hold both.

`ontheline.readback` corroborates the claim against the recorded transcript, with
no model and no credentials, so it runs on every call in every mode. The
independent extraction graph reads the finished transcript and has to agree
before a change can publish, which is what the architecture always said the live
path did.
"""

from __future__ import annotations

import asyncio

from ontheline.config import Settings
from ontheline.domain import ExtractionResult, ProposedFieldChange, Tier
from ontheline.policy import classify_extraction, overall_tier
from ontheline.readback import corroborate, quote_supported
from ontheline.spike.call_agent import CallRecord
from ontheline.telephony import (
    call_record_to_extraction,
    reconcile_extractions,
    speaker_role,
    verified_extraction,
)
from ontheline.workflow import CALL_SCRIPTS

NEW_HOURS = "WE 10:00-13:00"


def transcript_of(scenario: str) -> list[dict[str, str]]:
    return [{"role": role, "text": text} for role, text in CALL_SCRIPTS[scenario]]


def call(transcript: list[dict[str, str]], *, claimed: bool = True) -> CallRecord:
    record = CallRecord(org_name="Riverside", fields_in_question={"schedule": "WE 09:00-12:00"})
    record.disclosure_spoken = True
    record.end_reason = "complete"
    record.transcript = transcript
    record.proposed_changes.append({
        "field": "schedule",
        "new_value": NEW_HOURS,
        "quote": "Yes, ten to one on Wednesdays is correct.",
        "read_back_confirmed": claimed,
    })
    return record


# ------------------------------------------------- corroborating a readback


def test_a_genuine_readback_is_corroborated():
    evidence = corroborate(transcript_of("hours_changed"), "schedule", NEW_HOURS)
    assert evidence.corroborated, evidence.reason
    assert "read that back" in evidence.readback_quote.lower()
    assert "correct" in evidence.confirmation_quote.lower()


def test_a_value_the_provider_never_raised_is_not_corroborated():
    """The evidence chain starts with the provider, not with the assistant.

    Reading the directory's existing value out loud and hearing "the phone
    number is right, the hours moved though" is a correction. It used to read as
    agreement.
    """
    evidence = corroborate(transcript_of("hours_changed"), "schedule", "WE 09:00-12:00")
    assert not evidence.corroborated
    assert "provider" in evidence.reason


def test_a_readback_of_the_wrong_value_is_not_corroborated():
    evidence = corroborate(transcript_of("hours_changed"), "schedule", "TH 10:00-13:00")
    assert not evidence.corroborated
    assert "whole value" in evidence.reason


def test_an_unsure_provider_is_not_a_confirmation():
    evidence = corroborate(transcript_of("uncertain_volunteer"), "schedule", "TH 09:00-12:00")
    assert not evidence.corroborated


def test_a_phone_readback_cannot_be_checked_against_a_redacted_transcript():
    """A stated limitation, and it fails the safe way."""
    evidence = corroborate(
        [
            {"role": "provider", "text": "The new number is [PHONE REDACTED]."},
            {"role": "assistant", "text": "Let me read that back, [PHONE REDACTED]. Right?"},
            {"role": "provider", "text": "Yes, correct."},
        ],
        "phone",
        "+15550101199",
    )
    assert not evidence.corroborated
    assert "redact" in evidence.reason


def test_a_quote_the_provider_never_said_is_not_supported():
    assert quote_supported(transcript_of("hours_changed"), "Yes, ten to one on Wednesdays.")
    assert not quote_supported(
        transcript_of("hours_changed"), "We now open on Saturday evenings for dinner service."
    )


# ------------------------------------------------- the claim reaching policy


def test_an_uncorroborated_readback_claim_cannot_publish():
    """The reviewed defect, end to end: a wrong tool call no longer publishes.

    Everything the tier policy needs for Tier 1 is present -- an allowlisted
    low-judgment field, a canonical schedule value, and the agent asserting the
    readback was confirmed. The transcript does not show the exchange, so it
    goes to a person.
    """
    record = call([
        {"role": "assistant", "text": "Hello, this is an automated assistant calling."},
        {"role": "provider", "text": "Go ahead."},
    ])
    extraction = call_record_to_extraction(record)
    change = extraction.changes[0]

    assert change.read_back_confirmed is False, "the agent's own account is not evidence"
    assert change.confidence <= 0.5
    assert change.ambiguous
    assert overall_tier(classify_extraction(extraction, ["schedule"])) == Tier.HUMAN_DECISION
    assert "transcript does not show" in extraction.summary


def test_a_corroborated_readback_still_publishes():
    """The control against the test above: the only difference is the evidence."""
    extraction = call_record_to_extraction(call(transcript_of("hours_changed")))
    change = extraction.changes[0]

    assert change.read_back_confirmed is True
    assert change.explicitly_confirmed is True
    assert change.confidence > 0.9
    assert overall_tier(classify_extraction(extraction, ["schedule"])) == Tier.SAFE_CHANGE


def test_a_change_the_agent_did_not_claim_is_untouched():
    extraction = call_record_to_extraction(call(transcript_of("hours_changed"), claimed=False))
    assert extraction.changes[0].read_back_confirmed is False
    assert overall_tier(classify_extraction(extraction, ["schedule"])) == Tier.HUMAN_DECISION


# --------------------------------------- two readings of the same call agree


def independent(value: str = NEW_HOURS, *, confirmed: bool = True) -> ExtractionResult:
    return ExtractionResult(
        changes=[ProposedFieldChange(
            field="schedule", new_value=value, quote="Yes, ten to one on Wednesdays is correct.",
            confidence=0.99, explicitly_confirmed=confirmed, read_back_confirmed=confirmed,
        )],
        summary="Read from the finished transcript.", outcome="complete", disclosure_spoken=True,
    )


def test_agreement_between_the_two_readings_keeps_a_change_publishable():
    trace: list[dict[str, str]] = []
    merged = reconcile_extractions(
        call_record_to_extraction(call(transcript_of("hours_changed"))), independent(), trace
    )
    assert merged.changes[0].read_back_confirmed is True
    assert overall_tier(classify_extraction(merged, ["schedule"])) == Tier.SAFE_CHANGE
    assert any("both-readings-agree" in event["event"] for event in trace)


def test_a_disagreement_about_the_value_goes_to_a_person():
    trace: list[dict[str, str]] = []
    merged = reconcile_extractions(
        call_record_to_extraction(call(transcript_of("hours_changed"))),
        independent("WE 11:00-14:00"),
        trace,
    )
    assert merged.changes[0].read_back_confirmed is False
    assert overall_tier(classify_extraction(merged, ["schedule"])) == Tier.HUMAN_DECISION
    assert any("disagree" in event["event"] for event in trace)


def test_a_change_only_the_call_agent_saw_goes_to_a_person():
    trace: list[dict[str, str]] = []
    merged = reconcile_extractions(
        call_record_to_extraction(call(transcript_of("hours_changed"))),
        ExtractionResult(summary="Nothing changed.", outcome="complete", disclosure_spoken=True),
        trace,
    )
    assert merged.changes[0].read_back_confirmed is False
    assert any("only-the-call-agent" in event["event"] for event in trace)


def test_a_change_only_the_transcript_reading_saw_still_reaches_a_person():
    """An omission is the failure nobody would ever discover afterwards."""
    record = CallRecord(org_name="Riverside", fields_in_question={"schedule": "WE 09:00-12:00"})
    record.disclosure_spoken = True
    record.end_reason = "complete"
    record.transcript = transcript_of("hours_changed")
    trace: list[dict[str, str]] = []

    merged = reconcile_extractions(call_record_to_extraction(record), independent(), trace)

    assert [change.field for change in merged.changes] == ["schedule"]
    assert merged.changes[0].ambiguous
    assert any("only-the-transcript-reading" in event["event"] for event in trace)


def test_either_reading_may_stop_the_call():
    trace: list[dict[str, str]] = []
    merged = reconcile_extractions(
        call_record_to_extraction(call(transcript_of("hours_changed"))),
        ExtractionResult(
            summary="They asked us to stop calling.", outcome="stop_requested",
            disclosure_spoken=True,
        ),
        trace,
    )
    assert merged.outcome == "stop_requested"
    assert overall_tier(classify_extraction(merged, ["schedule"])) == Tier.SAFETY_STOP


def test_an_unavailable_independent_reading_sends_everything_to_a_person(monkeypatch, tmp_path):
    """A reading that did not happen is not a reading that agreed."""
    import ontheline.telephony as telephony

    def unavailable(*args, **kwargs):
        raise RuntimeError("Bedrock is not reachable")

    monkeypatch.setattr(telephony, "extract_from_transcript", unavailable)
    settings = Settings(
        environment="test", database_path=tmp_path / "ontheline.db", agent_mode="bedrock"
    )
    task = {
        "id": "task_food", "organization_name": "Riverside", "service_name": "Groceries",
        "fields": ["schedule"],
    }
    trace: list[dict[str, str]] = []

    extraction = asyncio.run(
        verified_extraction(settings, task, call(transcript_of("hours_changed")), trace)
    )

    assert extraction.changes[0].read_back_confirmed is False
    assert overall_tier(classify_extraction(extraction, ["schedule"])) == Tier.HUMAN_DECISION
    assert any("independent-extraction.unavailable" in event["event"] for event in trace)


def test_demo_mode_still_corroborates_without_a_model(tmp_path):
    settings = Settings(environment="test", database_path=tmp_path / "ontheline.db")
    trace: list[dict[str, str]] = []
    extraction = asyncio.run(
        verified_extraction(settings, {"id": "task_food"}, call(transcript_of("hours_changed")), trace)
    )
    assert extraction.changes[0].read_back_confirmed is True
    assert any("transcript-corroboration.only" in event["event"] for event in trace)


# ---------------------------------------------- the speakers have to line up


def test_the_callers_turns_are_filed_as_the_provider():
    """A turn filed under the wrong speaker is a turn that cannot be evidence.

    The bidirectional model speaks in "assistant" and "user"; the directory
    speaks in "assistant", "provider" and "seeker". The bridge used to drop
    anything it did not recognise into "system", so every provider turn on a live
    call was filed as neither party. Nothing failed and nothing looked wrong: the
    corroboration would simply never find a provider agreeing to anything, and
    every live call would quietly stop qualifying for Tier 1.
    """
    assert speaker_role("user", "provider") == "provider"
    assert speaker_role("user", "seeker") == "seeker"
    assert speaker_role("assistant", "provider") == "assistant"
    assert speaker_role("provider", "provider") == "provider"
    assert speaker_role(None, "provider") == "system"
    assert speaker_role("something else", "provider") == "system"


def test_a_call_transcribed_the_way_the_model_speaks_still_corroborates():
    """The same conversation, arriving with the model's own speaker labels."""
    spoken = [
        {"role": speaker_role("assistant" if role == "assistant" else "user", "provider"),
         "text": text}
        for role, text in CALL_SCRIPTS["hours_changed"]
    ]
    assert corroborate(spoken, "schedule", NEW_HOURS).corroborated
