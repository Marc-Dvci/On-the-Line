"""Safety invariants for the verification-call agent.

These are the rules the project promises in its consent form and its pitch. They are
tested here so that a later prompt edit cannot quietly drop one. They require no AWS
credentials because they are properties of the call script and tool authority. Model
behavior under adversarial provider dialogue is covered by the versioned evaluation corpus.
"""

from __future__ import annotations

import json

import pytest

from ontheline.spike.call_agent import (
    CALL_SCRIPT,
    DISCLOSURE,
    CallRecord,
    build_call_tools,
)


@pytest.fixture
def record() -> CallRecord:
    return CallRecord(
        org_name="Riverside Community Pantry",
        fields_in_question={"schedule": "Wednesdays 9:00am-12:00pm"},
    )


@pytest.fixture
def tools(record: CallRecord) -> dict:
    return {tool.tool_name: tool for tool in build_call_tools(record)}


# ------------------------------------------------------------------ disclosure


def test_disclosure_says_it_is_automated():
    assert "automated assistant" in DISCLOSURE.lower()


def test_disclosure_is_the_first_instruction_in_the_call_script():
    """It has to precede every other instruction, not merely appear somewhere."""
    rendered = CALL_SCRIPT.format(
        directory_name="the county resource directory",
        org_name="Riverside Community Pantry",
        disclosure=DISCLOSURE.format(directory_name="the county resource directory"),
    )
    disclosure_at = rendered.index("automated assistant")
    for later_instruction in ("propose_change", "confirm_unchanged", "HARD RULES"):
        assert rendered.index(later_instruction) > disclosure_at


def test_call_script_names_the_directory_and_the_organization():
    rendered = CALL_SCRIPT.format(
        directory_name="Countywide 211",
        org_name="Riverside Community Pantry",
        disclosure=DISCLOSURE.format(directory_name="Countywide 211"),
    )
    assert "Countywide 211" in rendered
    assert "Riverside Community Pantry" in rendered


# ----------------------------------------------------------------- hard rules


@pytest.mark.parametrize(
    "rule",
    [
        "stop calling",  # honoured immediately
        "stop_requested",  # with a recorded reason
        "reached_client",  # a help-seeker is never put through the script
        "Never promise",  # no service, appointment or eligibility promises
    ],
)
def test_call_script_states_each_hard_rule(rule: str):
    rendered = CALL_SCRIPT.format(directory_name="d", org_name="o", disclosure="x")
    assert rule in rendered


def test_call_script_treats_service_endings_as_non_routine():
    """The asymmetry that the whole design rests on: removals go to a human."""
    rendered = CALL_SCRIPT.format(directory_name="d", org_name="o", disclosure="x")
    assert "Never treat it as routine" in rendered
    assert "follow up" in rendered


# ---------------------------------------------------------------------- tools


def test_the_agent_has_no_tool_that_can_publish_a_change(tools):
    """Nothing in the call agent's reach writes to the directory.

    The call proposes; the Reconciler and the tier policy dispose. If a tool named
    anything like `apply`, `publish` or `delete` ever appears here, the safety story
    of the project is broken.
    """
    assert set(tools) == {"lookup_record", "confirm_unchanged", "propose_change", "end_call"}
    for forbidden in ("apply", "publish", "delete", "remove", "update"):
        assert not any(forbidden in name for name in tools)


def test_proposed_change_keeps_the_verbatim_quote_for_review(tools, record):
    tools["propose_change"](
        field_name="schedule",
        new_value="Wednesdays 10:00am-1:00pm",
        verbatim_quote="it's ten to one now, not nine to twelve",
    )
    change = record.proposed_changes[0]
    assert change["field"] == "schedule"
    assert change["quote"] == "it's ten to one now, not nine to twelve"


def test_proposed_change_says_out_loud_that_it_is_not_published_yet(tools):
    result = tools["propose_change"](field_name="phone", new_value="555", verbatim_quote="q")
    assert "reviewed" in result.lower()


def test_lookup_exposes_only_the_fields_under_question(tools, record):
    payload = json.loads(tools["lookup_record"]())
    assert payload["organization"] == record.org_name
    assert set(payload["fields_to_verify"]) == {"schedule"}


def test_confirming_a_field_does_not_count_as_changing_it(tools, record):
    tools["confirm_unchanged"]("schedule")
    assert record.confirmed_unchanged == ["schedule"]
    assert record.proposed_changes == []


def test_call_record_serialises_the_fields_the_curator_needs(record):
    record.proposed_changes.append({"field": "schedule", "new_value": "x", "quote": "y"})
    record.end_reason = "complete"
    payload = json.loads(record.to_json())
    assert payload["end_reason"] == "complete"
    assert payload["proposed_changes"][0]["field"] == "schedule"
    assert payload["disclosure_spoken"] is False
