from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from ontheline.api import create_app
from ontheline.config import Settings
from ontheline.database import Store
from ontheline.spike.intake_agent import (
    INTAKE_DISCLOSURE,
    INTAKE_SCRIPT,
    IntakeRecord,
    build_intake_tools,
)
from ontheline.telephony import twilio_signature


@pytest.fixture
def intake(tmp_path):
    settings = Settings(environment="test", database_path=tmp_path / "intake.db")
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    record = IntakeRecord()
    tools = {tool.tool_name: tool for tool in build_intake_tools(store, record)}
    return store, record, tools


def test_inbound_voice_script_discloses_and_collects_no_identity():
    assert "automated assistant" in INTAKE_DISCLOSURE.lower()
    assert INTAKE_SCRIPT.index("OPEN WITH EXACTLY") < INTAKE_SCRIPT.index("search_services")
    for forbidden in ("name, email", "date of birth", "government identifier"):
        assert forbidden in INTAKE_SCRIPT


def test_voice_referral_and_failure_close_the_loop(intake):
    store, record, tools = intake
    results = json.loads(tools["search_services"]("food", "02038"))
    assert results[0]["id"] == "svc_food"
    receipt = json.loads(tools["accept_referral"]("svc_food", "food", "02038"))
    assert receipt["referral_id"] == record.last_referral_id
    failure = json.loads(tools["report_referral_failure"](
        "svc_food", "wrong_hours", "Call me at +1 202 555 0199"
    ))
    assert failure["verification_task_id"] == "task_food"
    with store.connect() as connection:
        referral = connection.execute(
            "SELECT channel FROM referrals WHERE id=?", (record.last_referral_id,)
        ).fetchone()
        details = connection.execute(
            "SELECT details FROM failure_reports WHERE id=?", (failure["report_id"],)
        ).fetchone()[0]
    assert referral["channel"] == "voice"
    assert "+1 202 555 0199" not in details


def test_signed_inbound_twiml_opens_intake_stream(tmp_path):
    settings = Settings(
        environment="test", database_path=tmp_path / "api.db",
        telephony_provider="twilio", twilio_account_sid="AC123",
        twilio_auth_token="twilio-secret", twilio_from_number="+12025550100",
        public_base_url="https://ontheline.example.org",
    )
    url = "https://ontheline.example.org/telephony/intake/twiml"
    signature = twilio_signature(url, [], settings.twilio_auth_token)
    with TestClient(create_app(settings, Store(settings.database_path))) as client:
        response = client.post(
            "/telephony/intake/twiml", headers={"x-twilio-signature": signature}
        )
    assert response.status_code == 200
    assert 'url="wss://ontheline.example.org/telephony/intake/twilio"' in response.text
