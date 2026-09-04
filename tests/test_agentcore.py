from __future__ import annotations

import io
import json

import pytest
from deployment.deploy_agentcore import enable_mmdsv2, wait_until_ready
from starlette.testclient import TestClient

from ontheline.agentcore import create_app
from ontheline.config import Settings
from ontheline.database import Store
from ontheline.domain import ExtractionResult
from ontheline.workflow import VerificationWorkflow, demo_transcript


def extraction() -> ExtractionResult:
    return ExtractionResult(
        confirmed_fields=["schedule", "phone"],
        summary="Provider confirmed both requested fields.",
        outcome="complete",
        disclosure_spoken=True,
    )


def test_stateless_agentcore_contract_returns_extraction(monkeypatch, tmp_path):
    settings = Settings(
        environment="agentcore",
        agent_mode="bedrock",
        agent_session_path=tmp_path / "sessions",
    )

    def fake_run(runtime_settings, task, transcript, trace):
        assert runtime_settings is settings
        assert task["id"] == "task-123"
        assert task["fields"] == ["schedule", "phone"]
        assert transcript[-1].role == "provider"
        trace.append({"agent": "hsds-extractor", "event": "invocation.completed"})
        return extraction()

    monkeypatch.setattr("ontheline.agentcore.run_strands_extraction", fake_run)
    with TestClient(create_app(settings)) as client:
        assert client.get("/ping").json() == {"status": "Healthy"}
        response = client.post("/invocations", json={"input": {
            "session_id": "task-123",
            "organization_name": "Riverside Pantry",
            "service_name": "Weekly food pickup",
            "fields": ["schedule", "phone"],
            "transcript": [
                {"role": "assistant", "text": "This is an automated directory verification call."},
                {"role": "provider", "text": "Those details are correct."},
            ],
        }})
    assert response.status_code == 200
    assert response.json()["output"]["extraction"]["outcome"] == "complete"
    assert response.json()["output"]["trace"][0]["agent"] == "hsds-extractor"


def test_agentcore_rejects_path_like_session_ids(tmp_path):
    settings = Settings(
        environment="agentcore",
        agent_mode="bedrock",
        agent_session_path=tmp_path / "sessions",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/invocations", json={"input": {
            "session_id": "../../escape",
            "organization_name": "Organization",
            "service_name": "Service",
            "fields": ["phone"],
            "transcript": [{"role": "provider", "text": "The number is correct."}],
        }})
    assert response.status_code == 422


def test_application_can_delegate_extraction_to_agentcore(monkeypatch, tmp_path):
    settings = Settings(
        environment="test",
        database_path=tmp_path / "app.db",
        agent_mode="agentcore",
        agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/ontheline",
    )
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)

    class RuntimeClient:
        def invoke_agent_runtime(self, **kwargs):
            assert kwargs["agentRuntimeArn"] == settings.agentcore_runtime_arn
            request = json.loads(kwargs["payload"])
            assert request["input"]["session_id"] == "task_food"
            body = json.dumps({"output": {
                "extraction": extraction().model_dump(mode="json"),
                "trace": [{"agent": "hsds-extractor", "event": "invocation.completed"}],
            }}).encode()
            return {"response": io.BytesIO(body)}

    monkeypatch.setattr("ontheline.workflow.boto3.client", lambda *args, **kwargs: RuntimeClient())
    result = VerificationWorkflow(store, settings).run(
        "task_food", demo_transcript("confirmed")
    )
    assert result["status"] == "completed"
    with store.connect() as connection:
        trace = json.loads(connection.execute(
            "SELECT agent_trace_json FROM verification_runs WHERE task_id='task_food' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
    assert trace == [{"agent": "hsds-extractor", "event": "invocation.completed"}]


def test_agentcore_deployment_enables_required_mmdsv2(monkeypatch):
    class ControlClient:
        def __init__(self):
            self.updates = []
            self.statuses = iter([
                {"status": "CREATING"},
                {"status": "READY", "agentRuntimeVersion": "1"},
            ])

        def get_agent_runtime(self, **kwargs):
            assert kwargs == {"agentRuntimeId": "ontheline-runtime"}
            return next(self.statuses)

        def update_agent_runtime(self, **kwargs):
            self.updates.append(kwargs)
            return {"status": "UPDATING"}

    client = ControlClient()
    monkeypatch.setattr("deployment.deploy_agentcore.time.sleep", lambda _: None)
    runtime = wait_until_ready(client, "ontheline-runtime", timeout_seconds=60, poll_seconds=0)
    assert runtime["status"] == "READY"

    enable_mmdsv2(
        client,
        runtime_id="ontheline-runtime",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/ontheline-agentcore:release",
        role_arn="arn:aws:iam::123456789012:role/ontheline-agentcore",
        description="On the Line extraction runtime",
        runtime_environment={"OTL_ENVIRONMENT": "agentcore"},
    )
    update = client.updates[0]
    assert update["metadataConfiguration"] == {"requireMMDSV2": True}
    assert update["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert update["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_agentcore_deployment_surfaces_control_plane_failure():
    class ControlClient:
        def get_agent_runtime(self, **kwargs):
            return {"status": "CREATE_FAILED", "failureReason": "image architecture mismatch"}

    with pytest.raises(RuntimeError, match="image architecture mismatch"):
        wait_until_ready(ControlClient(), "ontheline-runtime", timeout_seconds=60, poll_seconds=0)


def test_agentcore_deployment_does_not_accept_stale_ready_version(monkeypatch):
    class ControlClient:
        def __init__(self):
            self.statuses = iter([
                {"status": "READY", "agentRuntimeVersion": "1"},
                {"status": "UPDATING", "agentRuntimeVersion": "2"},
                {
                    "status": "READY",
                    "agentRuntimeVersion": "2",
                    "metadataConfiguration": {"requireMMDSV2": True},
                },
            ])

        def get_agent_runtime(self, **kwargs):
            return next(self.statuses)

    monkeypatch.setattr("deployment.deploy_agentcore.time.sleep", lambda _: None)
    runtime = wait_until_ready(
        ControlClient(),
        "ontheline-runtime",
        timeout_seconds=60,
        poll_seconds=0,
        expected_version="2",
        require_mmdsv2=True,
    )
    assert runtime["agentRuntimeVersion"] == "2"
