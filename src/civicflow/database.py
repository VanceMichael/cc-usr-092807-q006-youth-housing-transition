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

-- 住房保障子域：全部历史只追加保存，事实（租期、缴费、轮候、成员照料）不因资格变化被重写。

-- 家庭与成员关系（按生效时间保存，离职/关系结束只写 valid_to，从不删除）
CREATE TABLE IF NOT EXISTS hh_families (
    family_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hh_members (
    member_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    role TEXT NOT NULL,
    needs_care INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(family_id, person_id, valid_from)
);
CREATE INDEX IF NOT EXISTS hh_members_family ON hh_members(family_id, valid_from, valid_to);

-- 房源项目与房间
CREATE TABLE IF NOT EXISTS hh_projects (
    project_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hh_rooms (
    room_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,              -- available | reserved | occupied | releasing | out_of_order
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, code)
);
CREATE INDEX IF NOT EXISTS hh_rooms_status ON hh_rooms(project_id, status);

-- 证明：同一内容（按摘要）对同一人只产生一次资格；内容冲突进入 conflict 并暂停相关申请
CREATE TABLE IF NOT EXISTS hh_documents (
    document_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,           -- employment | social_security | income | housing_difficulty | family
    doc_subtype TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    state TEXT NOT NULL               -- active | superseded | conflicted
);
CREATE INDEX IF NOT EXISTS hh_documents_lookup ON hh_documents(person_id, doc_type, content_digest);
CREATE TABLE IF NOT EXISTS hh_document_conflicts (
    conflict_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    existing_document_id TEXT NOT NULL,
    incoming_document_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_doc_conflicts_open ON hh_document_conflicts(person_id, resolved_at);

-- 申请：同一个人同一类保障同一轮只能有一个活跃申请；缺件/incomplete、冲突 suspended、定稿 finalized
CREATE TABLE IF NOT EXISTS hh_applications (
    application_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    program TEXT NOT NULL,          -- station | affordable_rental | public_rental | settled
    round_id TEXT NOT NULL,
    state TEXT NOT NULL,            -- draft | incomplete | pending | suspended | approved | rejected | withdrawn
    version INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    suspension_reason TEXT NOT NULL DEFAULT '',
    missing_docs TEXT NOT NULL DEFAULT '',
    eligible_version INTEGER NOT NULL DEFAULT 0,
    decision_by TEXT NOT NULL DEFAULT '',
    decision_at TEXT,
    decision_reason TEXT NOT NULL DEFAULT '',
    prev_application_id TEXT,
    wait_time_before_days INTEGER NOT NULL DEFAULT 0,
    carrying_text TEXT NOT NULL DEFAULT '',
    factors_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS hh_apps_family ON hh_applications(family_id, recorded_at);
CREATE INDEX IF NOT EXISTS hh_apps_active ON hh_applications(applicant_id, program, state);
CREATE INDEX IF NOT EXISTS hh_apps_round ON hh_applications(round_id, state);

-- 资格认定：每次决定只对“后续安排”生效；历史租约与缴费事实永不重写
CREATE TABLE IF NOT EXISTS hh_eligibility_decisions (
    decision_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    program TEXT NOT NULL,
    outcome TEXT NOT NULL,          -- granted | denied
    effective_from TEXT NOT NULL,   -- 只影响该时间之后的安排
    policy_version TEXT NOT NULL,
    criteria_snapshot_json TEXT NOT NULL,
    factors_json TEXT NOT NULL,
    carry_wait_days INTEGER NOT NULL DEFAULT 0,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_elig_app ON hh_eligibility_decisions(application_id);
CREATE INDEX IF NOT EXISTS hh_elig_family ON hh_eligibility_decisions(family_id, effective_from);

-- 租约：已履行事实不可变；同一房间时间窗不重叠以防重复占用
CREATE TABLE IF NOT EXISTS hh_leases (
    lease_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    program TEXT NOT NULL,
    project_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    state TEXT NOT NULL,            -- pending | active | expiring | ended | released
    version INTEGER NOT NULL,
    application_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_leases_room_time ON hh_leases(room_id, start_at, end_at);
CREATE INDEX IF NOT EXISTS hh_leases_family ON hh_leases(family_id, start_at);

-- 租金记录与减免：减免仅对减免生效日之后的账期生效，绝不追溯改写已缴事实
CREATE TABLE IF NOT EXISTS hh_rent_records (
    rent_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    list_rent_minor INTEGER NOT NULL,
    due_minor INTEGER,
    reduction_id TEXT,
    paid_minor INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    paid_by TEXT NOT NULL DEFAULT '',
    billed_by TEXT NOT NULL,
    billed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_rent_lease ON hh_rent_records(lease_id, period_start);
CREATE TABLE IF NOT EXISTS hh_reductions (
    reduction_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    percent INTEGER NOT NULL,       -- 0..100
    effective_from TEXT NOT NULL,   -- 只影响该日及之后账期
    state TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT
);
CREATE INDEX IF NOT EXISTS hh_reductions_lease ON hh_reductions(lease_id, effective_from);

-- 交接：换房与退租必须先完成交接，房间才释放、下一租约/递补才能推进
CREATE TABLE IF NOT EXISTS hh_handovers (
    handover_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    from_lease_id TEXT NOT NULL,
    to_lease_id TEXT,               -- 换房时指向新租约；退租时为空
    kind TEXT NOT NULL,            -- transfer | exit
    state TEXT NOT NULL,           -- pending | completed
    checklist_json TEXT NOT NULL,  -- 钥匙、费用结清、房屋查验等
    scheduled_at TEXT NOT NULL,
    completed_at TEXT,
    completed_by TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_handovers_from ON hh_handovers(from_lease_id, state);

-- 轮候：冻结的排序规则；offer 未应答过期后仍保留原顺位
CREATE TABLE IF NOT EXISTS hh_waitlists (
    waitlist_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    project_id TEXT,
    round_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    ranking_rule_json TEXT NOT NULL,  -- 冻结的排序规则（入队时快照）
    frozen_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hh_waitlist_entries (
    entry_id TEXT PRIMARY KEY,
    waitlist_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    rank INTEGER NOT NULL,           -- 入队时冻结的顺位
    enqueued_at TEXT NOT NULL,
    base_since TEXT NOT NULL,        -- 轮候起算时间（可携带更早保障的轮候时间）
    state TEXT NOT NULL,             -- waiting | offered | admitted | skipped | withdrawn
    offer_lease_id TEXT,
    offer_expires_at TEXT,
    offered_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(waitlist_id, family_id)
);
CREATE INDEX IF NOT EXISTS hh_wait_entries_list ON hh_waitlist_entries(waitlist_id, rank, state);
CREATE TABLE IF NOT EXISTS hh_waitlist_events (
    event_id TEXT PRIMARY KEY,
    waitlist_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- enqueued | offered | offer_expired | admitted | skipped
    rank_snapshot INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hh_wait_events ON hh_waitlist_events(waitlist_id, occurred_at);
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
