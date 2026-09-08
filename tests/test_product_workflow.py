from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ontheline.config import Settings
from ontheline.database import Store
from ontheline.domain import ConsentRequest, ExtractionResult, HSDSServiceInput
from ontheline.policy import classify_extraction
from ontheline.workflow import VerificationWorkflow, demo_transcript


@pytest.fixture
def product(tmp_path):
    settings = Settings(environment="test", database_path=tmp_path / "ontheline.db")
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    return store, VerificationWorkflow(store, settings)


def test_schema_v2_is_upgraded_without_losing_outbound_calls(tmp_path):
    database_path = tmp_path / "schema-v2.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript("""
            CREATE TABLE schema_meta (version INTEGER NOT NULL);
            INSERT INTO schema_meta(version) VALUES (2);
            CREATE TABLE outbound_calls (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, consent_id TEXT NOT NULL,
                provider TEXT NOT NULL, destination TEXT NOT NULL,
                stream_token_hash TEXT NOT NULL UNIQUE, external_sid TEXT, status TEXT NOT NULL,
                failure_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO outbound_calls VALUES (
                'call_existing','task_existing','consent_existing','twilio','+12025550100',
                'token-hash',NULL,'completed','',
                '2026-08-11T12:00:00+00:00','2026-08-11T12:01:00+00:00'
            );
        """)

    store = Store(database_path)
    store.initialize()
    with store.connect() as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(outbound_calls)")}
        existing = connection.execute(
            "SELECT id,stream_started_at FROM outbound_calls WHERE id='call_existing'"
        ).fetchone()

    assert version == 3
    assert "stream_started_at" in columns
    assert dict(existing) == {"id": "call_existing", "stream_started_at": None}


def test_safe_schedule_change_auto_applies_with_evidence(product):
    store, workflow = product
    before = store.service("svc_food")["assured_date"]
    result = workflow.run("task_food", demo_transcript("hours_changed"), "hours_changed")

    assert result["tier"] == 1
    assert result["status"] == "completed"
    assert store.service("svc_food")["assured_date"] > before
    with store.connect() as connection:
        schedule = connection.execute(
            "SELECT byday,opens_at,closes_at,description FROM schedules WHERE service_id='svc_food'"
        ).fetchone()
    assert tuple(schedule) == ("WE", "10:00", "13:00", "WE 10:00-13:00")
    assert all(item["service_id"] != "svc_food" for item in store.decisions())
    assert store.verify_audit_chain()


def test_closure_waits_for_a_human_and_can_then_be_approved(product):
    store, workflow = product
    result = workflow.run("task_legal", demo_transcript("program_closed"), "program_closed")

    assert result["tier"] == 2
    assert store.service("svc_legal")["status"] == "active"
    proposal = next(item for item in store.decisions() if item["service_id"] == "svc_legal")
    store.resolve_decision(proposal["id"], "approve", "usr_judge", None, "Provider confirmed closure")
    assert store.service("svc_legal")["status"] == "inactive"
    assert all(item["service_id"] != "svc_legal" for item in store.decisions())
    assert store.verify_audit_chain()


def test_stop_calling_sets_permanent_suppression(product):
    store, workflow = product
    result = workflow.run("task_food", demo_transcript("stop_calling"), "stop_calling")
    assert result["tier"] == 3
    with store.connect() as connection:
        assert connection.execute(
            "SELECT do_not_call FROM organizations WHERE id='org_riverside'"
        ).fetchone()[0] == 1
    flag = next(item for item in store.decisions() if item["service_id"] == "svc_food")
    assert flag["kind"] == "safety"
    store.resolve_decision(flag["id"], "acknowledge", "usr_judge", None, "Opt-out noted")
    assert all(item["service_id"] != "svc_food" for item in store.decisions())


def test_provider_transcript_is_redacted_before_storage(product):
    store, workflow = product
    transcript = demo_transcript("confirmed")
    transcript[-1].text += " Call +1 202 555 0199 or person@example.com."
    workflow.run("task_food", transcript, "confirmed")
    with store.connect() as connection:
        stored = connection.execute(
            "SELECT transcript_json FROM verification_runs WHERE task_id='task_food'"
        ).fetchone()[0]
    assert "+1 202 555 0199" not in stored
    assert "person@example.com" not in stored


def test_production_bootstrap_never_loads_synthetic_directory(tmp_path):
    settings = Settings(
        environment="production", database_path=tmp_path / "prod.db",
        secret_key="a-production-secret-key-with-more-than-32-characters",
        curator_password="a-unique-production-curator-password",
        agent_mode="bedrock", telephony_provider="twilio",
        twilio_account_sid="AC-test", twilio_auth_token="twilio-test-token",
        twilio_from_number="+12025550100", bootstrap_sample_data=False,
        https_only=True, public_base_url="https://ontheline.example.org",
        allowed_hosts="ontheline.example.org",
    )
    store = Store(settings.database_path)
    store.initialize()
    store.seed_demo(settings)
    assert store.list_services() == []
    assert store.user_by_username("judge") is not None


def test_production_configuration_rejects_documented_placeholders(tmp_path):
    with pytest.raises(ValueError, match="placeholder values must be replaced"):
        Settings(
            environment="production",
            database_path=tmp_path / "prod.db",
            secret_key="CHANGE_ME-secret-key-is-definitely-long-enough",
            curator_password="CHANGE_ME-curator-password",
            agent_mode="agentcore",
            agentcore_runtime_arn="CHANGE_ME-runtime-arn",
            telephony_provider="twilio",
            twilio_account_sid="CHANGE_ME",
            twilio_auth_token="CHANGE_ME",
            twilio_from_number="CHANGE_ME",
            bootstrap_sample_data=False,
            https_only=True,
            public_base_url="https://ontheline.example.org",
            allowed_hosts="ontheline.example.org",
        )


def test_failure_report_promotes_existing_task_to_top_priority(product):
    store, _ = product
    report_id, task_id = store.report_failure("svc_legal", "wrong_hours", "Door was closed", None)
    assert report_id.startswith("fail_")
    assert task_id == "task_legal"
    task = next(item for item in store.list_tasks() if item["id"] == "task_legal")
    assert task["priority"] == 1000
    assert task["trigger"] == "seeker_failure"


def test_scheduler_recomputes_live_queue_from_directory_signals(product):
    store, _ = product
    result = store.refresh_queue()
    assert result["updated"] == 2
    assert result["created"] == 0
    assert any(event["event_type"] == "queue.refreshed" for event in store.audit_events())


def test_referral_search_does_not_return_irrelevant_nearby_services(product):
    store, _ = product
    results = store.search_referrals("food", "02038", 10)
    assert [item["id"] for item in results] == ["svc_food"]


def test_audit_log_rejects_update_and_delete(product):
    store, _ = product
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_events SET actor='tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_events")


def test_partial_verification_does_not_refresh_assurance(product):
    store, _ = product
    before = store.service("svc_food")["assured_date"]
    task = store.claim_task("task_food", "test")
    extraction = ExtractionResult(
        confirmed_fields=["schedule"], summary="Phone was not checked", outcome="complete",
        disclosure_spoken=True,
    )
    result = store.record_verification(
        task["id"], extraction, classify_extraction(extraction, task["fields"]), []
    )
    assert result["status"] == "needs_review"
    assert store.service("svc_food")["assured_date"] == before
    assert any(item["kind"] == "review" for item in store.decisions())


def test_failure_referral_must_belong_to_same_service(product):
    store, _ = product
    referral = store.record_referral("svc_food", "food", "02038")
    with pytest.raises(ValueError, match="does not belong"):
        store.report_failure("svc_legal", "wrong_hours", "closed", referral)


def test_task_claim_is_atomic(product):
    store, _ = product
    claimed = store.claim_task("task_food", "worker-a")
    assert claimed["status"] == "in_progress"
    with pytest.raises(ValueError, match="not available"):
        store.claim_task("task_food", "worker-b")


def test_hsds_export_round_trips_into_an_empty_directory(tmp_path, product):
    source, _ = product
    exported = source.hsds_services()
    parsed = [HSDSServiceInput.model_validate(item) for item in exported]
    target = Store(tmp_path / "import.db")
    target.initialize()
    result = target.import_hsds_services(parsed, "usr_importer")
    assert result == {"services": 4, "organizations": 4}
    imported = target.hsds_services()
    assert {item["id"] for item in imported} == {item["id"] for item in exported}
    assert all(len(item["id"]) == 36 for item in imported)


def test_consent_window_is_enforced_when_reserving_a_call(product):
    store, _ = product
    store.create_consent(ConsentRequest(
        organization_id="org_riverside", phone_number="+12025550199",
        timezone="America/New_York", allowed_days=["MO"], earliest_local="09:00",
        latest_local="17:00", valid_from="2026-01-01", valid_until="2026-12-31",
        max_calls=1, consented_by="Director", evidence_reference="signed-form-1",
    ), "usr_judge")
    store.claim_task("task_food", "test")
    call = store.reserve_outbound_call(
        "task_food", "twilio", "token-hash",
        at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
    )
    assert call["destination"] == "+12025550199"
    store.release_task("task_food", "test cleanup")


def test_organization_call_spacing_is_enforced(product):
    store, _ = product
    store.create_consent(ConsentRequest(
        organization_id="org_riverside", phone_number="+12025550199",
        timezone="America/New_York", allowed_days=["MO"], earliest_local="09:00",
        latest_local="17:00", valid_from="2026-01-01", valid_until="2026-12-31",
        max_calls=3, consented_by="Director", evidence_reference="signed-form-spacing",
    ), "usr_judge")
    first = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    store.claim_task("task_food", "test")
    store.reserve_outbound_call(
        "task_food", "twilio", "first-token", at=first, minimum_interval_days=1
    )
    store.release_task("task_food", "test transition")
    store.claim_task("task_food", "test")
    with pytest.raises(ValueError, match="not callable again before"):
        store.reserve_outbound_call(
            "task_food", "twilio", "second-token",
            at=first + timedelta(hours=12), minimum_interval_days=1,
        )
    store.release_task("task_food", "test cleanup")


def test_non_contact_retries_are_persisted_with_exponential_backoff(product):
    store, _ = product
    start = datetime.now(UTC).replace(microsecond=0)
    store.claim_task("task_food", "test")
    first_retry = store.reschedule_task("task_food", "no-answer", at=start)
    assert datetime.fromisoformat(first_retry) == start + timedelta(days=1)
    with pytest.raises(ValueError, match="scheduled for"):
        store.claim_task("task_food", "too-early")

    with store.transaction() as connection:
        connection.execute(
            "UPDATE verification_tasks SET scheduled_for=? WHERE id='task_food'",
            ((start - timedelta(seconds=1)).isoformat(timespec="seconds"),),
        )
    store.claim_task("task_food", "test")
    second_retry = store.reschedule_task("task_food", "busy", at=start)
    assert datetime.fromisoformat(second_retry) == start + timedelta(days=2)


def test_online_backup_is_consistent_and_never_overwrites(product, tmp_path):
    store, _ = product
    target = tmp_path / "backups" / "ontheline.db"
    assert store.backup_to(target) == target
    backup = Store(target)
    assert backup.verify_audit_chain()
    assert len(backup.list_services()) == len(store.list_services())
    with pytest.raises(FileExistsError):
        store.backup_to(target)


def test_failure_arriving_during_a_call_expands_the_transactional_policy_gate(product):
    store, _ = product
    before = store.service("svc_food")["assured_date"]
    claimed = store.claim_task("task_food", "worker")
    store.report_failure("svc_food", "ineligible", "Rules changed", None)
    extraction = ExtractionResult(
        confirmed_fields=["schedule", "phone"],
        summary="Original fields were confirmed.", outcome="complete", disclosure_spoken=True,
    )
    result = store.record_verification(
        claimed["id"], extraction, classify_extraction(extraction, claimed["fields"]), []
    )
    assert result["status"] == "needs_review"
    assert result["tier"] == 2
    assert any("eligibility" in item["reason"] for item in result["policy_decisions"])
    assert store.service("svc_food")["assured_date"] == before


def test_referral_store_rechecks_active_status_inside_its_transaction(product):
    store, _ = product
    with store.transaction() as connection:
        connection.execute("UPDATE services SET status='inactive' WHERE id='svc_food'")
    with pytest.raises(ValueError, match="not currently active"):
        store.record_referral("svc_food", "food", "02038")


def test_a_curator_cannot_write_a_phone_number_the_policy_would_refuse(product):
    """The ethics document says curator edits pass the same field validators.

    They did not: `_apply_change` revalidated schedule and status, and wrote a
    phone number straight through. An autonomous change to the same field has to
    be canonical E.164, so a hand-typed one does now too.
    """
    store, _ = product
    with pytest.raises(ValueError, match="E.164"):
        with store.transaction() as connection:
            store._apply_change(connection, "svc_food", "phone", "555 0101, ask for Dana")

    with store.transaction() as connection:
        store._apply_change(connection, "svc_food", "phone", "+15550101199")
    with store.connect() as connection:
        number = connection.execute(
            "SELECT number FROM phones WHERE service_id='svc_food'"
        ).fetchone()[0]
    assert number == "+15550101199"
