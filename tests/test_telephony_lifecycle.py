from __future__ import annotations

from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from ontheline.api import create_app
from ontheline.config import Settings
from ontheline.database import Store
from ontheline.telephony import place_outbound_call, stream_token_hash, twilio_signature


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"sid": "CA00000000000000000000000000000001"}


class FakeAsyncClient:
    def __init__(self, **kwargs):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse()


@pytest.mark.asyncio
async def test_outbound_call_uses_real_provider_lifecycle(tmp_path, monkeypatch):
    settings = Settings(
        environment="test", database_path=tmp_path / "call.db",
        telephony_provider="twilio", twilio_account_sid="AC123",
        twilio_auth_token="auth-token", twilio_from_number="+12025550100",
        public_base_url="https://ontheline.example.org",
    )
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    store.claim_task("task_food", "test")
    monkeypatch.setattr("ontheline.telephony.httpx.AsyncClient", FakeAsyncClient)
    result = await place_outbound_call(settings, store, "task_food")
    assert result["status"] == "dialing"
    with store.connect() as connection:
        call = connection.execute("SELECT * FROM outbound_calls").fetchone()
    assert call["external_sid"] == "CA00000000000000000000000000000001"
    assert call["status"] == "dialing"


def test_signed_failed_callback_releases_the_task(tmp_path):
    settings = Settings(
        environment="test", database_path=tmp_path / "callback.db",
        telephony_provider="twilio", twilio_account_sid="AC123",
        twilio_auth_token="auth-token", twilio_from_number="+12025550100",
        public_base_url="http://testserver",
    )
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    store.claim_task("task_food", "test")
    raw_token = "one-time-media-token"
    reserved = store.reserve_outbound_call("task_food", "twilio", stream_token_hash(raw_token))
    call_id = reserved["call_id"]
    query = urlencode({"token": raw_token, "call": call_id})
    url = f"http://testserver/telephony/status/{call_id}?{query}"
    params = [("CallSid", "CA123"), ("CallStatus", "no-answer")]
    signature = twilio_signature(url, params, settings.twilio_auth_token)
    with TestClient(create_app(settings, store)) as client:
        response = client.post(
            f"/telephony/status/{call_id}?{query}", data=dict(params),
            headers={"X-Twilio-Signature": signature},
        )
    assert response.status_code == 204
    assert store.get_task("task_food")["status"] == "queued"


def test_unsigned_callback_cannot_mutate_call_state(tmp_path):
    settings = Settings(
        environment="test", database_path=tmp_path / "unsigned.db",
        telephony_provider="twilio", twilio_account_sid="AC123",
        twilio_auth_token="auth-token", twilio_from_number="+12025550100",
        public_base_url="http://testserver",
    )
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    with TestClient(create_app(settings, store)) as client:
        response = client.post("/telephony/twiml/task_food?token=forged&call=forged")
    assert response.status_code == 401


def test_provider_media_token_can_be_consumed_only_once(tmp_path):
    settings = Settings(environment="test", database_path=tmp_path / "token.db")
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    store.claim_task("task_food", "test")
    token_hash = stream_token_hash("single-use-token")
    reservation = store.reserve_outbound_call("task_food", "twilio", token_hash)

    first = store.consume_outbound_media_token(
        token_hash, reservation["call_id"], "task_food"
    )
    replay = store.consume_outbound_media_token(
        token_hash, reservation["call_id"], "task_food"
    )

    assert first and first["status"] == "answered"
    assert first["stream_started_at"]
    assert replay is None
