from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ontheline.api import create_app
from ontheline.config import Settings
from ontheline.database import Store


@pytest.fixture
def client(tmp_path):
    settings = Settings(environment="test", database_path=tmp_path / "api.db")
    with TestClient(create_app(settings, Store(settings.database_path))) as test_client:
        yield test_client


def login(client):
    response = client.post(
        "/api/auth/login", json={"username": "judge", "password": "ontheline-demo-2026"}
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_health_and_static_console_are_public(client):
    assert client.get("/health").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "When a meal is on the line, someone should be on the line." in page.text
    assert 'href="/help"' in page.text
    help_page = client.get("/help")
    assert help_page.status_code == 200
    assert "Search what is confirmed open" in help_page.text
    css = client.get("/assets/app.css")
    assert css.status_code == 200
    assert "[hidden]{display:none!important}" in css.text


def test_private_routes_require_login(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "authentication required"


def test_login_sets_http_only_cookie_and_dashboard_works(client):
    login(client)
    cookie = client.cookies.get("ontheline_session")
    assert cookie
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["services"] == 4


def test_mutation_requires_csrf_token(client):
    login(client)
    response = client.post("/api/tasks/task_food/demo", json={"scenario": "confirmed"})
    assert response.status_code == 403


def test_curator_can_recompute_the_priority_queue(client):
    csrf = login(client)
    response = client.post("/api/campaign/refresh", headers={"x-csrf-token": csrf})
    assert response.status_code == 200
    assert response.json() == {"created": 0, "updated": 2}


def test_complete_demo_flow_creates_human_decision(client):
    csrf = login(client)
    response = client.post(
        "/api/tasks/task_legal/demo", json={"scenario": "program_closed"},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "needs_review"
    inbox = client.get("/api/decisions").json()["decisions"]
    assert len(inbox) == 2
    proposal = next(item for item in inbox if item["service_id"] == "svc_legal")
    resolved = client.post(
        f"/api/decisions/{proposal['id']}",
        json={"action": "approve", "note": "Confirmed by provider"},
        headers={"x-csrf-token": csrf},
    )
    assert resolved.status_code == 200
    remaining = client.get("/api/decisions").json()["decisions"]
    assert len(remaining) == 1
    assert remaining[0]["service_id"] == "svc_shelter"


def test_public_intake_closes_the_feedback_loop_and_redacts_pii(client):
    search = client.post("/api/intake/search", json={"need": "food", "postal_code": "02038"})
    assert [item["id"] for item in search.json()["results"]] == ["svc_food"]
    failure = client.post(
        "/api/intake/failures",
        json={"service_id": "svc_food", "reason": "wrong_hours",
              "details": "Call me at +1 202 555 0199 or person@example.com"},
    )
    assert failure.status_code == 201
    login(client)
    event = client.get("/api/audit").json()["events"][0]
    assert event["event_type"] == "failure.reported"
    with client.app.state.store.connect() as connection:
        stored = connection.execute("SELECT details FROM failure_reports").fetchone()[0]
    assert "+1 202 555 0199" not in stored
    assert "person@example.com" not in stored


def test_security_headers_are_applied(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_twiml_is_fail_closed_without_a_configured_media_secret(client):
    response = client.post("/telephony/twiml/task_food?token=anything")
    assert response.status_code == 401


def test_hsds_api_exposes_stable_uuid_services(client):
    metadata = client.get("/hsds").json()
    assert metadata["version"] == "3.2"
    collection = client.get("/hsds/services").json()
    assert collection["total_items"] == 4
    service_id = collection["contents"][0]["id"]
    assert client.get(f"/hsds/services/{service_id}").json()["id"] == service_id


def test_agentcore_contract_is_not_exposed_by_main_application(client):
    response = client.post("/invocations", json={"input": {}})
    assert response.status_code == 405
