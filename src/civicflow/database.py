"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);
CREATE TABLE IF NOT EXISTS hs_families (
    family_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hs_family_versions (
    family_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(family_id, version)
);
CREATE TABLE IF NOT EXISTS hs_relations (
    relation_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    role TEXT NOT NULL,
    care_need TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    revoked_at TEXT,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_relation_family ON hs_relations(family_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS hs_relation_person ON hs_relations(person_id, valid_from, valid_to);
CREATE TABLE IF NOT EXISTS hs_projects (
    project_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    name TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hs_rooms (
    room_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    monthly_rent_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_room_project ON hs_rooms(project_id, state);
CREATE TABLE IF NOT EXISTS hs_documents (
    document_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS hs_document_dedup ON hs_documents(family_id, dedup_key);
CREATE TABLE IF NOT EXISTS hs_document_conflicts (
    conflict_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    existing_document_id TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    incoming_payload_json TEXT NOT NULL,
    application_ids_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS hs_doc_conflict_family ON hs_document_conflicts(family_id, state);
CREATE TABLE IF NOT EXISTS hs_applications (
    application_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    program TEXT NOT NULL,
    project_id TEXT NOT NULL,
    state TEXT NOT NULL,
    missing_docs_json TEXT NOT NULL,
    pause_reason TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    eligibility_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_application_family ON hs_applications(family_id, state);
CREATE TABLE IF NOT EXISTS hs_eligibility (
    eligibility_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    program TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    fact_snapshot_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    superseded_by TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_eligibility_person ON hs_eligibility(person_id, program, effective_from);
CREATE TABLE IF NOT EXISTS hs_waitlists (
    waitlist_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    program TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    ranking_json TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hs_waitlist_entries (
    waitlist_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    carry_days INTEGER NOT NULL,
    care_bonus INTEGER NOT NULL,
    state TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    placed_at TEXT,
    left_at TEXT,
    application_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(waitlist_id, family_id)
);
CREATE INDEX IF NOT EXISTS hs_wait_entry_family ON hs_waitlist_entries(family_id, state);
CREATE TABLE IF NOT EXISTS hs_leases (
    lease_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    program TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    monthly_rent_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    handover_state TEXT NOT NULL DEFAULT '',
    handover_completed_at TEXT,
    predecessor_lease_id TEXT NOT NULL DEFAULT '',
    eligibility_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS hs_lease_room_active ON hs_leases(room_id) WHERE state IN ('prepared','active','ending');
CREATE UNIQUE INDEX IF NOT EXISTS hs_lease_family_active ON hs_leases(family_id) WHERE state IN ('prepared','active','ending');
CREATE INDEX IF NOT EXISTS hs_lease_person ON hs_leases(person_id, state);
CREATE TABLE IF NOT EXISTS hs_payments (
    payment_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    period TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    kind TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    paid_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS hs_payment_once ON hs_payments(lease_id, period, kind);
CREATE INDEX IF NOT EXISTS hs_payment_family ON hs_payments(family_id, paid_at);
CREATE TABLE IF NOT EXISTS hs_rent_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    reduction_minor INTEGER NOT NULL,
    effective_from_period TEXT NOT NULL,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_adjustment_lease ON hs_rent_adjustments(lease_id, state);
CREATE TABLE IF NOT EXISTS hs_handovers (
    handover_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    room_id TEXT NOT NULL,
    target_room_id TEXT NOT NULL DEFAULT '',
    checklist_json TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    completed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_handover_lease ON hs_handovers(lease_id, state);
CREATE TABLE IF NOT EXISTS hs_exceptions (
    exception_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS hs_exception_family ON hs_exceptions(family_id, state);
CREATE TABLE IF NOT EXISTS hs_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hs_event_family ON hs_events(family_id, occurred_at, event_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
