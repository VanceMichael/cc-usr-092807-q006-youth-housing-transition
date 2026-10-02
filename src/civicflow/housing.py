"""住房保障资格迁移领域服务。

承载一名青年从筑梦驿站、保障性租赁住房、公共租赁住房到安居房的
连续保障轨迹：家庭关系、房源项目与房间、证明材料、申请与资格认定、
轮候、租约、缴费、租金减免和退出交接都按生效时间保存。

核心约束：
- 资格变化只影响后续安排；已履行租期、缴费事实永不重写。
- 同一证明重复提交不产生第二次资格；内容冲突暂停相关申请。
- 换房与退租先完成交接才释放房源；候补递补按冻结名次。
- 经办人不能审批自己录入的例外；申请人只能查看本家庭资料。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Mapping

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .housing_policy import (
    PROGRAM_LABELS,
    PROGRAMS,
    care_bonus_days,
    evaluate,
    merge_policy,
    policy_digest,
)
from .identifiers import new_id
from .jsonutil import canonical_json, digest_json
from .ledger import to_minor
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant

STAFF_SCOPE = "*"

ROOM_STATES = ("available", "reserved", "occupied")


def _month_iter(start_period: str, end_period_exclusive: str) -> list[str]:
    year, month = (int(x) for x in start_period.split("-"))
    end_year, end_month = (int(x) for x in end_period_exclusive.split("-"))
    result: list[str] = []
    while (year, month) < (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return result


def _periods_through(start_at: str, end_instant: str) -> list[str]:
    """入住月到结束时点已经起算的账期；结束日恰为月初时当月不计。"""
    end_dt = parse_instant(end_instant)
    exclusive_month = end_dt.month if end_dt.day == 1 else end_dt.month + 1
    exclusive_year = end_dt.year
    if exclusive_month == 13:
        exclusive_month = 1
        exclusive_year += 1
    return _month_iter(start_at[:7], f"{exclusive_year:04d}-{exclusive_month:02d}")


def _add_days(instant: str, days: int) -> str:
    return (parse_instant(instant) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _period_cmp(a: str, b: str) -> int:
    ay, am = (int(x) for x in a.split("-"))
    by, bm = (int(x) for x in b.split("-"))
    return (ay * 12 + am) - (by * 12 + bm)


def _amount_minor(value: str | int) -> int:
    """金额入参：整数视为分，字符串视为元（如 "500.00"）。"""
    if isinstance(value, int):
        return value
    return to_minor(value)


@dataclass(frozen=True)
class HousingService:
    database: Database
    clock: Clock

    # ------------------------------------------------------------------ 基础

    def _staff(self, context: AccessContext, permission: str) -> None:
        context.require(permission)
        if context.scopes != frozenset({STAFF_SCOPE}) and STAFF_SCOPE not in context.scopes:
            raise PermissionDenied("该操作仅面向住房保障经办机构")

    def _family_scope(self, context: AccessContext, family_id: str) -> None:
        context.require("read:housing")
        if STAFF_SCOPE not in context.scopes and f"family:{family_id}" not in context.scopes:
            raise PermissionDenied("只能访问本家庭的住房保障资料")

    def _event(self, connection, *, family_id: str, event_type: str, detail: dict, actor: str) -> None:
        connection.execute(
            "INSERT INTO hs_events(family_id,occurred_at,event_type,detail_json,actor_id) VALUES(?,?,?,?,?)",
            (family_id, self.clock.now(), event_type, canonical_json(detail), actor))

    def _audit(self, connection, *, actor: str, action: str, entity_id: str, version: int, detail: dict) -> None:
        AuditLog(self.clock).append(connection, actor_id=actor, action=action,
                                   entity_type="housing", entity_id=entity_id,
                                   version=version, detail=detail)

    def _job_once(self, connection, *, job_id: str, job_type: str, subject_id: str, run_at: str, payload: dict) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO scheduled_jobs(job_id,job_type,subject_id,run_at,payload_json,status) "
            "VALUES(?,?,?,?,?,'waiting')",
            (job_id, job_type, subject_id, canonical_instant(run_at), canonical_json(payload)))

    def _row(self, connection, sql: str, params: Iterable[Any] = ()):
        row = connection.execute(sql, tuple(params)).fetchone()
        if not row:
            raise NotFoundError("记录不存在")
        return dict(row)

    # ------------------------------------------------------------------ 家庭

    def create_family(self, context: AccessContext, *, applicant_id: str, label: str, request_key: str) -> dict:
        self._staff(context, "write:housing")
        applicant_id = applicant_id.strip()
        label = label.strip()
        if not applicant_id or not label:
            raise ValidationError("家庭申请人和称谓不能为空")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT family_id FROM hs_relations WHERE person_id=? AND (valid_to IS NULL OR valid_to>?)",
                (applicant_id, self.clock.now())).fetchone()
            if existing:
                raise ConflictError("该申请人已经属于一个在册家庭")
            family_id = new_id("family")
            now = self.clock.now()
            payload = {"label": label, "applicant_id": applicant_id}
            connection.execute(
                "INSERT INTO hs_families(family_id,version,state,payload_json,created_at,updated_at,created_by,updated_by) "
                "VALUES(?,1,'active',?,?,?,?,?)",
                (family_id, canonical_json(payload), now, now, context.actor_id, context.actor_id))
            connection.execute(
                "INSERT INTO hs_family_versions(family_id,version,state,payload_json,valid_from,actor_id,request_key) "
                "VALUES(?,1,'active',?,?,?,?)",
                (family_id, canonical_json(payload), now, context.actor_id, request_key))
            connection.execute(
                "INSERT INTO hs_relations(relation_id,family_id,person_id,role,care_need,valid_from,created_by) "
                "VALUES(?,?,?,'applicant','',?,?)",
                (new_id("rel"), family_id, applicant_id, now, context.actor_id))
            self._event(connection, family_id=family_id, event_type="family.created",
                        detail={"applicant_id": applicant_id, "label": label}, actor=context.actor_id)
            self._audit(connection, actor=context.actor_id, action="housing.family.create",
                        entity_id=family_id, version=1, detail=payload)
            return self._family_view(connection, dict(connection.execute(
                "SELECT * FROM hs_families WHERE family_id=?", (family_id,)).fetchone()))

    def add_member(self, context: AccessContext, family_id: str, *, person_id: str, role: str,
                   care_need: str = "", valid_from: str | None = None, request_key: str) -> dict:
        self._staff(context, "write:housing")
        if role not in ("applicant", "spouse", "child", "elder", "dependent", "other"):
            raise ValidationError("家庭关系类型不合法")
        valid_from = canonical_instant(valid_from or self.clock.now())
        with self.database.transaction() as connection:
            family = self._row(connection, "SELECT * FROM hs_families WHERE family_id=?", (family_id,))
            dup = connection.execute(
                "SELECT relation_id FROM hs_relations WHERE family_id=? AND person_id=? AND (valid_to IS NULL OR valid_to>?)",
                (family_id, person_id, valid_from)).fetchone()
            if dup:
                raise ConflictError("该成员已在家庭关系中")
            relation_id = new_id("rel")
            connection.execute(
                "INSERT INTO hs_relations(relation_id,family_id,person_id,role,care_need,valid_from,created_by) "
                "VALUES(?,?,?,?,?,?,?)",
                (relation_id, family_id, person_id.strip(), role.strip(), care_need.strip(), valid_from, context.actor_id))
            self._bump_family(connection, family, actor=context.actor_id, request_key=request_key,
                              change={"member_added": person_id, "role": role, "care_need": care_need,
                                      "valid_from": valid_from})
            self._event(connection, family_id=family_id, event_type="family.member_added",
                        detail={"person_id": person_id, "role": role, "care_need": care_need,
                                "valid_from": valid_from}, actor=context.actor_id)
            return self._family_view(connection, dict(connection.execute(
                "SELECT * FROM hs_families WHERE family_id=?", (family_id,)).fetchone()))

    def end_member(self, context: AccessContext, family_id: str, *, person_id: str,
                   valid_to: str, reason: str, request_key: str) -> dict:
        self._staff(context, "write:housing")
        valid_to = canonical_instant(valid_to)
        with self.database.transaction() as connection:
            family = self._row(connection, "SELECT * FROM hs_families WHERE family_id=?", (family_id,))
            changed = connection.execute(
                "UPDATE hs_relations SET valid_to=?,revoked_at=? WHERE family_id=? AND person_id=? "
                "AND (valid_to IS NULL OR valid_to>?)",
                (valid_to, self.clock.now(), family_id, person_id, valid_to)).rowcount
            if changed != 1:
                raise ConflictError("该成员当前不在家庭关系中")
            self._bump_family(connection, family, actor=context.actor_id, request_key=request_key,
                              change={"member_ended": person_id, "valid_to": valid_to, "reason": reason})
            self._event(connection, family_id=family_id, event_type="family.member_ended",
                        detail={"person_id": person_id, "valid_to": valid_to, "reason": reason},
                        actor=context.actor_id)
            return self._family_view(connection, dict(connection.execute(
                "SELECT * FROM hs_families WHERE family_id=?", (family_id,)).fetchone()))

    def _bump_family(self, connection, family: dict, *, actor: str, request_key: str, change: dict) -> None:
        version = int(family["version"]) + 1
        payload = json.loads(family["payload_json"])
        now = self.clock.now()
        connection.execute(
            "UPDATE hs_families SET version=?,payload_json=?,updated_at=?,updated_by=? WHERE family_id=?",
            (version, canonical_json(payload), now, actor, family["family_id"]))
        connection.execute(
            "INSERT INTO hs_family_versions(family_id,version,state,payload_json,valid_from,actor_id,request_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (family["family_id"], version, family["state"], canonical_json(payload), now, actor, request_key))
        self._audit(connection, actor=actor, action="housing.family.update",
                    entity_id=family["family_id"], version=version, detail=change)

    def get_family(self, context: AccessContext, family_id: str) -> dict:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            family = self._row(connection, "SELECT * FROM hs_families WHERE family_id=?", (family_id,))
            return self._family_view(connection, family)

    def list_families(self, context: AccessContext, *, limit: int = 100) -> list[dict]:
        context.require("read:housing")
        if STAFF_SCOPE not in context.scopes:
            raise PermissionDenied("仅经办机构可以列举家庭")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_families ORDER BY created_at,family_id LIMIT ?", (limit,)).fetchall()
            return [self._family_view(connection, dict(row)) for row in rows]

    def _family_view(self, connection, family: dict) -> dict:
        members = [dict(row) for row in connection.execute(
            "SELECT * FROM hs_relations WHERE family_id=? ORDER BY valid_from,person_id", (family["family_id"],))]
        payload = json.loads(family["payload_json"])
        return {"family_id": family["family_id"], "state": family["state"], "version": family["version"],
                "created_at": family["created_at"], "updated_at": family["updated_at"], **payload,
                "members": members}

    def _members_at(self, connection, family_id: str, at: str) -> list[dict]:
        instant = canonical_instant(at)
        return [dict(row) for row in connection.execute(
            "SELECT * FROM hs_relations WHERE family_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) "
            "ORDER BY person_id", (family_id, instant, instant))]

    def _care_count(self, connection, family_id: str, at: str) -> int:
        return sum(1 for m in self._members_at(connection, family_id, at)
                   if m["role"] in ("child", "elder", "dependent") and m["care_need"])

    def _assert_person_in_family(self, connection, family_id: str, person_id: str, at: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM hs_relations WHERE family_id=? AND person_id=? AND valid_from<=? "
            "AND (valid_to IS NULL OR valid_to>?)",
            (family_id, person_id, canonical_instant(at), canonical_instant(at))).fetchone()
        if not row:
            raise ValidationError("该人员在此时点不属于申请家庭")

    # -------------------------------------------------------------- 房源与房间

    def register_project(self, context: AccessContext, *, program: str, name: str,
                         policy_override: Mapping[str, Any] | None = None, request_key: str) -> dict:
        self._staff(context, "write:housing")
        if program not in PROGRAMS:
            raise ValidationError("未知住房保障类型")
        name = name.strip()
        if not name:
            raise ValidationError("项目名称不能为空")
        policy = merge_policy(program, dict(policy_override or {}))
        with self.database.transaction() as connection:
            dup_name = connection.execute("SELECT 1 FROM hs_projects WHERE name=?", (name,)).fetchone()
            if dup_name:
                raise ConflictError("同名房源项目已经存在")
            project_id = new_id("project")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO hs_projects(project_id,program,name,policy_digest,policy_json,state,version,"
                "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?, 'active',1,?,?,?,?)",
                (project_id, program, name, policy_digest(policy), canonical_json(policy),
                 now, now, context.actor_id, context.actor_id))
            self._audit(connection, actor=context.actor_id, action="housing.project.register",
                        entity_id=project_id, version=1, detail={"program": program, "name": name})
            return self._project_view(connection, project_id)

    def revise_project_policy(self, context: AccessContext, project_id: str, *,
                              override: Mapping[str, Any], reason: str, request_key: str) -> dict:
        """新版本政策即时生效；已经作出的认定保留旧 policy_digest。"""
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            row = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?", (project_id,))
            old = json.loads(row["policy_json"])
            policy = merge_policy(row["program"], {**old, **dict(override)})
            if policy_digest(policy) == row["policy_digest"]:
                raise ConflictError("政策内容没有变化")
            version = int(row["version"]) + 1
            now = self.clock.now()
            connection.execute(
                "UPDATE hs_projects SET policy_digest=?,policy_json=?,version=?,updated_at=?,updated_by=? "
                "WHERE project_id=?",
                (policy_digest(policy), canonical_json(policy), version, now, context.actor_id, project_id))
            self._audit(connection, actor=context.actor_id, action="housing.project.policy_revise",
                        entity_id=project_id, version=version,
                        detail={"reason": reason, "old_digest": row["policy_digest"],
                                "new_digest": policy_digest(policy)})
            return self._project_view(connection, project_id)

    def get_project(self, context: AccessContext, project_id: str) -> dict:
        self._staff(context, "read:housing")
        with self.database.connect() as connection:
            return self._project_view(connection, project_id)

    def _project_view(self, connection, project_id: str) -> dict:
        row = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?", (project_id,))
        policy = json.loads(row.pop("policy_json"))
        result = dict(row)
        result["policy"] = policy
        rooms = connection.execute("SELECT * FROM hs_rooms WHERE project_id=? ORDER BY label",
                                   (project_id,)).fetchall()
        result["rooms"] = [dict(r) for r in rooms]
        return result

    def add_room(self, context: AccessContext, project_id: str, *, label: str,
                 monthly_rent: str | int, request_key: str) -> dict:
        self._staff(context, "write:housing")
        rent = _amount_minor(monthly_rent)
        if rent <= 0:
            raise ValidationError("月租金必须大于零")
        with self.database.transaction() as connection:
            self._row(connection, "SELECT project_id FROM hs_projects WHERE project_id=?", (project_id,))
            dup = connection.execute("SELECT 1 FROM hs_rooms WHERE project_id=? AND label=?",
                                     (project_id, label.strip())).fetchone()
            if dup:
                raise ConflictError("项目内房间编号重复")
            room_id = new_id("room")
            connection.execute(
                "INSERT INTO hs_rooms(room_id,project_id,label,monthly_rent_minor,state,version,created_by) "
                "VALUES(?,?,?,?,'available',1,?)",
                (room_id, project_id, label.strip(), rent, context.actor_id))
            return self._get_room(connection, room_id)

    def _get_room(self, connection, room_id: str) -> dict:
        return dict(self._row(connection, "SELECT * FROM hs_rooms WHERE room_id=?", (room_id,)))

    def get_room_state(self, room_id: str) -> str:
        """供经办与恢复流程读取房间当前状态。"""
        with self.database.connect() as connection:
            return self._get_room(connection, room_id)["state"]

    def list_rooms(self, context: AccessContext, *, program: str | None = None,
                   state: str | None = None) -> list[dict]:
        self._staff(context, "read:housing")
        sql = ("SELECT r.* FROM hs_rooms r JOIN hs_projects p ON p.project_id=r.project_id WHERE 1=1")
        params: list[Any] = []
        if program:
            sql += " AND p.program=?"; params.append(program)
        if state:
            sql += " AND r.state=?"; params.append(state)
        sql += " ORDER BY p.name,r.label"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def _set_room_state(self, connection, room_id: str, state: str) -> None:
        if state not in ROOM_STATES:
            raise ValidationError("房间状态不合法")
        connection.execute("UPDATE hs_rooms SET state=?,version=version+1 WHERE room_id=?", (state, room_id))

    # ------------------------------------------------------------ 证明材料

    def submit_document(self, context: AccessContext, family_id: str, *, person_id: str, doc_type: str,
                        dedup_key: str, payload: Mapping[str, Any], request_key: str) -> dict:
        """提交证明。同键同内容幂等返回已有材料；同键不同内容挂起冲突并暂停申请。"""
        self._staff(context, "write:housing")
        doc_type = doc_type.strip(); dedup_key = dedup_key.strip()
        if not doc_type or not dedup_key:
            raise ValidationError("证明类型与去重键不能为空")
        body = dict(payload)
        digest = digest_json(body)
        now = self.clock.now()
        with self.database.transaction() as connection:
            self._row(connection, "SELECT family_id FROM hs_families WHERE family_id=?", (family_id,))
            self._assert_person_in_family(connection, family_id, person_id, now)
            existing = connection.execute(
                "SELECT * FROM hs_documents WHERE family_id=? AND dedup_key=?",
                (family_id, dedup_key)).fetchone()
            if existing:
                if existing["content_digest"] == digest and existing["state"] != "superseded":
                    result = dict(existing)
                    result["acceptance"] = "duplicate"
                    return result
                if existing["content_digest"] == digest:
                    # 内容相同但旧件已被替代：恢复为有效材料，不新增资格效力。
                    connection.execute(
                        "UPDATE hs_documents SET state='received',version=version+1,updated_by=? WHERE document_id=?",
                        (context.actor_id, existing["document_id"]))
                    result = dict(connection.execute(
                        "SELECT * FROM hs_documents WHERE document_id=?", (existing["document_id"],)).fetchone())
                    result["acceptance"] = "duplicate"
                    return result
                return self._open_doc_conflict(connection, family_id=family_id, person_id=person_id,
                                               doc_type=doc_type, dedup_key=dedup_key, body=body,
                                               digest=digest, existing=dict(existing), actor=context.actor_id)
            document_id = new_id("doc")
            connection.execute(
                "INSERT INTO hs_documents(document_id,family_id,person_id,doc_type,dedup_key,content_digest,"
                "payload_json,state,version,submitted_at,created_by,updated_by) "
                "VALUES(?,?,?,?,?,?,?, 'received',1,?,?,?)",
                (document_id, family_id, person_id, doc_type, dedup_key, digest,
                 canonical_json(body), now, context.actor_id, context.actor_id))
            self._event(connection, family_id=family_id, event_type="document.submitted",
                        detail={"document_id": document_id, "doc_type": doc_type,
                                "person_id": person_id, "dedup_key": dedup_key}, actor=context.actor_id)
            result = dict(connection.execute(
                "SELECT * FROM hs_documents WHERE document_id=?", (document_id,)).fetchone())
            result["acceptance"] = "accepted"
            return result

    def _open_doc_conflict(self, connection, *, family_id: str, person_id: str, doc_type: str,
                           dedup_key: str, body: dict, digest: str, existing: dict, actor: str) -> dict:
        open_conflict = connection.execute(
            "SELECT * FROM hs_document_conflicts WHERE family_id=? AND dedup_key=? AND state='open'",
            (family_id, dedup_key)).fetchone()
        if open_conflict:
            result = dict(open_conflict)
            result["acceptance"] = "conflict_open"
            return result
        conflict_id = new_id("docconflict")
        now = self.clock.now()
        application_ids = [r["application_id"] for r in connection.execute(
            "SELECT application_id FROM hs_applications WHERE family_id=? AND state IN ('received','ready')",
            (family_id,)).fetchall()]
        connection.execute(
            "INSERT INTO hs_document_conflicts(conflict_id,family_id,person_id,doc_type,dedup_key,"
            "existing_document_id,incoming_digest,incoming_payload_json,application_ids_json,state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'open',?)",
            (conflict_id, family_id, person_id, doc_type, dedup_key, existing["document_id"],
             digest, canonical_json(body), canonical_json(application_ids), now))
        connection.execute(
            "UPDATE hs_applications SET state='paused',pause_reason=?,updated_at=? "
            "WHERE family_id=? AND state IN ('received','ready')",
            (f"证明冲突待裁定: {dedup_key}", now, family_id))
        self._event(connection, family_id=family_id, event_type="document.conflict_opened",
                    detail={"conflict_id": conflict_id, "dedup_key": dedup_key,
                            "doc_type": doc_type, "applications": application_ids}, actor=actor)
        result = dict(connection.execute(
            "SELECT * FROM hs_document_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone())
        result["acceptance"] = "conflict"
        return result

    def verify_document(self, context: AccessContext, document_id: str, *, request_key: str) -> dict:
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            doc = self._row(connection, "SELECT * FROM hs_documents WHERE document_id=?", (document_id,))
            if doc["state"] == "verified":
                return doc
            if doc["state"] != "received":
                raise ConflictError("只有待验材料可以核验")
            connection.execute(
                "UPDATE hs_documents SET state='verified',version=version+1,verified_at=?,updated_by=? "
                "WHERE document_id=?",
                (self.clock.now(), context.actor_id, document_id))
            self._event(connection, family_id=doc["family_id"], event_type="document.verified",
                        detail={"document_id": document_id, "doc_type": doc["doc_type"]},
                        actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_documents WHERE document_id=?", (document_id,)).fetchone())

    def resolve_document_conflict(self, context: AccessContext, conflict_id: str, *,
                                  resolution: str, note: str = "", request_key: str) -> dict:
        """冲突裁定后恢复相关申请；保留原证明或以前来内容替换，全过程留痕。"""
        self._staff(context, "write:housing")
        if resolution not in ("keep_existing", "use_incoming"):
            raise ValidationError("裁定必须是 keep_existing 或 use_incoming")
        with self.database.transaction() as connection:
            conflict = self._row(connection,
                                 "SELECT * FROM hs_document_conflicts WHERE conflict_id=?", (conflict_id,))
            if conflict["state"] != "open":
                raise ConflictError("该证明冲突已经裁定")
            new_doc_id = ""
            if resolution == "use_incoming":
                payload = json.loads(conflict["incoming_payload_json"])
                new_doc_id = new_id("doc")
                connection.execute(
                    "UPDATE hs_documents SET state='superseded',version=version+1,updated_by=? WHERE document_id=?",
                    (context.actor_id, conflict["existing_document_id"]))
                connection.execute(
                    "INSERT INTO hs_documents(document_id,family_id,person_id,doc_type,dedup_key,content_digest,"
                    "payload_json,state,version,submitted_at,created_by,updated_by) "
                    "VALUES(?,?,?,?,?,?,?, 'received',1,?,?,?)",
                    (new_doc_id, conflict["family_id"], conflict["person_id"], conflict["doc_type"],
                     conflict["dedup_key"], conflict["incoming_digest"],
                     conflict["incoming_payload_json"], self.clock.now(),
                     context.actor_id, context.actor_id))
            connection.execute(
                "UPDATE hs_document_conflicts SET state='resolved',resolved_at=?,resolution=? WHERE conflict_id=?",
                (self.clock.now(), f"{resolution}:{note.strip()}", conflict_id))
            self._resume_applications(connection, conflict["family_id"], actor=context.actor_id)
            self._event(connection, family_id=conflict["family_id"], event_type="document.conflict_resolved",
                        detail={"conflict_id": conflict_id, "resolution": resolution,
                                "new_document_id": new_doc_id}, actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_document_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone())

    def list_documents(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_documents WHERE family_id=? ORDER BY submitted_at,document_id",
                (family_id,)).fetchall()
            return [dict(row) for row in rows]

    def list_conflicts(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_document_conflicts WHERE family_id=? ORDER BY created_at",
                (family_id,)).fetchall()
            return [dict(row) for row in rows]

    def _verified_doc_types(self, connection, family_id: str) -> set[str]:
        return {r["doc_type"] for r in connection.execute(
            "SELECT DISTINCT doc_type FROM hs_documents WHERE family_id=? AND state='verified'",
            (family_id,)).fetchall()}

    def _open_conflict(self, connection, family_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM hs_document_conflicts WHERE family_id=? AND state='open'",
            (family_id,)).fetchone() is not None

    def _resume_applications(self, connection, family_id: str, *, actor: str) -> None:
        if self._open_conflict(connection, family_id):
            return
        rows = connection.execute(
            "SELECT * FROM hs_applications WHERE family_id=? AND state='paused'", (family_id,)).fetchall()
        for row in rows:
            app = dict(row)
            project = json.loads(connection.execute(
                "SELECT policy_json FROM hs_projects WHERE project_id=?", (app["project_id"],)).fetchone()["policy_json"])
            missing = [d for d in project["required_docs"]
                       if d not in self._verified_doc_types(connection, family_id)]
            state = "received" if missing else "ready"
            connection.execute(
                "UPDATE hs_applications SET state=?,missing_docs_json=?,pause_reason='',updated_at=? "
                "WHERE application_id=?",
                (state, canonical_json(missing), self.clock.now(), app["application_id"]))
            self._event(connection, family_id=family_id, event_type="application.resumed",
                        detail={"application_id": app["application_id"], "state": state}, actor=actor)

    # ------------------------------------------------------------ 申请与认定

    def apply(self, context: AccessContext, family_id: str, *, applicant_id: str, program: str,
              project_id: str, request_key: str) -> dict:
        self._staff(context, "write:housing")
        if program not in PROGRAMS:
            raise ValidationError("未知住房保障类型")
        now = self.clock.now()
        with self.database.transaction() as connection:
            self._row(connection, "SELECT family_id FROM hs_families WHERE family_id=?", (family_id,))
            self._assert_person_in_family(connection, family_id, applicant_id, now)
            project = self._row(connection,
                                "SELECT * FROM hs_projects WHERE project_id=? AND state='active'", (project_id,))
            if project["program"] != program:
                raise ValidationError("房源项目与申请的保障类型不一致")
            open_app = connection.execute(
                "SELECT application_id FROM hs_applications WHERE family_id=? AND program=? "
                "AND state IN ('received','ready','paused')",
                (family_id, program)).fetchone()
            if open_app:
                raise ConflictError("该家庭同一保障类型已有在途申请")
            active_elig = self._effective_eligibility(connection, family_id, program, now)
            if active_elig:
                raise ConflictError("该家庭该保障类型的资格仍在有效期，无需重复申请")
            policy = json.loads(project["policy_json"])
            missing = [d for d in policy["required_docs"]
                       if d not in self._verified_doc_types(connection, family_id)]
            state = "received" if missing else "ready"
            if self._open_conflict(connection, family_id):
                state = "paused"
            application_id = new_id("app")
            connection.execute(
                "INSERT INTO hs_applications(application_id,family_id,applicant_id,program,project_id,state,"
                "missing_docs_json,recorded_by,created_at,updated_at,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (application_id, family_id, applicant_id, program, project_id, state,
                 canonical_json(missing), context.actor_id, now, now))
            self._event(connection, family_id=family_id, event_type="application.submitted",
                        detail={"application_id": application_id, "program": program,
                                "project_id": project_id, "state": state, "missing_docs": missing},
                        actor=context.actor_id)
            if missing:
                self._job_once(connection, job_id=f"hs:doc-nudge:{application_id}",
                               job_type="housing.doc_nudge", subject_id=application_id,
                               run_at=_add_days(now, 7), payload={"family_id": family_id})
            return dict(connection.execute(
                "SELECT * FROM hs_applications WHERE application_id=?", (application_id,)).fetchone())

    def decide_application(self, context: AccessContext, application_id: str, *, decision: str,
                           facts: Mapping[str, Any], note: str = "",
                           effective_from: str | None = None, request_key: str) -> dict:
        """审核认定：批准则生成带生效区间的资格；经办人不能是录入人。"""
        self._staff(context, "approve:housing")
        if decision not in ("approved", "rejected"):
            raise ValidationError("决定必须是 approved 或 rejected")
        facts = self._clean_facts(facts)
        effective_from = canonical_instant(effective_from or self.clock.now())
        with self.database.transaction() as connection:
            app = self._row(connection,
                            "SELECT * FROM hs_applications WHERE application_id=?", (application_id,))
            if app["recorded_by"] == context.actor_id:
                raise PermissionDenied("经办人不能审批自己录入的申请")
            if app["state"] in ("approved", "rejected", "cancelled"):
                raise ConflictError("申请已经作出决定")
            if app["state"] == "paused":
                raise ConflictError("申请因证明冲突暂停，需先裁定冲突")
            project = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?",
                                (app["project_id"],))
            policy = json.loads(project["policy_json"])
            verified = self._verified_doc_types(connection, app["family_id"])
            result = evaluate(policy, facts, verified)
            now = self.clock.now()
            if decision == "rejected":
                connection.execute(
                    "UPDATE hs_applications SET state='rejected',decided_by=?,decision_note=?,updated_at=? "
                    "WHERE application_id=?",
                    (context.actor_id, note.strip(), now, application_id))
                self._event(connection, family_id=app["family_id"], event_type="application.rejected",
                            detail={"application_id": application_id, "program": app["program"],
                                    "reasons": result["reasons"], "note": note}, actor=context.actor_id)
                self._audit(connection, actor=context.actor_id, action="housing.application.reject",
                            entity_id=application_id, version=int(app["version"]) + 1,
                            detail={"reasons": result["reasons"]})
                return dict(connection.execute(
                    "SELECT * FROM hs_applications WHERE application_id=?", (application_id,)).fetchone())
            if app["state"] == "received" or not result["eligible"]:
                raise ConflictError("材料不齐或不符合条件，不能批准: " + "; ".join(result["reasons"]))
            # 批准：仅把在新生效日仍有效的旧资格截止到新起点；
            # 已自然到期的旧资格保留，由生效区间表达到期。
            previous = connection.execute(
                "SELECT * FROM hs_eligibility WHERE family_id=? AND state='active' "
                "AND effective_from<? AND (effective_to IS NULL OR effective_to>?)",
                (app["family_id"], effective_from, effective_from)).fetchall()
            eligibility_id = new_id("elig")
            snapshot = {"facts": dict(facts), "verified_docs": sorted(verified),
                        "members": self._members_at(connection, app["family_id"], effective_from),
                        "care_dependents": self._care_count(connection, app["family_id"], effective_from)}
            effective_to = _add_days(effective_from, int(policy["eligibility_days"]))
            connection.execute(
                "INSERT INTO hs_eligibility(eligibility_id,family_id,person_id,program,policy_digest,"
                "fact_snapshot_json,effective_from,effective_to,state,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'active',?,?)",
                (eligibility_id, app["family_id"], app["applicant_id"], app["program"],
                 project["policy_digest"], canonical_json(snapshot), effective_from, effective_to,
                 context.actor_id, now))
            for row in previous:
                connection.execute(
                    "UPDATE hs_eligibility SET state='superseded',effective_to=?,superseded_by=? "
                    "WHERE eligibility_id=?",
                    (effective_from, eligibility_id, row["eligibility_id"]))
            connection.execute(
                "UPDATE hs_applications SET state='approved',decided_by=?,eligibility_id=?,decision_note=?,"
                "missing_docs_json='[]',updated_at=? WHERE application_id=?",
                (context.actor_id, eligibility_id, note.strip(), now, application_id))
            self._event(connection, family_id=app["family_id"], event_type="eligibility.granted",
                        detail={"eligibility_id": eligibility_id, "program": app["program"],
                                "effective_from": effective_from, "effective_to": effective_to,
                                "policy_digest": project["policy_digest"], "policy_version": policy["version"],
                                "reasons": result["reasons"],
                                "superseded": [r["eligibility_id"] for r in previous]},
                        actor=context.actor_id)
            self._audit(connection, actor=context.actor_id, action="housing.eligibility.grant",
                        entity_id=eligibility_id, version=1,
                        detail={"family_id": app["family_id"], "program": app["program"],
                                "effective_from": effective_from, "superseded":
                                    [r["eligibility_id"] for r in previous]})
            return dict(connection.execute(
                "SELECT * FROM hs_applications WHERE application_id=?", (application_id,)).fetchone())

    def _clean_facts(self, facts: Mapping[str, Any]) -> dict:
        allowed = {"social_security_months", "monthly_income_per_capita_minor", "housing_difficulty"}
        unknown = set(facts) - allowed
        if unknown:
            raise ValidationError("未知认定事实字段: " + ",".join(sorted(unknown)))
        result = {
            "social_security_months": int(facts.get("social_security_months", 0) or 0),
            "monthly_income_per_capita_minor": int(facts.get("monthly_income_per_capita_minor", 0) or 0),
            "housing_difficulty": bool(facts.get("housing_difficulty", False)),
        }
        if result["social_security_months"] < 0 or result["monthly_income_per_capita_minor"] < 0:
            raise ValidationError("社保月数与收入不能为负")
        return result

    def get_application(self, context: AccessContext, application_id: str) -> dict:
        with self.database.connect() as connection:
            app = self._row(connection,
                            "SELECT * FROM hs_applications WHERE application_id=?", (application_id,))
        self._family_scope(context, app["family_id"])
        return app

    def list_applications(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM hs_applications WHERE family_id=? ORDER BY created_at", (family_id,)).fetchall()]

    def list_eligibility(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_eligibility WHERE family_id=? ORDER BY effective_from",
                (family_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["fact_snapshot"] = json.loads(item.pop("fact_snapshot_json"))
                result.append(item)
            return result

    def _effective_eligibility(self, connection, family_id: str, program: str, at: str):
        instant = canonical_instant(at)
        row = connection.execute(
            "SELECT * FROM hs_eligibility WHERE family_id=? AND program=? AND state='active' "
            "AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)",
            (family_id, program, instant, instant)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 轮候

    def freeze_waitlist(self, context: AccessContext, project_id: str, *,
                        entries: Iterable[Mapping[str, Any]], request_key: str) -> dict:
        """按入列时间、累计保障天数与照顾加分冻结名次；冻结后名次不再变化。"""
        self._staff(context, "write:housing")
        normalized: list[dict] = []
        for entry in entries:
            normalized.append({
                "family_id": str(entry["family_id"]),
                "application_id": str(entry.get("application_id", "")),
                "joined_at": canonical_instant(entry["joined_at"]),
            })
        if len({e["family_id"] for e in normalized}) != len(normalized):
            raise ValidationError("同一家庭在同一轮次中重复出现")
        with self.database.transaction() as connection:
            project = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?", (project_id,))
            open_waitlist = connection.execute(
                "SELECT waitlist_id FROM hs_waitlists WHERE project_id=? AND state='open'",
                (project_id,)).fetchone()
            if open_waitlist:
                raise ConflictError("该项目已有冻结中的轮候批次")
            now = self.clock.now()
            enriched = []
            for e in normalized:
                self._row(connection, "SELECT family_id FROM hs_families WHERE family_id=?",
                          (e["family_id"],))
                carry = self._carried_days(connection, e["family_id"], now)
                bonus = care_bonus_days(self._care_count(connection, e["family_id"], now))
                enriched.append({**e, "carry_days": carry, "care_bonus": bonus,
                                 "priority": carry + bonus})
            # 冻结的排序规则：累计保障天数与照顾加分折算的优先级在前，
            # 同分按最早入列时间，再以家庭标识兜底；冻结后名次永不重排。
            enriched.sort(key=lambda x: (-x["priority"], x["joined_at"], x["family_id"]))
            ranking = [{"rank": i + 1, "family_id": e["family_id"], "priority": e["priority"],
                        "carry_days": e["carry_days"], "care_bonus": e["care_bonus"],
                        "joined_at": e["joined_at"]} for i, e in enumerate(enriched)]
            waitlist_id = new_id("waitlist")
            connection.execute(
                "INSERT INTO hs_waitlists(waitlist_id,project_id,program,policy_digest,ranking_json,"
                "frozen_at,state,version,created_by) VALUES(?,?,?,?,?,?,'open',1,?)",
                (waitlist_id, project_id, project["program"], project["policy_digest"],
                 canonical_json(ranking), now, context.actor_id))
            for item in ranking:
                e = next(x for x in enriched if x["family_id"] == item["family_id"])
                connection.execute(
                    "INSERT INTO hs_waitlist_entries(waitlist_id,family_id,rank,carry_days,care_bonus,"
                    "state,joined_at,application_id) VALUES(?,?,?,?,?, 'waiting',?,?)",
                    (waitlist_id, item["family_id"], item["rank"], item["carry_days"],
                     item["care_bonus"], e["joined_at"], e["application_id"]))
                self._event(connection, family_id=item["family_id"], event_type="waitlist.frozen",
                            detail={"waitlist_id": waitlist_id, "project_id": project_id,
                                    "rank": item["rank"], "priority": item["priority"]},
                            actor=context.actor_id)
            return self._waitlist_view(connection, waitlist_id, context)

    def _carried_days(self, connection, family_id: str, at: str) -> int:
        """家庭跨项目随迁保留的历史天数：已履行租住天数与历次轮候等待天数。"""
        instant = parse_instant(canonical_instant(at))
        total = 0.0
        for row in connection.execute(
                "SELECT start_at,end_at,handover_completed_at,state FROM hs_leases WHERE family_id=? "
                "AND state IN ('active','ending','handed_over')", (family_id,)).fetchall():
            start = parse_instant(row["start_at"])
            end_raw = row["handover_completed_at"] or (row["end_at"] if row["state"] == "handed_over" else None)
            end = parse_instant(end_raw) if end_raw else instant
            total += max(0.0, (min(end, instant) - start).total_seconds() / 86400)
        for row in connection.execute(
                "SELECT joined_at,placed_at,left_at FROM hs_waitlist_entries "
                "WHERE family_id=?", (family_id,)).fetchall():
            start = parse_instant(row["joined_at"])
            end_raw = row["placed_at"] or row["left_at"]
            end = parse_instant(end_raw) if end_raw else instant
            total += max(0.0, (min(end, instant) - start).total_seconds() / 86400)
        return int(total)

    def get_waitlist(self, context: AccessContext, waitlist_id: str) -> dict:
        context.require("read:housing")
        with self.database.connect() as connection:
            return self._waitlist_view(connection, waitlist_id, context)

    def _waitlist_view(self, connection, waitlist_id: str, context: AccessContext) -> dict:
        row = self._row(connection, "SELECT * FROM hs_waitlists WHERE waitlist_id=?",
                        (waitlist_id,))
        result = dict(row)
        result["ranking"] = json.loads(result.pop("ranking_json"))
        entries = connection.execute(
            "SELECT * FROM hs_waitlist_entries WHERE waitlist_id=? ORDER BY rank",
            (waitlist_id,)).fetchall()
        result["entries"] = [dict(e) for e in entries]
        if STAFF_SCOPE in context.scopes:
            return result
        own = {fid for fid in {e["family_id"] for e in result["entries"]}
               if f"family:{fid}" in context.scopes}
        if not own:
            raise PermissionDenied("只能查看本家庭所在的轮候批次")
        # 申请人只看到自己的明细与其他名次的存在，不看到其他家庭的身份与加分
        for item in result["ranking"]:
            if item["family_id"] not in own:
                item["family_id"] = "***"
                item["carry_days"] = None
                item["care_bonus"] = None
                item["priority"] = None
        for entry in result["entries"]:
            if entry["family_id"] not in own:
                entry["family_id"] = "***"
                entry["carry_days"] = None
                entry["care_bonus"] = None
                entry["application_id"] = ""
        return result

    def promote(self, context: AccessContext, waitlist_id: str, *, room_id: str,
                request_key: str) -> dict:
        """按冻结名次取首位在候家庭递补，生成预备租约（占用房间但未起租）。"""
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            waitlist = self._row(connection,
                                 "SELECT * FROM hs_waitlists WHERE waitlist_id=?", (waitlist_id,))
            if waitlist["state"] != "open":
                raise ConflictError("轮候批次已经关闭")
            entry = connection.execute(
                "SELECT * FROM hs_waitlist_entries WHERE waitlist_id=? AND state='waiting' "
                "ORDER BY rank LIMIT 1", (waitlist_id,)).fetchone()
            if not entry:
                raise ConflictError("轮候批次中没有可递补家庭")
            entry = dict(entry)
            room = self._get_room(connection, room_id)
            project = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?",
                                (waitlist["project_id"],))
            if room["project_id"] != waitlist["project_id"]:
                raise ValidationError("房间不属于该轮候项目")
            if room["state"] != "available":
                raise ConflictError("房间尚未释放，不能递补入住")
            eligibility = self._effective_eligibility(connection, entry["family_id"],
                                                      waitlist["program"], self.clock.now())
            if not eligibility:
                raise ConflictError("递补家庭当前没有该保障类型的有效资格")
            open_lease = connection.execute(
                "SELECT 1 FROM hs_leases WHERE family_id=? AND state IN ('prepared','active','ending')",
                (entry["family_id"],)).fetchone()
            if open_lease:
                raise ConflictError("递补家庭尚有未结合约，不能重复占用房源")
            start = self.clock.now()
            end = _add_days(start, int(json.loads(project["policy_json"])["eligibility_days"]))
            lease_id = self._insert_lease(connection, family_id=entry["family_id"],
                                          person_id=eligibility["person_id"], room=room,
                                          project=project, start_at=start, end_at=end,
                                          eligibility_id=eligibility["eligibility_id"],
                                          actor=context.actor_id)
            connection.execute(
                "UPDATE hs_waitlist_entries SET state='placed',placed_at=?,application_id=? "
                "WHERE waitlist_id=? AND family_id=?",
                (self.clock.now(), entry["application_id"], waitlist_id, entry["family_id"]))
            self._set_room_state(connection, room_id, "reserved")
            self._event(connection, family_id=entry["family_id"], event_type="waitlist.promoted",
                        detail={"waitlist_id": waitlist_id, "rank": entry["rank"],
                                "room_id": room_id, "lease_id": lease_id}, actor=context.actor_id)
            return {"waitlist_id": waitlist_id, "family_id": entry["family_id"],
                    "rank": entry["rank"], "lease_id": lease_id}

    def forfeit_placement(self, context: AccessContext, waitlist_id: str, family_id: str, *,
                          reason: str, request_key: str) -> dict:
        """放弃递补：预备租约撤销、房间释放，下一名按冻结名次递补。"""
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            entry = self._row(connection,
                              "SELECT * FROM hs_waitlist_entries WHERE waitlist_id=? AND family_id=?",
                              (waitlist_id, family_id))
            if entry["state"] != "placed":
                raise ConflictError("只有已递补未入住的家庭可以放弃")
            lease = connection.execute(
                "SELECT * FROM hs_leases WHERE family_id=? AND state='prepared' ORDER BY created_at DESC LIMIT 1",
                (family_id,)).fetchone()
            if lease:
                lease = dict(lease)
                connection.execute("UPDATE hs_leases SET state='cancelled',updated_at=? WHERE lease_id=?",
                                   (self.clock.now(), lease["lease_id"]))
                self._set_room_state(connection, lease["room_id"], "available")
            connection.execute(
                "UPDATE hs_waitlist_entries SET state='forfeited',left_at=? WHERE waitlist_id=? AND family_id=?",
                (self.clock.now(), waitlist_id, family_id))
            self._event(connection, family_id=family_id, event_type="waitlist.forfeited",
                        detail={"waitlist_id": waitlist_id, "rank": entry["rank"], "reason": reason},
                        actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_waitlist_entries WHERE waitlist_id=? AND family_id=?",
                (waitlist_id, family_id)).fetchone())

    def close_waitlist(self, context: AccessContext, waitlist_id: str, *, request_key: str) -> dict:
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            connection.execute("UPDATE hs_waitlists SET state='closed',version=version+1 WHERE waitlist_id=?",
                               (waitlist_id,))
            return dict(self._row(connection, "SELECT * FROM hs_waitlists WHERE waitlist_id=?",
                                  (waitlist_id,)))

    # ------------------------------------------------------------ 租约

    def check_in(self, context: AccessContext, family_id: str, *, person_id: str, room_id: str,
                 start_at: str | None = None, end_at: str | None = None,
                 monthly_rent_minor: int | None = None, request_key: str) -> dict:
        """驿站等直接入住：要求该保障类型资格在入住日有效。"""
        self._staff(context, "write:housing")
        start_at = canonical_instant(start_at or self.clock.now())
        with self.database.transaction() as connection:
            room = self._get_room(connection, room_id)
            project = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?",
                                (room["project_id"],))
            self._assert_person_in_family(connection, family_id, person_id, start_at)
            if room["state"] != "available":
                raise ConflictError("房间已有在住或预备租约")
            active_family_lease = connection.execute(
                "SELECT 1 FROM hs_leases WHERE family_id=? AND state IN ('prepared','active','ending')",
                (family_id,)).fetchone()
            if active_family_lease:
                raise ConflictError("家庭已有未结合约，不能重复占用房源")
            eligibility = self._effective_eligibility(connection, family_id, project["program"], start_at)
            if not eligibility:
                raise ConflictError(f"家庭在 {start_at} 没有有效的{PROGRAM_LABELS[project['program']]}资格")
            if end_at is None:
                days = int(json.loads(project["policy_json"])["eligibility_days"])
                end_at = _add_days(start_at, days)
            else:
                end_at = canonical_instant(end_at)
            if parse_instant(end_at) <= parse_instant(start_at):
                raise ValidationError("租约结束时间必须晚于开始时间")
            rent = int(monthly_rent_minor if monthly_rent_minor is not None else room["monthly_rent_minor"])
            lease_id = self._insert_lease(connection, family_id=family_id, person_id=person_id, room=room,
                                          project=project, start_at=start_at, end_at=end_at,
                                          eligibility_id=eligibility["eligibility_id"], actor=context.actor_id,
                                          rent_override=rent)
            connection.execute(
                "UPDATE hs_leases SET state='active',updated_at=? WHERE lease_id=?",
                (self.clock.now(), lease_id))
            self._set_room_state(connection, room_id, "occupied")
            self._schedule_lease_jobs(connection, lease_id, start_at, end_at)
            self._event(connection, family_id=family_id, event_type="lease.active",
                        detail={"lease_id": lease_id, "room_id": room_id, "program": project["program"],
                                "start_at": start_at, "end_at": end_at,
                                "eligibility_id": eligibility["eligibility_id"]},
                        actor=context.actor_id)
            self._audit(connection, actor=context.actor_id, action="housing.lease.activate",
                        entity_id=lease_id, version=1,
                        detail={"family_id": family_id, "room_id": room_id, "program": project["program"]})
            return self._get_lease(connection, lease_id)

    def _insert_lease(self, connection, *, family_id: str, person_id: str, room: dict, project: dict,
                      start_at: str, end_at: str, eligibility_id: str, actor: str,
                      rent_override: int | None = None, predecessor: str = "") -> str:
        lease_id = new_id("lease")
        now = self.clock.now()
        rent = int(rent_override if rent_override is not None else room["monthly_rent_minor"])
        connection.execute(
            "INSERT INTO hs_leases(lease_id,family_id,person_id,room_id,project_id,program,start_at,end_at,"
            "monthly_rent_minor,state,predecessor_lease_id,eligibility_id,created_by,created_at,updated_at,"
            "version) VALUES(?,?,?,?,?,?,?,?,?, 'prepared',?,?,?,?,?,1)",
            (lease_id, family_id, person_id, room["room_id"], project["project_id"], project["program"],
             start_at, end_at, rent, predecessor, eligibility_id, actor, now, now))
        return lease_id

    def _schedule_lease_jobs(self, connection, lease_id: str, start_at: str, end_at: str) -> None:
        notice_at = _add_days(end_at, -7)
        if parse_instant(notice_at) <= parse_instant(self.clock.now()):
            notice_at = self.clock.now()
        self._job_once(connection, job_id=f"hs:lease-notice:{lease_id}", job_type="housing.lease_notice",
                       subject_id=lease_id, run_at=notice_at, payload={"end_at": end_at})
        self._job_once(connection, job_id=f"hs:lease-expiry:{lease_id}", job_type="housing.lease_expiry",
                       subject_id=lease_id, run_at=end_at, payload={})

    def activate_lease(self, context: AccessContext, lease_id: str, *, request_key: str) -> dict:
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            lease = self._get_lease(connection, lease_id)
            if lease["state"] != "prepared":
                raise ConflictError("只有预备租约可以生效")
            if not self._effective_eligibility(connection, lease["family_id"], lease["program"],
                                               self.clock.now()):
                raise ConflictError("家庭当前没有有效资格，租约不能生效")
            now = self.clock.now()
            connection.execute("UPDATE hs_leases SET state='active',updated_at=? WHERE lease_id=?",
                               (now, lease_id))
            self._set_room_state(connection, lease["room_id"], "occupied")
            connection.execute(
                "UPDATE hs_waitlist_entries SET state='served',placed_at=COALESCE(placed_at,?) "
                "WHERE family_id=? AND state='placed'", (now, lease["family_id"]))
            self._schedule_lease_jobs(connection, lease_id, lease["start_at"], lease["end_at"])
            self._event(connection, family_id=lease["family_id"], event_type="lease.active",
                        detail={"lease_id": lease_id, "room_id": lease["room_id"]}, actor=context.actor_id)
            return self._get_lease(connection, lease_id)

    def begin_handover(self, context: AccessContext, lease_id: str, *, kind: str,
                       target_room_id: str = "", checklist: Iterable[str] = (),
                       request_key: str) -> dict:
        """退租或换房先建立交接：租约进入 ending，房间继续占用，交接完成前不释放。"""
        self._staff(context, "write:housing")
        if kind not in ("exit", "transfer"):
            raise ValidationError("交接类型必须是 exit 或 transfer")
        items = [str(x).strip() for x in checklist if str(x).strip()]
        if not items:
            raise ValidationError("交接清单不能为空")
        with self.database.transaction() as connection:
            lease = self._get_lease(connection, lease_id)
            if lease["state"] != "active":
                raise ConflictError("只有履行中的租约可以发起交接")
            if kind == "transfer":
                if not target_room_id:
                    raise ValidationError("换房必须指定目标房间")
                target = self._get_room(connection, target_room_id)
                target_project = self._row(connection,
                                           "SELECT * FROM hs_projects WHERE project_id=?",
                                           (target["project_id"],))
                if target_project["program"] != lease["program"]:
                    raise ValidationError("跨保障类型换房应先重新申请与认定")
                if target["state"] != "available":
                    raise ConflictError("目标房间尚未释放")
                if target_room_id == lease["room_id"]:
                    raise ValidationError("目标房间不能与现房间相同")
            pending = connection.execute(
                "SELECT 1 FROM hs_handovers WHERE lease_id=? AND state='pending'", (lease_id,)).fetchone()
            if pending:
                raise ConflictError("该租约已有进行中的交接")
            now = self.clock.now()
            connection.execute("UPDATE hs_leases SET state='ending',updated_at=? WHERE lease_id=?",
                               (now, lease_id))
            handover_id = new_id("handover")
            checklist_json = canonical_json([{"item": x, "done": False} for x in items])
            connection.execute(
                "INSERT INTO hs_handovers(handover_id,lease_id,family_id,kind,room_id,target_room_id,"
                "checklist_json,settled,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,0,'pending',?,?)",
                (handover_id, lease_id, lease["family_id"], kind, lease["room_id"], target_room_id,
                 checklist_json, context.actor_id, now))
            self._job_once(connection, job_id=f"hs:room-release:{lease_id}",
                           job_type="housing.room_release", subject_id=lease_id,
                           run_at=_add_days(now, 3), payload={"handover_id": handover_id})
            self._event(connection, family_id=lease["family_id"], event_type="handover.begun",
                        detail={"handover_id": handover_id, "lease_id": lease_id, "kind": kind,
                                "target_room_id": target_room_id, "checklist": items},
                        actor=context.actor_id)
            return self._get_handover(connection, handover_id)

    def complete_handover_item(self, context: AccessContext, handover_id: str, item: str,
                               *, done: bool = True, request_key: str) -> dict:
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            handover = dict(self._row(connection,
                                      "SELECT * FROM hs_handovers WHERE handover_id=?",
                                      (handover_id,)))
            if handover["state"] != "pending":
                raise ConflictError("交接已经结束")
            items = json.loads(handover["checklist_json"])
            matched = [x for x in items if x["item"] == item]
            if not matched:
                raise ValidationError("清单中没有该事项")
            for x in items:
                if x["item"] == item:
                    x["done"] = bool(done)
            connection.execute("UPDATE hs_handovers SET checklist_json=? WHERE handover_id=?",
                               (canonical_json(items), handover_id))
            return self._get_handover(connection, handover_id)

    def complete_handover(self, context: AccessContext, handover_id: str, *, request_key: str) -> dict:
        """完成交接：费用结清、清单全完成，才释放房源；换房同时生成新租约。"""
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            handover = self._get_handover(connection, handover_id)
            if handover["state"] != "pending":
                raise ConflictError("交接已经结束")
            items = handover["checklist"]
            if not items or not all(x["done"] for x in items):
                raise ConflictError("交接清单未全部完成，不能退房或换房")
            lease = self._get_lease(connection, handover["lease_id"])
            due, paid = self._rent_position(connection, lease, at=self.clock.now())
            if paid < due:
                raise ConflictError(f"费用未结清，欠缴 {due - paid} 分；房源暂不释放")
            now = self.clock.now()
            connection.execute(
                "UPDATE hs_leases SET state='handed_over',handover_state='completed',"
                "handover_completed_at=?,updated_at=? WHERE lease_id=?",
                (now, now, lease["lease_id"]))
            connection.execute(
                "UPDATE hs_handovers SET state='completed',settled=1,completed_at=? WHERE handover_id=?",
                (now, handover_id))
            self._set_room_state(connection, lease["room_id"], "available")
            new_lease_id = ""
            if handover["kind"] == "transfer":
                target = self._get_room(connection, handover["target_room_id"])
                project = self._row(connection, "SELECT * FROM hs_projects WHERE project_id=?",
                                    (target["project_id"],))
                eligibility = self._effective_eligibility(connection, lease["family_id"],
                                                          lease["program"], now)
                if not eligibility:
                    raise ConflictError("家庭资格已经失效，不能办理换房入住")
                new_lease_id = self._insert_lease(
                    connection, family_id=lease["family_id"], person_id=lease["person_id"], room=target,
                    project=project, start_at=now, end_at=lease["end_at"],
                    eligibility_id=eligibility["eligibility_id"], actor=context.actor_id,
                    predecessor=lease["lease_id"])
                connection.execute(
                    "UPDATE hs_leases SET state='active',updated_at=? WHERE lease_id=?",
                    (now, new_lease_id))
                self._set_room_state(connection, target["room_id"], "occupied")
                self._schedule_lease_jobs(connection, new_lease_id, now, lease["end_at"])
            self._event(connection, family_id=lease["family_id"], event_type="handover.completed",
                        detail={"handover_id": handover_id, "lease_id": lease["lease_id"],
                                "kind": handover["kind"], "new_lease_id": new_lease_id,
                                "released_room_id": lease["room_id"]}, actor=context.actor_id)
            self._audit(connection, actor=context.actor_id, action="housing.handover.complete",
                        entity_id=handover_id, version=1,
                        detail={"lease_id": lease["lease_id"], "kind": handover["kind"],
                                "new_lease_id": new_lease_id})
            result = self._get_handover(connection, handover_id)
            result["new_lease_id"] = new_lease_id
            return result

    def _get_lease(self, connection, lease_id: str) -> dict:
        return dict(self._row(connection, "SELECT * FROM hs_leases WHERE lease_id=?", (lease_id,)))

    def _get_handover(self, connection, handover_id: str) -> dict:
        row = dict(self._row(connection,
                             "SELECT * FROM hs_handovers WHERE handover_id=?", (handover_id,)))
        row["checklist"] = json.loads(row.pop("checklist_json"))
        return row

    def get_lease(self, context: AccessContext, lease_id: str) -> dict:
        with self.database.connect() as connection:
            lease = self._get_lease(connection, lease_id)
        self._family_scope(context, lease["family_id"])
        return lease

    def list_leases(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM hs_leases WHERE family_id=? ORDER BY start_at", (family_id,)).fetchall()]

    def list_handovers(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_handovers WHERE family_id=? ORDER BY created_at", (family_id,)).fetchall()
            return [self._get_handover(connection, r["handover_id"]) for r in rows]

    # ------------------------------------------------------------ 缴费与减免

    def record_payment(self, context: AccessContext, lease_id: str, *, period: str,
                       amount: str | int, kind: str = "rent", paid_at: str | None = None,
                       request_key: str) -> dict:
        """缴费事实不可变：(租约, 账期, 类型) 唯一，重复缴费被拒绝。"""
        self._staff(context, "write:housing")
        if kind not in ("rent", "deposit", "other"):
            raise ValidationError("缴费类型不合法")
        period = period.strip()
        if len(period) != 7 or period[4] != "-":
            raise ValidationError("账期格式必须为 YYYY-MM")
        minor = _amount_minor(amount)
        if minor <= 0:
            raise ValidationError("缴费金额必须大于零")
        paid_at = canonical_instant(paid_at or self.clock.now())
        with self.database.transaction() as connection:
            lease = self._get_lease(connection, lease_id)
            if lease["state"] == "cancelled":
                raise ConflictError("已撤销租约不能入账")
            dup = connection.execute(
                "SELECT 1 FROM hs_payments WHERE lease_id=? AND period=? AND kind=?",
                (lease_id, period, kind)).fetchone()
            if dup:
                raise ConflictError("该账期费用已经缴纳，不能重复入账")
            payment_id = new_id("pay")
            connection.execute(
                "INSERT INTO hs_payments(payment_id,family_id,lease_id,period,amount_minor,kind,paid_at,paid_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (payment_id, lease["family_id"], lease_id, period, minor, kind, paid_at, context.actor_id))
            self._event(connection, family_id=lease["family_id"], event_type="payment.recorded",
                        detail={"payment_id": payment_id, "lease_id": lease_id, "period": period,
                                "amount_minor": minor, "kind": kind}, actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_payments WHERE payment_id=?", (payment_id,)).fetchone())

    def list_payments(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM hs_payments WHERE family_id=? ORDER BY paid_at,payment_id",
                (family_id,)).fetchall()]

    def rent_position(self, context: AccessContext, lease_id: str) -> dict:
        self.get_lease(context, lease_id)  # 范围校验
        with self.database.connect() as connection:
            lease = self._get_lease(connection, lease_id)
            due, paid = self._rent_position(connection, lease, at=self.clock.now())
            return {"lease_id": lease_id, "due_minor": due, "paid_minor": paid,
                    "outstanding_minor": max(0, due - paid)}

    def _active_reduction(self, connection, lease_id: str, period: str) -> int:
        total = 0
        for row in connection.execute(
                "SELECT reduction_minor,effective_from_period FROM hs_rent_adjustments "
                "WHERE lease_id=? AND state='active'", (lease_id,)).fetchall():
            if _period_cmp(period, row["effective_from_period"]) >= 0:
                total += int(row["reduction_minor"])
        return total

    def _rent_position(self, connection, lease: dict, *, at: str) -> tuple[int, int]:
        """已履行月份的应缴与实缴；减免只影响生效之后的账期，不重写历史账期。"""
        now_instant = canonical_instant(at)
        if lease["state"] in ("ending", "handed_over") and lease["handover_completed_at"]:
            end_instant = min(lease["handover_completed_at"], lease["end_at"])
        else:
            end_instant = min(lease["end_at"], now_instant)
        if parse_instant(end_instant) <= parse_instant(lease["start_at"]):
            return 0, 0
        periods = _periods_through(lease["start_at"], end_instant)
        due = 0
        for period in periods:
            reduction = min(lease["monthly_rent_minor"],
                            self._active_reduction(connection, lease["lease_id"], period))
            due += lease["monthly_rent_minor"] - reduction
        paid = connection.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS n FROM hs_payments WHERE lease_id=? AND kind='rent'",
            (lease["lease_id"],)).fetchone()["n"]
        return int(due), int(paid)

    def request_rent_adjustment(self, context: AccessContext, lease_id: str, *, reason: str,
                                reduction_minor: int, effective_from_period: str,
                                request_key: str) -> dict:
        """租金减免作为例外录入，待另一名经办人审批；通过后只作用于后续账期。"""
        self._staff(context, "write:housing")
        reason = reason.strip()
        if not reason:
            raise ValidationError("减免原因不能为空")
        reduction = int(reduction_minor)
        if reduction <= 0:
            raise ValidationError("减免金额必须大于零")
        if len(effective_from_period) != 7:
            raise ValidationError("生效账期格式必须为 YYYY-MM")
        with self.database.transaction() as connection:
            lease = self._get_lease(connection, lease_id)
            if lease["state"] not in ("active", "ending"):
                raise ConflictError("只有履行中的租约可以申请减免")
            if reduction > lease["monthly_rent_minor"]:
                raise ValidationError("减免金额不能超过月租金")
            adjustment_id = new_id("adj")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO hs_rent_adjustments(adjustment_id,lease_id,family_id,reason,reduction_minor,"
                "effective_from_period,state,created_by,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                (adjustment_id, lease_id, lease["family_id"], reason, reduction,
                 effective_from_period, context.actor_id, now))
            exception_id = self._record_exception(connection, family_id=lease["family_id"],
                                                  subject_type="rent_adjustment",
                                                  subject_id=adjustment_id, reason=reason,
                                                  payload={"reduction_minor": reduction,
                                                           "effective_from_period": effective_from_period},
                                                  actor=context.actor_id)
            self._event(connection, family_id=lease["family_id"], event_type="rent_adjustment.requested",
                        detail={"adjustment_id": adjustment_id, "exception_id": exception_id,
                                "lease_id": lease_id, "reduction_minor": reduction,
                                "effective_from_period": effective_from_period}, actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_rent_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone())

    def _record_exception(self, connection, *, family_id: str, subject_type: str, subject_id: str,
                          reason: str, payload: dict, actor: str) -> str:
        exception_id = new_id("exc")
        connection.execute(
            "INSERT INTO hs_exceptions(exception_id,family_id,subject_type,subject_id,reason,payload_json,"
            "state,recorded_by,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
            (exception_id, family_id, subject_type, subject_id, reason, canonical_json(payload),
             actor, self.clock.now()))
        return exception_id

    def record_exception(self, context: AccessContext, family_id: str, *, subject_type: str,
                         subject_id: str, reason: str, payload: Mapping[str, Any] | None = None,
                         request_key: str) -> dict:
        self._staff(context, "write:housing")
        with self.database.transaction() as connection:
            self._row(connection, "SELECT family_id FROM hs_families WHERE family_id=?", (family_id,))
            exception_id = self._record_exception(connection, family_id=family_id,
                                                  subject_type=subject_type.strip(),
                                                  subject_id=subject_id.strip(), reason=reason.strip(),
                                                  payload=dict(payload or {}), actor=context.actor_id)
            self._event(connection, family_id=family_id, event_type="exception.recorded",
                        detail={"exception_id": exception_id, "subject_type": subject_type,
                                "subject_id": subject_id, "reason": reason}, actor=context.actor_id)
            return dict(connection.execute(
                "SELECT * FROM hs_exceptions WHERE exception_id=?", (exception_id,)).fetchone())

    def decide_exception(self, context: AccessContext, exception_id: str, *, decision: str,
                         note: str = "", request_key: str) -> dict:
        """审批例外：审批人不得是录入人；批准减免时生效，驳回则减免关闭。"""
        self._staff(context, "approve:housing")
        if decision not in ("approved", "rejected"):
            raise ValidationError("决定必须是 approved 或 rejected")
        with self.database.transaction() as connection:
            exc = dict(self._row(connection,
                                 "SELECT * FROM hs_exceptions WHERE exception_id=?", (exception_id,)))
            if exc["recorded_by"] == context.actor_id:
                raise PermissionDenied("经办人不能审批自己录入的例外")
            if exc["state"] != "pending":
                raise ConflictError("例外已经作出决定")
            now = self.clock.now()
            connection.execute(
                "UPDATE hs_exceptions SET state=?,decided_by=?,decision_note=?,decided_at=? WHERE exception_id=?",
                (decision, context.actor_id, note.strip(), now, exception_id))
            adjustment_effect = None
            if exc["subject_type"] == "rent_adjustment":
                state = "active" if decision == "approved" else "rejected"
                connection.execute(
                    "UPDATE hs_rent_adjustments SET state=? WHERE adjustment_id=? AND state='pending'",
                    (state, exc["subject_id"]))
                adjustment_effect = state
            self._event(connection, family_id=exc["family_id"], event_type="exception.decided",
                        detail={"exception_id": exception_id, "decision": decision, "note": note,
                                "subject_type": exc["subject_type"], "subject_id": exc["subject_id"],
                                "adjustment": adjustment_effect}, actor=context.actor_id)
            self._audit(connection, actor=context.actor_id, action="housing.exception.decide",
                        entity_id=exception_id, version=1,
                        detail={"decision": decision, "subject_type": exc["subject_type"]})
            return dict(connection.execute(
                "SELECT * FROM hs_exceptions WHERE exception_id=?", (exception_id,)).fetchone())

    def list_exceptions(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_exceptions WHERE family_id=? ORDER BY created_at", (family_id,)).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------ 恢复

    def ensure_recovery_jobs(self, context: AccessContext | None = None) -> dict:
        """系统恢复后扫描：为即将到期租约、缺件申请、待释放房源补齐幂等任务。"""
        if context is not None:
            self._staff(context, "write:housing")
        now = self.clock.now()
        scheduled: list[str] = []
        with self.database.transaction() as connection:
            for lease in connection.execute(
                    "SELECT * FROM hs_leases WHERE state IN ('active','ending')").fetchall():
                lease = dict(lease)
                if lease["state"] == "active":
                    notice_at = _add_days(lease["end_at"], -7)
                    if parse_instant(notice_at) <= parse_instant(now):
                        notice_at = now
                    self._job_once(connection, job_id=f"hs:lease-notice:{lease['lease_id']}",
                                   job_type="housing.lease_notice", subject_id=lease["lease_id"],
                                   run_at=notice_at, payload={"end_at": lease["end_at"]})
                    self._job_once(connection, job_id=f"hs:lease-expiry:{lease['lease_id']}",
                                   job_type="housing.lease_expiry", subject_id=lease["lease_id"],
                                   run_at=max(lease["end_at"], now), payload={})
                    scheduled.append(f"lease:{lease['lease_id']}")
                else:
                    handover = connection.execute(
                        "SELECT handover_id FROM hs_handovers WHERE lease_id=? AND state='pending'",
                        (lease["lease_id"],)).fetchone()
                    if handover:
                        self._job_once(connection, job_id=f"hs:room-release:{lease['lease_id']}",
                                       job_type="housing.room_release", subject_id=lease["lease_id"],
                                       run_at=now, payload={"handover_id": handover["handover_id"]})
                        scheduled.append(f"release:{lease['lease_id']}")
            for app in connection.execute(
                    "SELECT * FROM hs_applications WHERE state='received'").fetchall():
                app = dict(app)
                self._job_once(connection, job_id=f"hs:doc-nudge:{app['application_id']}",
                               job_type="housing.doc_nudge", subject_id=app["application_id"],
                               run_at=now, payload={"family_id": app["family_id"]})
                scheduled.append(f"docs:{app['application_id']}")
        return {"scheduled": scheduled, "at": now}

    def run_recovery(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        """认领并处理住房保障到期任务，返回每个任务的处理结果。"""
        self._staff(context, "write:housing")
        from .jobs import JobQueue
        jobs = JobQueue(self.database, self.clock).claim_due(limit=limit, prefix="housing.")
        results: list[dict] = []
        for job in jobs:
            try:
                outcome = self._process_job(job, actor=context.actor_id)
                JobQueue(self.database, self.clock).finish(job["job_id"])
                results.append({"job_id": job["job_id"], "job_type": job["job_type"], "outcome": outcome})
            except ConflictError as exc:
                # 交接未完成/欠费时立即回到待认领：人工补齐后下一次恢复即可释放，
                # JobQueue 的最多 5 次尝试仍然生效，避免无限重试。
                JobQueue(self.database, self.clock).retry(
                    job["job_id"], error=str(exc), retry_at=self.clock.now())
                results.append({"job_id": job["job_id"], "job_type": job["job_type"], "retry": str(exc)})
        return results

    def _process_job(self, job: dict, *, actor: str) -> str:
        payload = json.loads(job["payload_json"])
        kind = job["job_type"]
        with self.database.transaction() as connection:
            if kind == "housing.lease_notice":
                lease = connection.execute(
                    "SELECT * FROM hs_leases WHERE lease_id=?", (job["subject_id"],)).fetchone()
                if not lease or dict(lease)["state"] != "active":
                    return "noop"
                self._event(connection, family_id=lease["family_id"], event_type="lease.expiring_notice",
                            detail={"lease_id": lease["lease_id"], "end_at": lease["end_at"]},
                            actor="system")
            elif kind == "housing.lease_expiry":
                lease = connection.execute(
                    "SELECT * FROM hs_leases WHERE lease_id=? AND state='active'",
                    (job["subject_id"],)).fetchone()
                if not lease:
                    return "noop"
                lease = dict(lease)
                if parse_instant(lease["end_at"]) > parse_instant(self.clock.now()):
                    return "not_due"
                handover_id = new_id("handover")
                checklist = [{"item": "费用结清", "done": False}, {"item": "房屋验收", "done": False}]
                connection.execute(
                    "UPDATE hs_leases SET state='ending',updated_at=? WHERE lease_id=?",
                    (self.clock.now(), lease["lease_id"]))
                connection.execute(
                    "INSERT INTO hs_handovers(handover_id,lease_id,family_id,kind,room_id,"
                    "checklist_json,settled,state,created_by,created_at) "
                    "VALUES(?,?,?, 'exit',?,?,0, 'pending',?,?)",
                    (handover_id, lease["lease_id"], lease["family_id"], lease["room_id"],
                     canonical_json(checklist), "system", self.clock.now()))
                self._job_once(connection, job_id=f"hs:room-release:{lease['lease_id']}",
                               job_type="housing.room_release", subject_id=lease["lease_id"],
                               run_at=_add_days(self.clock.now(), 3),
                               payload={"handover_id": handover_id})
                self._event(connection, family_id=lease["family_id"], event_type="handover.begun",
                            detail={"handover_id": handover_id, "lease_id": lease["lease_id"],
                                    "kind": "exit", "reason": "lease_expired"}, actor="system")
                return "handover_begun"
            elif kind == "housing.room_release":
                handover = connection.execute(
                    "SELECT * FROM hs_handovers WHERE handover_id=?",
                    (payload.get("handover_id", ""),)).fetchone()
                if not handover or dict(handover)["state"] != "pending":
                    return "noop"
                handover = dict(handover)
                items = json.loads(handover["checklist_json"])
                lease = dict(connection.execute(
                    "SELECT * FROM hs_leases WHERE lease_id=?", (handover["lease_id"],)).fetchone())
                due, paid = self._rent_position(connection, lease, at=self.clock.now())
                if not all(x["done"] for x in items) or paid < due:
                    raise ConflictError(
                        f"交接未完成或欠费 {max(0, due - paid)} 分，房源继续保留")
                now = self.clock.now()
                connection.execute(
                    "UPDATE hs_leases SET state='handed_over',handover_state='completed',"
                    "handover_completed_at=?,updated_at=? WHERE lease_id=?",
                    (now, now, lease["lease_id"]))
                connection.execute(
                    "UPDATE hs_handovers SET state='completed',settled=1,completed_at=? WHERE handover_id=?",
                    (now, handover["handover_id"]))
                self._set_room_state(connection, lease["room_id"], "available")
                self._event(connection, family_id=lease["family_id"], event_type="handover.completed",
                            detail={"handover_id": handover["handover_id"], "lease_id": lease["lease_id"],
                                    "kind": "exit", "reason": "auto_release"}, actor="system")
                return "room_released"
            elif kind == "housing.doc_nudge":
                app = connection.execute(
                    "SELECT * FROM hs_applications WHERE application_id=?",
                    (job["subject_id"],)).fetchone()
                if not app or dict(app)["state"] != "received":
                    return "noop"
                self._event(connection, family_id=dict(app)["family_id"],
                            event_type="application.doc_nudge",
                            detail={"application_id": job["subject_id"],
                                    "missing_docs": json.loads(dict(app)["missing_docs_json"])},
                            actor="system")
                return "nudged"
            return "noop"

    # ------------------------------------------------------------ 解释

    def family_timeline(self, context: AccessContext, family_id: str) -> list[dict]:
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hs_events WHERE family_id=? ORDER BY occurred_at,event_id",
                (family_id,)).fetchall()
            return [{"occurred_at": r["occurred_at"], "event_type": r["event_type"],
                     "actor_id": r["actor_id"], "detail": json.loads(r["detail_json"])} for r in rows]

    def explain_support(self, context: AccessContext, family_id: str) -> dict:
        """组装一名申请人在不同阶段获得、失去或转换住房支持的完整解释。"""
        self._family_scope(context, family_id)
        with self.database.connect() as connection:
            family = self._family_view(
                connection, self._row(connection, "SELECT * FROM hs_families WHERE family_id=?",
                                      (family_id,)))
            eligibilities = [dict(r) for r in connection.execute(
                "SELECT * FROM hs_eligibility WHERE family_id=? ORDER BY effective_from",
                (family_id,)).fetchall()]
            leases = [dict(r) for r in connection.execute(
                "SELECT * FROM hs_leases WHERE family_id=? ORDER BY start_at", (family_id,)).fetchall()]
            payments = [dict(r) for r in connection.execute(
                "SELECT * FROM hs_payments WHERE family_id=? ORDER BY paid_at", (family_id,)).fetchall()]
            adjustments = [dict(r) for r in connection.execute(
                "SELECT * FROM hs_rent_adjustments WHERE family_id=? ORDER BY created_at",
                (family_id,)).fetchall()]
            applications = [dict(r) for r in connection.execute(
                "SELECT * FROM hs_applications WHERE family_id=? ORDER BY created_at",
                (family_id,)).fetchall()]
            wait_entries = [dict(r) for r in connection.execute(
                "SELECT w.project_id,e.* FROM hs_waitlist_entries e JOIN hs_waitlists w "
                "ON w.waitlist_id=e.waitlist_id WHERE e.family_id=? ORDER BY e.rank", (family_id,)).fetchall()]
            events = [{"occurred_at": r["occurred_at"], "event_type": r["event_type"],
                       "actor_id": r["actor_id"], "detail": json.loads(r["detail_json"])}
                      for r in connection.execute(
                          "SELECT * FROM hs_events WHERE family_id=? ORDER BY occurred_at,event_id",
                          (family_id,)).fetchall()]

        stages = []
        now = self.clock.now()
        for elig in eligibilities:
            snapshot = json.loads(elig["fact_snapshot_json"])
            related_leases = [l for l in leases if l["program"] == elig["program"]
                              and l["start_at"] >= elig["effective_from"]
                              and (elig["effective_to"] is None
                                   or l["start_at"] < elig["effective_to"]
                                   or l.get("eligibility_id") == elig["eligibility_id"])]
            related_payments = [p for l in related_leases for p in payments
                                if p["lease_id"] == l["lease_id"]]
            display_state = elig["state"]
            if display_state == "active" and elig["effective_to"] and elig["effective_to"] <= now:
                display_state = "expired"
            stages.append({
                "program": elig["program"],
                "program_label": PROGRAM_LABELS[elig["program"]],
                "eligibility_id": elig["eligibility_id"],
                "state": elig["state"],
                "display_state": display_state,
                "effective_from": elig["effective_from"],
                "effective_to": elig["effective_to"],
                "policy_digest": elig["policy_digest"],
                "facts": snapshot["facts"],
                "care_dependents_at_decision": snapshot.get("care_dependents", 0),
                "superseded_by": elig["superseded_by"],
                "leases": [{"lease_id": l["lease_id"], "room_id": l["room_id"],
                            "start_at": l["start_at"], "end_at": l["end_at"],
                            "state": l["state"], "monthly_rent_minor": l["monthly_rent_minor"],
                            "handover_completed_at": l["handover_completed_at"],
                            "predecessor_lease_id": l["predecessor_lease_id"]}
                           for l in related_leases],
                "rent_paid_minor": sum(p["amount_minor"] for p in related_payments
                                       if p["kind"] == "rent"),
            })

        narrative: list[str] = []
        for stage in stages:
            if stage["display_state"] == "superseded":
                line = (f"{stage['effective_from'][:10]} 起获得{stage['program_label']}支持"
                        f"（依据政策 {stage['policy_digest'][:10]}…），"
                        f"{stage['effective_to'][:10] if stage['effective_to'] else ''}起转换至后续保障后截止")
            elif stage["display_state"] == "expired":
                line = (f"{stage['effective_from'][:10]} 起获得{stage['program_label']}支持，"
                        f"{stage['effective_to'][:10]}到期失去，未再续认")
            elif stage["display_state"] == "revoked":
                line = f"{stage['effective_from'][:10]} 起获得{stage['program_label']}支持，后被取消"
            else:
                line = f"{stage['effective_from'][:10]} 起获得{stage['program_label']}支持"
                if stage["effective_to"]:
                    line += f"，有效期至 {stage['effective_to'][:10]}"
            if stage["leases"]:
                line += f"；已履行 {len(stage['leases'])} 段租约，实缴租金 {stage['rent_paid_minor']} 分"
            narrative.append(line)
        for entry in wait_entries:
            narrative.append(
                f"轮候项目 {entry['project_id']} 冻结名次 {entry['rank']}（历史保障 {entry['carry_days']} 天，"
                f"照顾优先 {entry['care_bonus']} 天），当前状态 {entry['state']}")

        return {"family": {k: family[k] for k in
                           ("family_id", "label", "applicant_id", "state", "version")},
                "members_now": [m for m in family["members"]
                                if not m["valid_to"] or m["valid_to"] > self.clock.now()],
                "members_all": family["members"],
                "stages": stages,
                "applications": [{"application_id": a["application_id"], "program": a["program"],
                                  "state": a["state"], "eligibility_id": a["eligibility_id"]}
                                 for a in applications],
                "waitlist_entries": wait_entries,
                "payments_total_minor": sum(p["amount_minor"] for p in payments),
                "rent_adjustments": [{"adjustment_id": a["adjustment_id"], "lease_id": a["lease_id"],
                                      "state": a["state"], "reduction_minor": a["reduction_minor"],
                                      "effective_from_period": a["effective_from_period"]}
                                     for a in adjustments],
                "narrative": narrative,
                "events": events}
