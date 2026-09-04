"""SQLite persistence for the HSDS directory, queue, decisions, and hash-chained audit log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from ontheline.config import Settings, password_hash
from ontheline.domain import ConsentRequest, ExtractionResult, HSDSServiceInput, Tier
from ontheline.policy import (
    CANONICAL_SCHEDULE,
    PolicyDecision,
    canonical_schedule,
    classify_extraction,
    overall_tier,
)
from ontheline.scoring import PrioritySignals, priority_score

SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def hsds_uuid(entity_type: str, internal_id: str) -> str:
    """Expose stable RFC 4122 identifiers while retaining readable internal keys."""
    try:
        return str(UUID(internal_id))
    except ValueError:
        pass
    return str(uuid5(NAMESPACE_URL, f"https://ontheline.directory/{entity_type}/{internal_id}"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('curator','viewer')), active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
    public_phone TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'America/New_York',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive','defunct')),
    do_not_call INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    assured_date TEXT, assurer_email TEXT NOT NULL DEFAULT '', eligibility TEXT NOT NULL DEFAULT '',
    fees TEXT NOT NULL DEFAULT '', application_process TEXT NOT NULL DEFAULT '',
    alert TEXT NOT NULL DEFAULT '', taxonomy TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, last_modified TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), name TEXT NOT NULL,
    address_1 TEXT NOT NULL, city TEXT NOT NULL, region TEXT NOT NULL, postal_code TEXT NOT NULL,
    latitude REAL, longitude REAL
);
CREATE TABLE IF NOT EXISTS service_at_location (
    service_id TEXT NOT NULL REFERENCES services(id), location_id TEXT NOT NULL REFERENCES locations(id),
    PRIMARY KEY(service_id, location_id)
);
CREATE TABLE IF NOT EXISTS phones (
    id TEXT PRIMARY KEY, organization_id TEXT REFERENCES organizations(id),
    service_id TEXT REFERENCES services(id), location_id TEXT REFERENCES locations(id),
    number TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '', type TEXT NOT NULL DEFAULT 'voice',
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id), location_id TEXT REFERENCES locations(id),
    byday TEXT NOT NULL, opens_at TEXT NOT NULL, closes_at TEXT NOT NULL,
    valid_from TEXT, valid_to TEXT, description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS verification_tasks (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    service_id TEXT NOT NULL REFERENCES services(id), trigger TEXT NOT NULL,
    fields_json TEXT NOT NULL, priority REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
      CHECK(status IN ('queued','in_progress','needs_review','completed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0, scheduled_for TEXT, claimed_at TEXT,
    created_at TEXT NOT NULL, completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_service
    ON verification_tasks(service_id) WHERE status IN ('queued','in_progress','needs_review');
CREATE INDEX IF NOT EXISTS task_queue ON verification_tasks(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS verification_runs (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES verification_tasks(id),
    tier INTEGER NOT NULL CHECK(tier BETWEEN 0 AND 3), status TEXT NOT NULL,
    disclosure_spoken INTEGER NOT NULL, recording_consent INTEGER NOT NULL,
    outcome TEXT NOT NULL, summary TEXT NOT NULL, transcript_json TEXT NOT NULL,
    agent_trace_json TEXT NOT NULL DEFAULT '[]', covered_fields_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL, completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposed_changes (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES verification_runs(id),
    service_id TEXT NOT NULL REFERENCES services(id), field_name TEXT NOT NULL,
    old_value TEXT, new_value TEXT, quote TEXT NOT NULL, confidence REAL NOT NULL,
    tier INTEGER NOT NULL CHECK(tier BETWEEN 0 AND 3), reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied','pending','approved','rejected','edited')),
    reviewed_by TEXT REFERENCES users(id), reviewed_at TEXT, review_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_inbox ON proposed_changes(status, tier DESC, created_at);
CREATE TABLE IF NOT EXISTS safety_flags (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES verification_runs(id),
    task_id TEXT NOT NULL REFERENCES verification_tasks(id),
    organization_id TEXT NOT NULL REFERENCES organizations(id), service_id TEXT NOT NULL REFERENCES services(id),
    reason TEXT NOT NULL, outcome TEXT NOT NULL, tier INTEGER NOT NULL DEFAULT 3 CHECK(tier IN (2,3)),
    status TEXT NOT NULL DEFAULT 'open'
      CHECK(status IN ('open','acknowledged')),
    reviewed_by TEXT REFERENCES users(id), reviewed_at TEXT, review_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id), need TEXT NOT NULL,
    postal_code TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'web', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failure_reports (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id), referral_id TEXT REFERENCES referrals(id),
    reason TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_consents (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    phone_number TEXT NOT NULL, timezone TEXT NOT NULL, allowed_days_json TEXT NOT NULL,
    earliest_local TEXT NOT NULL, latest_local TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
    max_calls INTEGER NOT NULL CHECK(max_calls > 0), calls_placed INTEGER NOT NULL DEFAULT 0,
    recording_allowed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn','expired')),
    consented_by TEXT NOT NULL, evidence_reference TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consent_by_organization
    ON provider_consents(organization_id,status,valid_until);
CREATE TABLE IF NOT EXISTS outbound_calls (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES verification_tasks(id),
    consent_id TEXT NOT NULL REFERENCES provider_consents(id), provider TEXT NOT NULL,
    destination TEXT NOT NULL, stream_token_hash TEXT NOT NULL UNIQUE,
    stream_started_at TEXT,
    external_sid TEXT, status TEXT NOT NULL
      CHECK(status IN ('reserved','dialing','answered','completed','failed','cancelled')),
    failure_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbound_call_by_task ON outbound_calls(task_id,created_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
    actor TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
"""

SERVICE_SELECT = """
    SELECT s.*,o.name organization_name,o.public_phone organization_phone,o.timezone organization_timezone,
           l.address_1,l.city,l.region,l.postal_code,
           p.number phone,sc.byday,sc.opens_at,sc.closes_at,
           CAST(julianday('now')-julianday(s.assured_date) AS INTEGER) age_days
    FROM services s JOIN organizations o ON o.id=s.organization_id
    LEFT JOIN locations l ON l.id=(
        SELECT location_id FROM service_at_location WHERE service_id=s.id ORDER BY location_id LIMIT 1
    )
    LEFT JOIN phones p ON p.id=(
        SELECT id FROM phones WHERE service_id=s.id ORDER BY id LIMIT 1
    )
    LEFT JOIN schedules sc ON sc.id=(
        SELECT id FROM schedules WHERE service_id=s.id ORDER BY id LIMIT 1
    )
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def backup_to(self, target: str | Path) -> Path:
        """Create and integrity-check a consistent online SQLite backup."""
        destination = Path(target)
        if destination.exists():
            raise FileExistsError(f"backup target already exists: {destination}")
        if destination.resolve() == self.path.resolve():
            raise ValueError("backup target must differ from the live database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.partial")
        try:
            with closing(self.connect()) as source, closing(sqlite3.connect(temporary)) as backup:
                source.backup(backup)
                check = backup.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError(f"backup integrity check failed: {check}")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] in {1, 2}:
                migrations = (
                    ("verification_runs", "agent_trace_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("verification_runs", "covered_fields_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("safety_flags", "tier", "INTEGER NOT NULL DEFAULT 3 CHECK(tier IN (2,3))"),
                    ("outbound_calls", "stream_started_at", "TEXT"),
                )
                for table, column, definition in migrations:
                    existing = {
                        item["name"] for item in connection.execute(f"PRAGMA table_info({table})")
                    }
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                connection.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(f"database schema {row['version']} is not supported")

    def seed_demo(self, settings: Settings) -> None:
        """Create an idempotent, clearly identified demonstration directory."""
        now = utc_now()
        # svc_family carries a completed Tier 1 verification below, so its assurance date has to
        # move with the calendar; a fixed date would make the seeded run look like it never
        # happened and open the console on 0% confirmed.
        family_assured = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                user_id = "usr_judge" if settings.bootstrap_sample_data else "usr_curator"
                display_name = "Demo curator" if settings.bootstrap_sample_data else "Directory curator"
                connection.execute(
                    "INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                    (user_id, settings.curator_username, password_hash(settings.curator_password),
                     display_name, "curator", 1, now),
                )
            if not settings.bootstrap_sample_data:
                if not connection.execute("SELECT 1 FROM audit_events LIMIT 1").fetchone():
                    self._audit(connection, "directory.initialized", "system", "directory", "production", {})
                return
            if connection.execute("SELECT 1 FROM services LIMIT 1").fetchone():
                if connection.execute("SELECT 1 FROM organizations WHERE id='org_riverside'").fetchone():
                    self._ensure_demo_history(connection, now)
                return
            organizations = [
                ("org_riverside", "Riverside Community Pantry", "Volunteer-run groceries and fresh produce.",
                 "https://example.org/riverside", "hello@example.org", "+1-555-010-1101", "America/New_York"),
                ("org_eastside", "Eastside Family Centre", "Family navigation and emergency supplies.",
                 "https://example.org/eastside", "help@example.org", "+1-555-010-2202", "America/New_York"),
                ("org_harbor", "Harbor Neighbour Network", "Seasonal shelter and outreach.",
                 "https://example.org/harbor", "team@example.org", "+1-555-010-3303", "America/New_York"),
                ("org_grove", "Grove Legal Clinic", "Free civil legal information clinics.",
                 "https://example.org/grove", "intake@example.org", "+1-555-010-4404", "America/New_York"),
            ]
            connection.executemany(
                "INSERT INTO organizations(id,name,description,url,email,public_phone,timezone,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", [(*org, now, now) for org in organizations]
            )
            connection.executemany(
                "INSERT INTO provider_consents(id,organization_id,phone_number,timezone,allowed_days_json,"
                "earliest_local,latest_local,valid_from,valid_until,max_calls,recording_allowed,consented_by,"
                "evidence_reference,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"consent_{org[0]}", org[0], org[5], org[6],
                        '["MO","TU","WE","TH","FR","SA","SU"]', "00:00", "23:59",
                        "2026-01-01", "2027-12-31", 100, 0, "provider-sandbox",
                        "seeded provider sandbox", now, now,
                    )
                    for org in organizations
                ],
            )
            services = [
                ("svc_food", "org_riverside", "Weekly grocery pickup", "A three-day grocery parcel with produce.",
                 "active", "2026-05-18", "Food insecurity; residents of the county.", "Free", "Call before visiting.", "Food"),
                ("svc_family", "org_eastside", "Family resource navigation", "Benefits, diapers and housing navigation.",
                 "active", family_assured, "Families with children under 18.", "Free", "Walk in or call.", "Family support"),
                ("svc_shelter", "org_harbor", "Cold-weather overnight shelter", "Low-barrier seasonal overnight shelter.",
                 "active", "2026-01-12", "Adults 18+ when the weather protocol is active.", "Free", "Arrive before 21:00.", "Housing"),
                ("svc_legal", "org_grove", "Tenant rights clinic", "Brief advice for renters facing eviction.",
                 "active", "2026-06-21", "County renters; no income test.", "Free", "Book by phone.", "Legal"),
            ]
            connection.executemany(
                "INSERT INTO services(id,organization_id,name,description,status,assured_date,eligibility,fees,application_process,taxonomy,created_at,last_modified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [(*svc, now, now) for svc in services]
            )
            locations = [
                ("loc_riverside", "org_riverside", "Riverside Hall", "14 River Street", "Franklin", "MA", "02038"),
                ("loc_eastside", "org_eastside", "Eastside Centre", "82 Grove Avenue", "Franklin", "MA", "02038"),
                ("loc_harbor", "org_harbor", "Harbor House", "6 Dock Lane", "Franklin", "MA", "02038"),
                ("loc_grove", "org_grove", "Grove Library", "225 Main Street", "Franklin", "MA", "02038"),
            ]
            connection.executemany(
                "INSERT INTO locations(id,organization_id,name,address_1,city,region,postal_code) VALUES (?,?,?,?,?,?,?)",
                locations,
            )
            connection.executemany("INSERT INTO service_at_location VALUES (?,?)", [
                ("svc_food", "loc_riverside"), ("svc_family", "loc_eastside"),
                ("svc_shelter", "loc_harbor"), ("svc_legal", "loc_grove")
            ])
            connection.executemany(
                "INSERT INTO phones(id,organization_id,service_id,number) VALUES (?,?,?,?)", [
                    ("phone_food", "org_riverside", "svc_food", "+1-555-010-1101"),
                    ("phone_family", "org_eastside", "svc_family", "+1-555-010-2202"),
                    ("phone_shelter", "org_harbor", "svc_shelter", "+1-555-010-3303"),
                    ("phone_legal", "org_grove", "svc_legal", "+1-555-010-4404"),
                ]
            )
            connection.executemany(
                "INSERT INTO schedules(id,service_id,location_id,byday,opens_at,closes_at,description) VALUES (?,?,?,?,?,?,?)", [
                    ("sch_food", "svc_food", "loc_riverside", "WE", "09:00", "12:00", "Every Wednesday"),
                    ("sch_family", "svc_family", "loc_eastside", "MO,TU,WE,TH,FR", "09:00", "17:00", "Weekdays"),
                    ("sch_shelter", "svc_shelter", "loc_harbor", "MO,TU,WE,TH,FR,SA,SU", "19:00", "07:00", "Weather protocol nights"),
                    ("sch_legal", "svc_legal", "loc_grove", "TU", "17:30", "20:00", "First and third Tuesday"),
                ]
            )
            tasks = [
                ("task_food", "org_riverside", "svc_food", "staleness", '["schedule","phone"]', 318.2),
                ("task_shelter", "org_harbor", "svc_shelter", "seasonality", '["status","schedule"]', 512.7),
                ("task_legal", "org_grove", "svc_legal", "staleness", '["schedule"]', 146.0),
            ]
            connection.executemany(
                "INSERT INTO verification_tasks(id,organization_id,service_id,trigger,fields_json,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                [(*task, now) for task in tasks],
            )
            # Seeded history makes every console surface judge-testable on first login.
            connection.execute(
                "INSERT INTO verification_tasks(id,organization_id,service_id,trigger,fields_json,priority,status,attempts,created_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,1,?,?)",
                ("task_family_done", "org_eastside", "svc_family", "staleness", '["phone"]',
                 91.0, "completed", now, now),
            )
            connection.execute(
                "INSERT INTO verification_runs(id,task_id,tier,status,disclosure_spoken,recording_consent,"
                "outcome,summary,transcript_json,agent_trace_json,covered_fields_json,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_family_done", "task_family_done", 1, "completed", 1, 0, "complete",
                 "Provider confirmed a simple phone update.", "[]", "[]", '["phone"]', now, now),
            )
            connection.execute(
                "INSERT INTO proposed_changes(id,run_id,service_id,field_name,old_value,new_value,quote,confidence,tier,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("chg_family_done", "run_family_done", "svc_family", "phone", "+1-555-010-2299",
                 "+1-555-010-2202", "Yes, 2202 is our current number.", 0.99, 1,
                 "Explicit simple change confirmed on readback.", "applied", now),
            )
            connection.execute(
                "UPDATE verification_tasks SET status='needs_review',attempts=1 WHERE id='task_shelter'"
            )
            connection.execute(
                "INSERT INTO verification_runs(id,task_id,tier,status,disclosure_spoken,recording_consent,"
                "outcome,summary,transcript_json,agent_trace_json,covered_fields_json,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_shelter_review", "task_shelter", 2, "needs_review", 1, 0, "complete",
                 "Provider reported that the seasonal program closed.", "[]", "[]", '["status"]', now, now),
            )
            connection.execute(
                "INSERT INTO proposed_changes(id,run_id,service_id,field_name,old_value,new_value,quote,confidence,tier,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("chg_shelter_review", "run_shelter_review", "svc_shelter", "status", "active",
                 "inactive", "We ended the overnight program after last winter.", 0.99, 2,
                 "A removal can strand a help-seeker.", "pending", now),
            )
            self._audit(connection, "directory.seeded", "system", "directory", "demo", {"services": 4})
            self._audit(connection, "change.auto_applied", "ontheline-agent", "service", "svc_family",
                        {"proposal_id": "chg_family_done", "field": "phone", "tier": 1})
            self._audit(connection, "decision.requested", "ontheline-agent", "service", "svc_shelter",
                        {"proposal_id": "chg_shelter_review", "field": "status", "tier": 2})

    def _ensure_demo_history(self, connection: sqlite3.Connection, now: str) -> None:
        """Upgrade an older demo database without replacing curator-created data."""
        demo_organizations = connection.execute(
            "SELECT id,public_phone,timezone FROM organizations WHERE id LIKE 'org_%'"
        ).fetchall()
        for organization in demo_organizations:
            connection.execute(
                "INSERT OR IGNORE INTO provider_consents(id,organization_id,phone_number,timezone,"
                "allowed_days_json,earliest_local,latest_local,valid_from,valid_until,max_calls,"
                "recording_allowed,consented_by,evidence_reference,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"consent_{organization['id']}", organization["id"], organization["public_phone"],
                    organization["timezone"], '["MO","TU","WE","TH","FR","SA","SU"]',
                    "00:00", "23:59", "2026-01-01", "2027-12-31", 100, 0,
                    "provider-sandbox", "seeded provider sandbox", now, now,
                ),
            )
        if not connection.execute(
            "SELECT 1 FROM verification_runs WHERE id='run_family_done'"
        ).fetchone():
            connection.execute(
                "INSERT OR IGNORE INTO verification_tasks(id,organization_id,service_id,trigger,fields_json,priority,status,attempts,created_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,1,?,?)",
                ("task_family_done", "org_eastside", "svc_family", "staleness", '["phone"]',
                 91.0, "completed", now, now),
            )
            connection.execute(
                "INSERT INTO verification_runs(id,task_id,tier,status,disclosure_spoken,recording_consent,"
                "outcome,summary,transcript_json,agent_trace_json,covered_fields_json,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_family_done", "task_family_done", 1, "completed", 1, 0, "complete",
                 "Provider confirmed a simple phone update.", "[]", "[]", '["phone"]', now, now),
            )
            connection.execute(
                "INSERT INTO proposed_changes(id,run_id,service_id,field_name,old_value,new_value,quote,confidence,tier,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("chg_family_done", "run_family_done", "svc_family", "phone", "+1-555-010-2299",
                 "+1-555-010-2202", "Yes, 2202 is our current number.", 0.99, 1,
                 "Explicit simple change confirmed on readback.", "applied", now),
            )
            self._audit(connection, "change.auto_applied", "ontheline-agent", "service", "svc_family",
                        {"proposal_id": "chg_family_done", "field": "phone", "tier": 1})
        shelter = connection.execute(
            "SELECT status FROM verification_tasks WHERE id='task_shelter'"
        ).fetchone()
        if shelter and shelter["status"] == "queued" and not connection.execute(
            "SELECT 1 FROM verification_runs WHERE id='run_shelter_review'"
        ).fetchone():
            connection.execute(
                "UPDATE verification_tasks SET status='needs_review',attempts=1 WHERE id='task_shelter'"
            )
            connection.execute(
                "INSERT INTO verification_runs(id,task_id,tier,status,disclosure_spoken,recording_consent,"
                "outcome,summary,transcript_json,agent_trace_json,covered_fields_json,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_shelter_review", "task_shelter", 2, "needs_review", 1, 0, "complete",
                 "Provider reported that the seasonal program closed.", "[]", "[]", '["status"]', now, now),
            )
            connection.execute(
                "INSERT INTO proposed_changes(id,run_id,service_id,field_name,old_value,new_value,quote,confidence,tier,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("chg_shelter_review", "run_shelter_review", "svc_shelter", "status", "active",
                 "inactive", "We ended the overnight program after last winter.", 0.99, 2,
                 "A removal can strand a help-seeker.", "pending", now),
            )
            self._audit(connection, "decision.requested", "ontheline-agent", "service", "svc_shelter",
                        {"proposal_id": "chg_shelter_review", "field": "status", "tier": 2})

    def _audit(
        self, connection: sqlite3.Connection, event_type: str, actor: str,
        entity_type: str, entity_id: str, payload: dict[str, Any]
    ) -> str:
        created_at = utc_now()
        previous = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "GENESIS"
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(
            f"{previous_hash}|{event_type}|{actor}|{entity_type}|{entity_id}|{canonical}|{created_at}".encode()
        ).hexdigest()
        event_id = new_id("aud")
        connection.execute(
            "INSERT INTO audit_events(id,event_type,actor,entity_type,entity_id,payload_json,previous_hash,event_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, event_type, actor, entity_type, entity_id, canonical, previous_hash, event_hash, created_at),
        )
        return event_id

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)
            ).fetchone()
            return dict(row) if row else None

    def create_session(self, user_id: str, token_hash: str, csrf_token: str, hours: int) -> None:
        now = datetime.now(UTC)
        with self.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?)",
                (token_hash, user_id, csrf_token, (now + timedelta(hours=hours)).isoformat(), now.isoformat()),
            )

    def session_user(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT u.id,u.username,u.display_name,u.role,s.csrf_token,s.expires_at "
                "FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1", (token_hash, utc_now())
            ).fetchone()
            return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def dashboard(self) -> dict[str, Any]:
        with self.connect() as connection:
            counts = connection.execute("""
                SELECT
                  (SELECT COUNT(*) FROM services WHERE status='active') AS services,
                  (SELECT COUNT(*) FROM verification_tasks WHERE status='queued') AS queued,
                  ((SELECT COUNT(*) FROM proposed_changes WHERE status='pending') +
                   (SELECT COUNT(*) FROM safety_flags WHERE status='open')) AS decisions,
                  (SELECT COUNT(*) FROM proposed_changes WHERE status='applied') AS autonomous,
                  -- every report promotes a service in the same transaction, so the count
                  -- is of reports that became calls, not of reports still waiting
                  (SELECT COUNT(*) FROM failure_reports) AS failures,
                  (SELECT COUNT(*) FROM services WHERE assured_date>=date('now','-30 day')) AS fresh
            """).fetchone()
            total = max(1, counts["services"])
            recent = self._rows(connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence DESC LIMIT 8"
            ).fetchall(), json_fields=("payload_json",))
            queue = self._task_rows(connection, limit=5)
            return {**dict(counts), "freshness": round(100 * counts["fresh"] / total),
                    "recent_activity": recent, "queue": queue}

    @staticmethod
    def _rows(rows: list[sqlite3.Row], *, json_fields: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            for field in json_fields:
                if field in item:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field))
            result.append(item)
        return result

    def _task_rows(self, connection: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = connection.execute("""
            SELECT t.*,o.name organization_name,s.name service_name,s.assured_date,
                   EXISTS(
                     SELECT 1 FROM provider_consents c WHERE c.organization_id=t.organization_id
                     AND c.status='active' AND c.valid_from<=date('now') AND c.valid_until>=date('now')
                     AND c.calls_placed<c.max_calls
                   ) consent_active
            FROM verification_tasks t JOIN organizations o ON o.id=t.organization_id
            JOIN services s ON s.id=t.service_id
            ORDER BY CASE t.status WHEN 'needs_review' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                     t.priority DESC, t.created_at LIMIT ?
        """, (limit,)).fetchall()
        return self._rows(rows, json_fields=("fields_json",))

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return self._task_rows(connection, limit=limit)

    def refresh_queue(self, actor: str = "scheduler") -> dict[str, int]:
        """Recompute field-aware priorities and create missing worthwhile tasks."""
        today = datetime.now(UTC).date()
        created = updated = 0
        with self.transaction() as connection:
            services = connection.execute("""
                SELECT s.id,s.organization_id,s.assured_date,s.taxonomy,
                  (SELECT COUNT(*) FROM referrals r WHERE r.service_id=s.id AND r.created_at>=datetime('now','-30 day')) referrals,
                  (SELECT COUNT(*) FROM failure_reports f WHERE f.service_id=s.id AND f.created_at>=datetime('now','-30 day')) failures,
                  (SELECT MAX(v.completed_at) FROM verification_runs v JOIN verification_tasks t ON t.id=v.task_id
                   WHERE t.organization_id=s.organization_id) last_contact
                FROM services s WHERE s.status='active'
            """).fetchall()
            for service in services:
                assured = date.fromisoformat(service["assured_date"]) if service["assured_date"] else None
                age = 365 if assured is None else max(0, (today - assured).days)
                fields = ["schedule", "phone"]
                if age >= 120:
                    fields.extend(["eligibility", "status"])
                last_contact = (
                    datetime.fromisoformat(service["last_contact"]).date()
                    if service["last_contact"] else None
                )
                taxonomy = service["taxonomy"].lower()
                seasonal = 1.0 if (
                    ("housing" in taxonomy and today.month in {10, 11, 12, 1, 2, 3})
                    or ("food" in taxonomy and today.month in {5, 6, 7, 8})
                ) else 0.0
                score = priority_score(PrioritySignals(
                    assured_date=assured, fields=tuple(fields), referrals_30d=service["referrals"],
                    failure_reports_30d=service["failures"], seasonality=seasonal,
                    days_since_contact=(today - last_contact).days if last_contact else None,
                ), today=today)
                existing = connection.execute(
                    "SELECT id,status FROM verification_tasks WHERE service_id=? "
                    "AND status IN ('queued','in_progress','needs_review')", (service["id"],)
                ).fetchone()
                if existing and existing["status"] == "queued":
                    connection.execute(
                        "UPDATE verification_tasks SET priority=?,fields_json=? WHERE id=?",
                        (score, json.dumps(fields), existing["id"]),
                    )
                    updated += 1
                elif not existing and score >= 30:
                    connection.execute(
                        "INSERT INTO verification_tasks(id,organization_id,service_id,trigger,fields_json,priority,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (new_id("task"), service["organization_id"], service["id"], "staleness",
                         json.dumps(fields), score, utc_now()),
                    )
                    created += 1
            self._audit(connection, "queue.refreshed", actor, "verification_queue", "default",
                        {"created": created, "updated": updated})
        return {"created": created, "updated": updated}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("""
                SELECT t.*,o.name organization_name,o.do_not_call,s.name service_name,s.status service_status,
                       s.assured_date FROM verification_tasks t JOIN organizations o ON o.id=t.organization_id
                JOIN services s ON s.id=t.service_id WHERE t.id=?
            """, (task_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["fields"] = json.loads(result.pop("fields_json"))
            return result

    def claim_task(self, task_id: str, actor: str) -> dict[str, Any]:
        """Atomically claim one queued task so two workers cannot verify it twice."""
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT t.*,o.name organization_name,o.do_not_call,s.name service_name,"
                "s.status service_status,s.assured_date FROM verification_tasks t "
                "JOIN organizations o ON o.id=t.organization_id "
                "JOIN services s ON s.id=t.service_id WHERE t.id=?",
                (task_id,),
            ).fetchone()
            if not task:
                raise KeyError("task not found")
            if task["status"] != "queued":
                raise ValueError("task is not available for verification")
            claimed_at = utc_now()
            if task["scheduled_for"] and task["scheduled_for"] > claimed_at:
                raise ValueError(f"task is scheduled for {task['scheduled_for']}")
            changed = connection.execute(
                "UPDATE verification_tasks SET status='in_progress',claimed_at=? "
                "WHERE id=? AND status='queued'",
                (claimed_at, task_id),
            ).rowcount
            if changed != 1:
                raise ValueError("task was claimed by another worker")
            self._audit(
                connection, "verification.claimed", actor, "verification_task", task_id, {}
            )
            result = dict(task)
            result["status"] = "in_progress"
            result["claimed_at"] = claimed_at
            result["fields"] = json.loads(result.pop("fields_json"))
            return result

    def release_task(self, task_id: str, reason: str, actor: str = "ontheline-workflow") -> None:
        """Return a failed in-progress task to the queue and preserve the failure evidence."""
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE verification_tasks SET status='queued',claimed_at=NULL "
                "WHERE id=? AND status='in_progress'",
                (task_id,),
            ).rowcount
            if changed:
                self._audit(
                    connection, "verification.failed", actor, "verification_task", task_id,
                    {"reason": reason[:500]},
                )

    def reschedule_task(
        self, task_id: str, reason: str, actor: str = "telephony",
        *, minimum_interval_days: int = 1, at: datetime | None = None,
    ) -> str | None:
        """Persist an exponential retry time after a non-contact call outcome."""
        at = at or datetime.now(UTC)
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT attempts,status FROM verification_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task:
                raise KeyError("task not found")
            if task["status"] != "in_progress":
                return None
            next_attempt = task["attempts"] + 1
            delay_days = max(minimum_interval_days, min(2 ** (next_attempt - 1), 14))
            scheduled_for = (at + timedelta(days=delay_days)).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE verification_tasks SET status='queued',attempts=?,scheduled_for=?,"
                "claimed_at=NULL WHERE id=? AND status='in_progress'",
                (next_attempt, scheduled_for, task_id),
            )
            self._audit(
                connection, "verification.rescheduled", actor, "verification_task", task_id,
                {"reason": reason[:500], "attempt": next_attempt, "scheduled_for": scheduled_for},
            )
            return scheduled_for

    def reserve_outbound_call(
        self, task_id: str, provider: str, stream_token_hash: str,
        *, at: datetime | None = None, minimum_interval_days: int = 1,
    ) -> dict[str, Any]:
        """Reserve a consented call attempt and consume one call from its signed allowance."""
        at = at or datetime.now(UTC)
        day_codes = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT t.*,o.do_not_call FROM verification_tasks t "
                "JOIN organizations o ON o.id=t.organization_id WHERE t.id=?",
                (task_id,),
            ).fetchone()
            if not task:
                raise KeyError("task not found")
            if task["status"] != "in_progress":
                raise ValueError("task must be claimed before dialing")
            if task["do_not_call"]:
                raise ValueError("organization has permanently opted out of calls")
            cutoff = (at - timedelta(days=minimum_interval_days)).isoformat(timespec="seconds")
            recent_call = connection.execute(
                "SELECT c.created_at FROM outbound_calls c "
                "JOIN verification_tasks prior ON prior.id=c.task_id "
                "WHERE prior.organization_id=? AND c.created_at>? AND c.status!='cancelled' "
                "ORDER BY c.created_at DESC LIMIT 1",
                (task["organization_id"], cutoff),
            ).fetchone()
            if recent_call:
                next_call = (
                    datetime.fromisoformat(recent_call["created_at"])
                    + timedelta(days=minimum_interval_days)
                ).isoformat(timespec="seconds")
                raise ValueError(f"organization is not callable again before {next_call}")
            consents = connection.execute(
                "SELECT * FROM provider_consents WHERE organization_id=? AND status='active' "
                "AND calls_placed<max_calls ORDER BY valid_until,id",
                (task["organization_id"],),
            ).fetchall()
            selected = None
            for consent in consents:
                try:
                    local = at.astimezone(ZoneInfo(consent["timezone"]))
                except Exception:
                    continue
                local_date = local.date().isoformat()
                if not (consent["valid_from"] <= local_date <= consent["valid_until"]):
                    continue
                if day_codes[local.weekday()] not in json.loads(consent["allowed_days_json"]):
                    continue
                local_time = local.strftime("%H:%M")
                earliest, latest = consent["earliest_local"], consent["latest_local"]
                in_window = (
                    earliest <= local_time <= latest
                    if earliest <= latest
                    else local_time >= earliest or local_time <= latest
                )
                if in_window:
                    selected = consent
                    break
            if not selected:
                raise ValueError("no active provider consent permits a call at this time")
            call_id, now = new_id("call"), utc_now()
            connection.execute(
                "UPDATE provider_consents SET calls_placed=calls_placed+1,updated_at=? WHERE id=?",
                (now, selected["id"]),
            )
            connection.execute(
                "INSERT INTO outbound_calls(id,task_id,consent_id,provider,destination,"
                "stream_token_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    call_id, task_id, selected["id"], provider, selected["phone_number"],
                    stream_token_hash, "reserved", now, now,
                ),
            )
            self._audit(
                connection, "call.reserved", "ontheline-caller", "outbound_call", call_id,
                {"task_id": task_id, "consent_id": selected["id"], "provider": provider},
            )
            result = dict(selected)
            result.update({"call_id": call_id, "task_id": task_id, "destination": selected["phone_number"]})
            return result

    def outbound_call_by_token(self, stream_token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT c.*,t.organization_id,t.service_id FROM outbound_calls c "
                "JOIN verification_tasks t ON t.id=c.task_id WHERE c.stream_token_hash=?",
                (stream_token_hash,),
            ).fetchone()
            return dict(row) if row else None

    def consume_outbound_media_token(
        self, stream_token_hash: str, call_id: str, task_id: str
    ) -> dict[str, Any] | None:
        """Atomically authorize exactly one provider media stream for a reserved call."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT c.*,t.organization_id,t.service_id FROM outbound_calls c "
                "JOIN verification_tasks t ON t.id=c.task_id "
                "WHERE c.stream_token_hash=? AND c.id=? AND c.task_id=? "
                "AND c.stream_started_at IS NULL AND c.status IN ('reserved','dialing')",
                (stream_token_hash, call_id, task_id),
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            updated = connection.execute(
                "UPDATE outbound_calls SET stream_started_at=?,status='answered',updated_at=? "
                "WHERE id=? AND stream_started_at IS NULL AND status IN ('reserved','dialing')",
                (now, now, call_id),
            )
            if updated.rowcount != 1:
                return None
            self._audit(
                connection, "call.answered", "telephony", "outbound_call", call_id,
                {"task_id": task_id, "media_token_consumed": True},
            )
            result = dict(row)
            result.update({"stream_started_at": now, "status": "answered"})
            return result

    def update_outbound_call(
        self, call_id: str, status: str, *, external_sid: str | None = None,
        failure_reason: str = "",
    ) -> None:
        allowed = {"dialing", "answered", "completed", "failed", "cancelled"}
        if status not in allowed:
            raise ValueError("invalid outbound call status")
        with self.transaction() as connection:
            call = connection.execute(
                "SELECT * FROM outbound_calls WHERE id=?", (call_id,)
            ).fetchone()
            if not call:
                raise KeyError("outbound call not found")
            connection.execute(
                "UPDATE outbound_calls SET status=?,external_sid=COALESCE(?,external_sid),"
                "failure_reason=?,updated_at=? WHERE id=?",
                (status, external_sid, failure_reason[:500], utc_now(), call_id),
            )
            self._audit(
                connection, f"call.{status}", "telephony", "outbound_call", call_id,
                {"external_sid": external_sid, "reason": failure_reason[:500]},
            )

    def list_services(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        search = f"%{query.strip()}%"
        with self.connect() as connection:
            rows = connection.execute(
                SERVICE_SELECT + """
                WHERE (?='' OR s.name LIKE ? OR s.description LIKE ? OR o.name LIKE ? OR s.taxonomy LIKE ?)
                ORDER BY s.status='active' DESC,s.name LIMIT ?
                """,
                (query.strip(), search, search, search, search, limit),
            ).fetchall()
            return self._rows(rows)

    def service(self, service_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                SERVICE_SELECT + " WHERE s.id=? LIMIT 1", (service_id,)
            ).fetchone()
            return dict(row) if row else None

    def hsds_services(self, public_id: str | None = None) -> list[dict[str, Any]]:
        """Return dereferenced HSDS 3.2 service objects with stable public UUIDs."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*,o.name organization_name,o.description organization_description,
                       o.url organization_url,o.email organization_email,o.public_phone,
                       p.id phone_id,p.number phone_number,p.extension phone_extension,
                       p.type phone_type,p.description phone_description,
                       sc.id schedule_id,sc.location_id schedule_location_id,sc.byday,
                       sc.opens_at,sc.closes_at,sc.valid_from,sc.valid_to,
                       sc.description schedule_description,
                       l.id location_id,l.name location_name,l.address_1,l.city,l.region,l.postal_code,
                       l.latitude,l.longitude
                FROM services s JOIN organizations o ON o.id=s.organization_id
                LEFT JOIN phones p ON p.id=(SELECT id FROM phones WHERE service_id=s.id ORDER BY id LIMIT 1)
                LEFT JOIN schedules sc ON sc.id=(SELECT id FROM schedules WHERE service_id=s.id ORDER BY id LIMIT 1)
                LEFT JOIN locations l ON l.id=(SELECT location_id FROM service_at_location
                                                WHERE service_id=s.id ORDER BY location_id LIMIT 1)
                ORDER BY s.name
                """
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            service_uuid = hsds_uuid("service", item["id"])
            if public_id and service_uuid != public_id:
                continue
            organization_uuid = hsds_uuid("organization", item["organization_id"])
            service: dict[str, Any] = {
                "id": service_uuid,
                "organization_id": organization_uuid,
                "name": item["name"],
                "description": item["description"],
                "status": item["status"],
                "eligibility_description": item["eligibility"],
                "fees_description": item["fees"],
                "application_process": item["application_process"],
                "assured_date": item["assured_date"],
                "assurer_email": item["assurer_email"],
                "alert": item["alert"],
                "last_modified": item["last_modified"],
                "organization": {
                    "id": organization_uuid,
                    "name": item["organization_name"],
                    "description": item["organization_description"],
                    "email": item["organization_email"],
                    "website": item["organization_url"],
                },
                "phones": [],
                "schedules": [],
                "service_at_locations": [],
            }
            if item["phone_id"]:
                service["phones"].append({
                    "id": hsds_uuid("phone", item["phone_id"]),
                    "service_id": service_uuid,
                    "organization_id": organization_uuid,
                    "number": item["phone_number"],
                    "extension": item["phone_extension"],
                    "type": item["phone_type"],
                    "description": item["phone_description"],
                })
            if item["schedule_id"]:
                schedule = {
                    "id": hsds_uuid("schedule", item["schedule_id"]),
                    "service_id": service_uuid,
                    "description": item["schedule_description"],
                    "freq": "WEEKLY",
                    "byday": item["byday"],
                    "opens_at": item["opens_at"],
                    "closes_at": item["closes_at"],
                }
                if item["valid_from"]:
                    schedule["valid_from"] = item["valid_from"]
                if item["valid_to"]:
                    schedule["valid_to"] = item["valid_to"]
                service["schedules"].append(schedule)
            if item["location_id"]:
                location_uuid = hsds_uuid("location", item["location_id"])
                location: dict[str, Any] = {
                    "id": location_uuid,
                    "organization_id": organization_uuid,
                    "location_type": "physical",
                    "name": item["location_name"],
                    "addresses": [{
                        "id": hsds_uuid("address", item["location_id"]),
                        "location_id": location_uuid,
                        "address_1": item["address_1"],
                        "city": item["city"],
                        "region": item["region"],
                        "postal_code": item["postal_code"],
                        "country": "US",
                        "address_type": "physical",
                    }],
                }
                if item["latitude"] is not None and item["longitude"] is not None:
                    location["latitude"] = item["latitude"]
                    location["longitude"] = item["longitude"]
                service["service_at_locations"].append({
                    "id": hsds_uuid("service_at_location", f"{item['id']}:{item['location_id']}"),
                    "service_id": service_uuid,
                    "location_id": location_uuid,
                    "location": location,
                })
            results.append(service)
        return results

    def import_hsds_services(
        self, services: list[HSDSServiceInput], actor: str
    ) -> dict[str, int]:
        """Idempotently ingest dereferenced HSDS services without deleting existing data."""
        now = utc_now()
        organizations_seen: set[str] = set()
        with self.transaction() as connection:
            for service_model in services:
                service = service_model.model_dump(mode="json")
                organization = service["organization"]
                organization_id = str(service_model.organization_id)
                service_id = str(service_model.id)
                organizations_seen.add(organization_id)
                public_phone = service["phones"][0]["number"] if service["phones"] else ""
                existing_org = connection.execute(
                    "SELECT public_phone FROM organizations WHERE id=?", (organization_id,)
                ).fetchone()
                if not public_phone and existing_org:
                    public_phone = existing_org["public_phone"]
                connection.execute(
                    """
                    INSERT INTO organizations(id,name,description,url,email,public_phone,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,
                      url=excluded.url,email=excluded.email,
                      public_phone=CASE WHEN excluded.public_phone='' THEN organizations.public_phone
                                        ELSE excluded.public_phone END,
                      updated_at=excluded.updated_at
                    """,
                    (
                        organization_id, organization["name"], organization["description"],
                        organization.get("website", ""), organization.get("email", ""),
                        public_phone, now, now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO services(id,organization_id,name,description,status,eligibility,fees,
                      application_process,alert,created_at,last_modified)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET organization_id=excluded.organization_id,
                      name=excluded.name,description=excluded.description,status=excluded.status,
                      eligibility=excluded.eligibility,fees=excluded.fees,
                      application_process=excluded.application_process,alert=excluded.alert,
                      last_modified=excluded.last_modified
                    """,
                    (
                        service_id, organization_id, service["name"], service["description"],
                        service["status"], service.get("eligibility_description", ""),
                        service.get("fees_description", ""), service.get("application_process", ""),
                        service.get("alert", ""), now, now,
                    ),
                )
                for phone in service["phones"]:
                    connection.execute(
                        """
                        INSERT INTO phones(id,organization_id,service_id,number,extension,type,description)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET organization_id=excluded.organization_id,
                          service_id=excluded.service_id,number=excluded.number,
                          extension=excluded.extension,type=excluded.type,description=excluded.description
                        """,
                        (
                            str(phone["id"]), organization_id, service_id, phone["number"],
                            phone.get("extension", ""), phone.get("type", "voice"),
                            phone.get("description", ""),
                        ),
                    )
                for schedule in service["schedules"]:
                    canonical = f"{schedule['byday']} {schedule['opens_at']}-{schedule['closes_at']}"
                    if not canonical_schedule(canonical):
                        raise ValueError(f"invalid schedule in service {service_id}")
                    connection.execute(
                        """
                        INSERT INTO schedules(id,service_id,byday,opens_at,closes_at,valid_from,valid_to,description)
                        VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET service_id=excluded.service_id,byday=excluded.byday,
                          opens_at=excluded.opens_at,closes_at=excluded.closes_at,
                          valid_from=excluded.valid_from,valid_to=excluded.valid_to,
                          description=excluded.description
                        """,
                        (
                            str(schedule["id"]), service_id, schedule["byday"], schedule["opens_at"],
                            schedule["closes_at"], schedule.get("valid_from"), schedule.get("valid_to"),
                            schedule.get("description", ""),
                        ),
                    )
                for link in service["service_at_locations"]:
                    location = link["location"]
                    address = location["addresses"][0]
                    location_id = str(location["id"])
                    connection.execute(
                        """
                        INSERT INTO locations(id,organization_id,name,address_1,city,region,postal_code,latitude,longitude)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET organization_id=excluded.organization_id,
                          name=excluded.name,address_1=excluded.address_1,city=excluded.city,
                          region=excluded.region,postal_code=excluded.postal_code,
                          latitude=excluded.latitude,longitude=excluded.longitude
                        """,
                        (
                            location_id, organization_id, location.get("name", ""), address["address_1"],
                            address["city"], address["region"], address["postal_code"],
                            location.get("latitude"), location.get("longitude"),
                        ),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO service_at_location(service_id,location_id) VALUES (?,?)",
                        (service_id, location_id),
                    )
                    connection.execute(
                        "UPDATE schedules SET location_id=? WHERE service_id=? AND location_id IS NULL",
                        (location_id, service_id),
                    )
                if service["status"] == "active":
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO verification_tasks(
                          id,organization_id,service_id,trigger,fields_json,priority,created_at
                        ) VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            new_id("task"), organization_id, service_id, "import",
                            '["schedule","phone","eligibility","status"]', 500, now,
                        ),
                    )
            self._audit(
                connection, "directory.imported", actor, "directory", "hsds",
                {"services": len(services), "organizations": len(organizations_seen), "version": "3.2"},
            )
        return {"services": len(services), "organizations": len(organizations_seen)}

    def create_consent(self, consent: ConsentRequest, actor: str) -> dict[str, Any]:
        """Persist an explicit provider call allowance used by the dialing transaction."""
        try:
            ZoneInfo(consent.timezone)
        except Exception as error:
            raise ValueError("timezone is not a valid IANA timezone") from error
        consent_id, now = new_id("consent"), utc_now()
        with self.transaction() as connection:
            organization = connection.execute(
                "SELECT id FROM organizations WHERE id=?", (consent.organization_id,)
            ).fetchone()
            if not organization:
                raise KeyError("organization not found")
            connection.execute(
                "UPDATE provider_consents SET status='withdrawn',updated_at=? "
                "WHERE organization_id=? AND phone_number=? AND status='active'",
                (now, consent.organization_id, consent.phone_number),
            )
            connection.execute(
                """
                INSERT INTO provider_consents(id,organization_id,phone_number,timezone,allowed_days_json,
                  earliest_local,latest_local,valid_from,valid_until,max_calls,recording_allowed,
                  consented_by,evidence_reference,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    consent_id, consent.organization_id, consent.phone_number, consent.timezone,
                    json.dumps(consent.allowed_days), consent.earliest_local, consent.latest_local,
                    consent.valid_from.isoformat(), consent.valid_until.isoformat(), consent.max_calls,
                    int(consent.recording_allowed), consent.consented_by, consent.evidence_reference,
                    now, now,
                ),
            )
            connection.execute(
                "UPDATE organizations SET public_phone=?,updated_at=? WHERE id=?",
                (consent.phone_number, now, consent.organization_id),
            )
            self._audit(
                connection, "consent.recorded", actor, "provider_consent", consent_id,
                {"organization_id": consent.organization_id, "valid_until": consent.valid_until.isoformat(),
                 "max_calls": consent.max_calls},
            )
        return {"id": consent_id, "status": "active"}

    def search_referrals(self, need: str, postal_code: str, limit: int) -> list[dict[str, Any]]:
        tokens = [token.lower() for token in need.split() if len(token) > 2]
        if not tokens:
            return []
        clauses = []
        parameters: list[Any] = []
        for token in tokens:
            clauses.append(
                "LOWER(s.name||' '||s.description||' '||s.taxonomy||' '||s.eligibility) LIKE ?"
            )
            parameters.append(f"%{token}%")
        candidate_limit = max(100, limit * 20)
        with self.connect() as connection:
            rows = connection.execute(
                SERVICE_SELECT
                + " WHERE s.status='active' AND (" + " OR ".join(clauses) + ")"
                + " ORDER BY CASE WHEN l.postal_code=? THEN 0 ELSE 1 END,"
                  " s.assured_date DESC LIMIT ?",
                (*parameters, postal_code, candidate_limit),
            ).fetchall()
            services = self._rows(rows)
        for item in services:
            haystack = " ".join(str(item.get(k, "")) for k in ("name", "description", "taxonomy", "eligibility")).lower()
            semantic_score = sum(
                2 if token in item["taxonomy"].lower() else 1
                for token in tokens
                if token in haystack
            )
            item["match_score"] = semantic_score
            if postal_code and item.get("postal_code") == postal_code:
                item["match_score"] += 2
            age_days = item.get("age_days") if item.get("age_days") is not None else 999
            item["freshness_label"] = "verified" if age_days <= 30 else "check before travel"
            item["semantic_match"] = semantic_score > 0
        matches = [item for item in services if item.pop("semantic_match")]
        return sorted(matches, key=lambda item: (-item["match_score"], item.get("age_days") or 999))[:limit]

    def record_referral(
        self, service_id: str, need: str, postal_code: str, channel: str = "web"
    ) -> str:
        if channel not in {"web", "voice"}:
            raise ValueError("invalid referral channel")
        referral_id = new_id("ref")
        with self.transaction() as connection:
            service = connection.execute(
                "SELECT status FROM services WHERE id=?", (service_id,)
            ).fetchone()
            if not service:
                raise KeyError("service not found")
            if service["status"] != "active":
                raise ValueError("service is not currently active")
            connection.execute(
                "INSERT INTO referrals VALUES (?,?,?,?,?,?)",
                (referral_id, service_id, need, postal_code, channel, utc_now()),
            )
            self._audit(connection, "referral.created", "intake", "service", service_id,
                        {"referral_id": referral_id, "need": need, "channel": channel})
        return referral_id

    def report_failure(
        self, service_id: str, reason: str, details: str, referral_id: str | None
    ) -> tuple[str, str]:
        report_id, task_id = new_id("fail"), new_id("task")
        fields = {"disconnected": ["phone"], "closed": ["status", "schedule"],
                  "wrong_hours": ["schedule"], "ineligible": ["eligibility"],
                  "moved": ["address"]}.get(reason, ["status", "phone", "schedule"])
        now = utc_now()
        with self.transaction() as connection:
            service = connection.execute("SELECT organization_id FROM services WHERE id=?", (service_id,)).fetchone()
            if not service:
                raise KeyError("service not found")
            if referral_id:
                referral = connection.execute(
                    "SELECT service_id FROM referrals WHERE id=?", (referral_id,)
                ).fetchone()
                if not referral:
                    raise KeyError("referral not found")
                if referral["service_id"] != service_id:
                    raise ValueError("referral does not belong to the reported service")
            connection.execute(
                "INSERT INTO failure_reports VALUES (?,?,?,?,?,?,?)",
                (report_id, service_id, referral_id, reason, details, "open", now),
            )
            existing = connection.execute(
                "SELECT id FROM verification_tasks WHERE service_id=? AND status IN ('queued','in_progress','needs_review')",
                (service_id,),
            ).fetchone()
            if existing:
                task_id = existing["id"]
                current_fields = connection.execute(
                    "SELECT fields_json FROM verification_tasks WHERE id=?", (task_id,)
                ).fetchone()["fields_json"]
                merged_fields = list(dict.fromkeys([*json.loads(current_fields), *fields]))
                connection.execute(
                    "UPDATE verification_tasks SET priority=MAX(priority,1000),trigger='seeker_failure',"
                    "fields_json=? WHERE id=?", (json.dumps(merged_fields), task_id)
                )
            else:
                connection.execute(
                    "INSERT INTO verification_tasks(id,organization_id,service_id,trigger,fields_json,priority,created_at) VALUES (?,?,?,?,?,?,?)",
                    (task_id, service["organization_id"], service_id, "seeker_failure", json.dumps(fields), 1000, now),
                )
            self._audit(connection, "failure.reported", "intake", "service", service_id,
                        {"report_id": report_id, "reason": reason, "task_id": task_id})
        return report_id, task_id

    def record_verification(
        self, task_id: str, extraction: ExtractionResult, decisions: list[PolicyDecision],
        transcript: list[dict[str, Any]], actor: str = "ontheline-agent",
        agent_trace: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        run_id, now = new_id("run"), utc_now()
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM verification_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError("task not found")
            if task["status"] != "in_progress":
                raise ValueError("task is not available for verification")
            decisions = classify_extraction(extraction, json.loads(task["fields_json"]))
            tier = overall_tier(decisions)
            pending = any(
                decision.tier in (Tier.HUMAN_DECISION, Tier.SAFETY_STOP)
                for decision in decisions
            )
            covered_fields = sorted(
                set(extraction.confirmed_fields).union(change.field for change in extraction.changes)
            )
            connection.execute(
                "INSERT INTO verification_runs(id,task_id,tier,status,disclosure_spoken,recording_consent,"
                "outcome,summary,transcript_json,agent_trace_json,covered_fields_json,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, int(tier), "needs_review" if pending else "completed",
                 int(extraction.disclosure_spoken), int(extraction.recording_consent), extraction.outcome,
                 extraction.summary, json.dumps(transcript, ensure_ascii=False),
                 json.dumps(agent_trace or [], ensure_ascii=False), json.dumps(covered_fields), now, now),
            )
            service = self._service_values(connection, task["service_id"])
            proposal_ids: list[str] = []
            for decision in decisions:
                if decision.change is None:
                    continue
                change = decision.change
                proposal_id = new_id("chg")
                proposal_ids.append(proposal_id)
                status = "applied" if decision.tier == Tier.SAFE_CHANGE and not pending else "pending"
                old_value = service.get(change.field)
                connection.execute("""
                    INSERT INTO proposed_changes(id,run_id,service_id,field_name,old_value,new_value,quote,
                      confidence,tier,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (proposal_id, run_id, task["service_id"], change.field, old_value,
                      change.new_value, change.quote, change.confidence, int(decision.tier),
                      decision.reason, status, now))
                if status == "applied":
                    self._apply_change(connection, task["service_id"], change.field, change.new_value)
                    self._audit(connection, "change.auto_applied", actor, "service", task["service_id"],
                                {"proposal_id": proposal_id, "field": change.field,
                                 "old": old_value, "new": change.new_value, "tier": 1})
            confirmed = [d.reason for d in decisions if d.tier == Tier.CONFIRMATION]
            safe_change = any(d.tier == Tier.SAFE_CHANGE for d in decisions)
            if (confirmed or safe_change) and not pending:
                connection.execute(
                    "UPDATE services SET assured_date=date('now'),last_modified=? WHERE id=?",
                    (now, task["service_id"]),
                )
            task_status = "needs_review" if pending else "completed"
            connection.execute(
                "UPDATE verification_tasks SET status=?,attempts=attempts+1,completed_at=? WHERE id=?",
                (task_status, None if pending else now, task_id),
            )
            if extraction.outcome == "stop_requested":
                connection.execute("UPDATE organizations SET do_not_call=1,updated_at=? WHERE id=?",
                                   (now, task["organization_id"]))
            review_flags = [
                decision for decision in decisions
                if decision.change is None and decision.tier in (Tier.HUMAN_DECISION, Tier.SAFETY_STOP)
            ]
            for review_flag in review_flags:
                flag_id = new_id("flag")
                connection.execute(
                    "INSERT INTO safety_flags(id,run_id,task_id,organization_id,service_id,reason,outcome,tier,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (flag_id, run_id, task_id, task["organization_id"], task["service_id"],
                     review_flag.reason, extraction.outcome, int(review_flag.tier), now),
                )
                event_type = "safety.flagged" if review_flag.tier == Tier.SAFETY_STOP else "decision.requested"
                self._audit(connection, event_type, actor, "review_flag", flag_id,
                            {"task_id": task_id, "outcome": extraction.outcome,
                             "reason": review_flag.reason, "tier": int(review_flag.tier)})
            self._audit(connection, "verification.completed", actor, "verification_run", run_id,
                        {"task_id": task_id, "tier": int(tier), "outcome": extraction.outcome,
                         "proposals": proposal_ids, "confirmed": confirmed})
            if not pending:
                connection.execute(
                    "UPDATE failure_reports SET status='resolved' WHERE service_id=? AND status='open'",
                    (task["service_id"],),
                )
        return {
            "run_id": run_id,
            "tier": int(tier),
            "status": "needs_review" if pending else "completed",
            "proposal_ids": proposal_ids,
            "policy_decisions": [
                {
                    "tier": int(item.tier),
                    "reason": item.reason,
                    "field": item.change.field if item.change else None,
                }
                for item in decisions
            ],
        }

    def _service_values(self, connection: sqlite3.Connection, service_id: str) -> dict[str, Any]:
        row = connection.execute("""
            SELECT s.*,p.number phone,
                   sc.byday||' '||sc.opens_at||'-'||sc.closes_at schedule,
                   l.address_1||', '||l.city||', '||l.region||' '||l.postal_code address
            FROM services s LEFT JOIN phones p ON p.service_id=s.id LEFT JOIN schedules sc ON sc.service_id=s.id
            LEFT JOIN service_at_location sl ON sl.service_id=s.id LEFT JOIN locations l ON l.id=sl.location_id
            WHERE s.id=?
        """, (service_id,)).fetchone()
        if not row:
            raise KeyError("service not found")
        return dict(row)

    def _apply_change(self, connection: sqlite3.Connection, service_id: str, field: str, value: str | None) -> None:
        if value is None:
            raise ValueError("On the Line never deletes a field automatically")
        direct = {"name", "description", "status", "eligibility", "fees", "application_process", "alert"}
        if field in direct:
            if field == "status" and value not in {"active", "inactive", "defunct", "temporarily closed"}:
                raise ValueError("status must use an HSDS status value")
            changed = connection.execute(
                f"UPDATE services SET {field}=?,last_modified=? WHERE id=?",
                (value, utc_now(), service_id),
            ).rowcount
            if changed != 1:
                raise KeyError("service not found")
        elif field == "phone":
            changed = connection.execute(
                "UPDATE phones SET number=? WHERE id=(SELECT id FROM phones WHERE service_id=? ORDER BY id LIMIT 1)",
                (value, service_id),
            ).rowcount
            if changed != 1:
                raise ValueError("service has no phone record to update")
            connection.execute(
                "UPDATE services SET last_modified=? WHERE id=?", (utc_now(), service_id)
            )
        elif field == "schedule":
            if not canonical_schedule(value):
                raise ValueError("schedule must use canonical form such as WE 10:00-13:00")
            match = CANONICAL_SCHEDULE.fullmatch(value)
            assert match is not None
            days, open_hour, open_minute, close_hour, close_minute = match.groups()
            byday, opens_at, closes_at = days, f"{open_hour}:{open_minute}", f"{close_hour}:{close_minute}"
            changed = connection.execute(
                "UPDATE schedules SET byday=?,opens_at=?,closes_at=?,description=? "
                "WHERE id=(SELECT id FROM schedules WHERE service_id=? ORDER BY id LIMIT 1)",
                (byday, opens_at, closes_at, value, service_id),
            ).rowcount
            if changed != 1:
                raise ValueError("service has no schedule record to update")
            connection.execute(
                "UPDATE services SET last_modified=? WHERE id=?", (utc_now(), service_id)
            )
        elif field == "address":
            changed = connection.execute("""
                UPDATE locations SET address_1=? WHERE id IN
                  (SELECT location_id FROM service_at_location WHERE service_id=? LIMIT 1)
            """, (value, service_id)).rowcount
            if changed != 1:
                raise ValueError("service has no physical location to update")
            connection.execute(
                "UPDATE services SET last_modified=? WHERE id=?", (utc_now(), service_id)
            )
        else:
            raise ValueError(f"unsupported field: {field}")

    def decisions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT c.*,s.name service_name,o.name organization_name,r.summary,r.outcome,r.transcript_json,t.id task_id
                FROM proposed_changes c JOIN services s ON s.id=c.service_id
                JOIN organizations o ON o.id=s.organization_id JOIN verification_runs r ON r.id=c.run_id
                JOIN verification_tasks t ON t.id=r.task_id WHERE c.status='pending'
                ORDER BY c.tier DESC,c.created_at
            """).fetchall()
            proposals = self._rows(rows, json_fields=("transcript_json",))
            for item in proposals:
                item["kind"] = "change"
            flag_rows = connection.execute("""
                SELECT f.id,f.run_id,f.service_id,'safety_flag' field_name,'' old_value,
                       f.outcome new_value,f.reason quote,1.0 confidence,f.tier,f.reason,
                       f.status,f.created_at,s.name service_name,o.name organization_name,
                       r.summary,r.outcome,r.transcript_json,f.task_id
                FROM safety_flags f JOIN services s ON s.id=f.service_id
                JOIN organizations o ON o.id=f.organization_id
                JOIN verification_runs r ON r.id=f.run_id WHERE f.status='open'
                ORDER BY f.created_at
            """).fetchall()
            flags = self._rows(flag_rows, json_fields=("transcript_json",))
            for item in flags:
                item["kind"] = "safety" if item["tier"] == 3 else "review"
            return sorted(proposals + flags, key=lambda item: (-item["tier"], item["created_at"]))

    def _complete_reviewed_run(
        self, connection: sqlite3.Connection, run_id: str, now: str
    ) -> bool:
        pending_changes = connection.execute(
            "SELECT COUNT(*) count FROM proposed_changes WHERE run_id=? AND status='pending'",
            (run_id,),
        ).fetchone()["count"]
        pending_flags = connection.execute(
            "SELECT COUNT(*) count FROM safety_flags WHERE run_id=? AND status='open'",
            (run_id,),
        ).fetchone()["count"]
        if pending_changes or pending_flags:
            return False
        run = connection.execute(
            "SELECT r.*,t.fields_json,t.service_id FROM verification_runs r "
            "JOIN verification_tasks t ON t.id=r.task_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if not run:
            raise KeyError("verification run not found")
        connection.execute("UPDATE verification_runs SET status='completed' WHERE id=?", (run_id,))
        connection.execute(
            "UPDATE verification_tasks SET status='completed',completed_at=? WHERE id=?",
            (now, run["task_id"]),
        )
        rejected = connection.execute(
            "SELECT COUNT(*) count FROM proposed_changes WHERE run_id=? AND status='rejected'",
            (run_id,),
        ).fetchone()["count"]
        required = set(json.loads(run["fields_json"]))
        covered = set(json.loads(run["covered_fields_json"]))
        if run["outcome"] == "complete" and run["tier"] < 3 and not rejected and required <= covered:
            connection.execute(
                "UPDATE services SET assured_date=date('now'),last_modified=? WHERE id=?",
                (now, run["service_id"]),
            )
            connection.execute(
                "UPDATE failure_reports SET status='resolved' WHERE service_id=? AND status='open'",
                (run["service_id"],),
            )
        return True

    def resolve_decision(
        self, proposal_id: str, action: str, user_id: str, edited_value: str | None, note: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            proposal = connection.execute(
                "SELECT * FROM proposed_changes WHERE id=? AND status='pending'", (proposal_id,)
            ).fetchone()
            if not proposal:
                flag = connection.execute(
                    "SELECT * FROM safety_flags WHERE id=? AND status='open'", (proposal_id,)
                ).fetchone()
                if not flag:
                    raise KeyError("pending decision not found")
                if action != "acknowledge":
                    raise ValueError("safety reviews must be acknowledged")
                connection.execute(
                    "UPDATE safety_flags SET status='acknowledged',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                    (user_id, now, note, proposal_id),
                )
                self._complete_reviewed_run(connection, flag["run_id"], now)
                self._audit(connection, "safety.acknowledged", user_id, "safety_flag", proposal_id,
                            {"outcome": flag["outcome"], "note": note})
                return {"id": proposal_id, "status": "acknowledged", "value": flag["outcome"]}
            if action == "acknowledge":
                raise ValueError("change proposals require approve, reject, or edit")
            applied_value = edited_value if action == "edit" else proposal["new_value"]
            if action in ("approve", "edit"):
                if applied_value is None:
                    raise ValueError("Removing a value requires an explicit replacement; deletion is blocked")
                self._apply_change(connection, proposal["service_id"], proposal["field_name"], applied_value)
            status = {"approve": "approved", "reject": "rejected", "edit": "edited"}[action]
            connection.execute(
                "UPDATE proposed_changes SET status=?,new_value=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                (status, applied_value, user_id, now, note, proposal_id),
            )
            self._complete_reviewed_run(connection, proposal["run_id"], now)
            self._audit(connection, f"decision.{status}", user_id, "proposed_change", proposal_id,
                        {"field": proposal["field_name"], "value": applied_value, "note": note})
            return {"id": proposal_id, "status": status, "value": applied_value}

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
            return self._rows(rows, json_fields=("payload_json",))

    def verify_audit_chain(self) -> bool:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events ORDER BY sequence").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            expected = hashlib.sha256(
                f"{previous}|{row['event_type']}|{row['actor']}|{row['entity_type']}|{row['entity_id']}|{row['payload_json']}|{row['created_at']}".encode()
            ).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous = expected
        return True
