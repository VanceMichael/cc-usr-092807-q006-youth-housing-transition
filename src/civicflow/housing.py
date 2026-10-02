"""住房保障子域：资格迁移台账。

设计要点：
- 家庭、成员、房源、房间、证明、申请、资格认定、租约、租金、减免、交接、轮候全部按生效时间保存；
  履行事实（租期、缴费、轮候、需照料成员）只追加、永不被后续资格变化重写。
- 资格认定带政策版本与要素快照，effective_from 只影响之后的安排。
- 同一证明重复提交不产生第二次资格；同周期内容冲突暂停相关申请。
- 经办人不能审批自己录入的例外（减免/资格决定）。
- 换房、退租必须先完成交接才能释放房源、激活下一租约。
- 轮候规则与入队顺位冻结；offer 过期保留原顺位，递补按冻结顺位进行。
- 申请人只能查看本家庭资料（family 作用域）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json, digest_json
from .jobs import JobQueue
from .outbox import Outbox
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant
from . import housing_policies as policies

REQUIRED_DOCS = {
    policies.STATION: (),
    policies.AFFORDABLE_RENTAL: ("employment", "social_security"),
    policies.PUBLIC_RENTAL: ("employment", "social_security", "income", "housing_difficulty"),
    policies.SETTLED: ("employment", "social_security", "family"),
}


def applicant_access(person_id: str, family_id: str) -> AccessContext:
    """申请人上下文：只能看到自己家庭，没有任何写权限。"""
    return AccessContext(actor_id=person_id, permissions=frozenset({"read:housing"}),
                         scopes=frozenset({f"family:{family_id}"}))


def staff_access(actor_id: str, *, permissions: frozenset[str] | None = None) -> AccessContext:
    """工作人员上下文：默认拥有住房业务全部读写与审批权限（跨家庭可见）。"""
    perms = permissions if permissions is not None else frozenset({
        "read:housing", "write:housing", "decide:housing", "approve:housing", "housing:staff"})
    return AccessContext(actor_id=actor_id, permissions=perms, scopes=frozenset({"*"}), reveal_sensitive=True)


@dataclass(frozen=True)
class HousingService:
    database: Database
    clock: Clock
    jobs: JobQueue
    outbox: Outbox

    # ---------- 内部工具 ----------

    def _now(self) -> str:
        return self.clock.now()

    @staticmethod
    def _row(row) -> dict:
        return dict(row) if row is not None else None

    def _can_read_family(self, context: AccessContext, family_id: str) -> None:
        if context.allows("housing:staff"):
            return
        context.require("read:housing")
        if not context.has_scope(f"family:{family_id}"):
            raise PermissionDenied("申请人只能查看本家庭资料")

    def _require_staff(self, context: AccessContext, permission: str) -> None:
        context.require(permission)

    def _emit(self, topic: str, aggregate_id: str, payload: dict, *, c=None) -> None:
        if c is not None:
            self.outbox.enqueue_with(c, topic=topic, aggregate_id=aggregate_id, payload=payload)
        else:
            self.outbox.enqueue(topic=topic, aggregate_id=aggregate_id, payload=payload)

    def _schedule(self, job_type: str, subject_id: str, run_at: str, payload: dict, *, c=None) -> str:
        if c is not None:
            return self.jobs.schedule_with(c, job_type=job_type, subject_id=subject_id, run_at=run_at, payload=payload)
        return self.jobs.schedule(job_type=job_type, subject_id=subject_id, run_at=run_at, payload=payload)

    # ---------- 家庭与成员 ----------

    def create_family(self, context: AccessContext, name: str) -> dict:
        self._require_staff(context, "write:housing")
        name = name.strip()
        if not name:
            raise ValidationError("家庭名称不能为空")
        family_id = new_id("family"); now = self._now()
        with self.database.transaction() as c:
            c.execute("INSERT INTO hh_families(family_id,name,version,created_by,created_at,updated_at) VALUES(?,?,1,?,?,?)",
                      (family_id, name, context.actor_id, now, now))
        self._emit("housing.family", family_id, {"family_id": family_id, "event": "created"})
        return {"family_id": family_id, "name": name}

    def add_member(self, context: AccessContext, family_id: str, person_id: str, role: str,
                   valid_from: str, *, needs_care: bool = False, valid_to: str | None = None) -> dict:
        self._require_staff(context, "write:housing")
        role = role.strip()
        if not role:
            raise ValidationError("成员关系不能为空")
        valid_from = canonical_instant(valid_from)
        valid_to = canonical_instant(valid_to) if valid_to else None
        if valid_to and parse_instant(valid_to) <= parse_instant(valid_from):
            raise ValidationError("成员关系结束时间必须晚于生效时间")
        member_id = new_id("member"); now = self._now()
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM hh_families WHERE family_id=?", (family_id,)).fetchone():
                raise NotFoundError("家庭不存在")
            dup = c.execute("SELECT 1 FROM hh_members WHERE family_id=? AND person_id=? AND valid_to IS NULL",
                            (family_id, person_id)).fetchone()
            if dup:
                raise ConflictError("该成员已在家庭中且关系未结束")
            c.execute("""INSERT INTO hh_members(member_id,family_id,person_id,role,needs_care,valid_from,valid_to,created_by,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (member_id, family_id, person_id, role, 1 if needs_care else 0, valid_from, valid_to, context.actor_id, now))
        self._emit("housing.family", family_id, {"event": "member_added", "person_id": person_id, "role": role,
                                                 "needs_care": needs_care})
        return {"member_id": member_id, "family_id": family_id, "person_id": person_id, "role": role,
                "needs_care": needs_care, "valid_from": valid_from, "valid_to": valid_to}

    def end_membership(self, context: AccessContext, member_id: str, valid_to: str, *, reason: str) -> dict:
        self._require_staff(context, "write:housing")
        valid_to = canonical_instant(valid_to)
        if not reason.strip():
            raise ValidationError("结束家庭关系必须说明原因")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM hh_members WHERE member_id=?", (member_id,)).fetchone()
            if not row:
                raise NotFoundError("成员关系不存在")
            if row["valid_to"]:
                raise ConflictError("成员关系已经结束")
            c.execute("UPDATE hh_members SET valid_to=? WHERE member_id=?", (valid_to, member_id))
        self._emit("housing.family", row["family_id"], {"event": "member_ended", "person_id": row["person_id"],
                                                        "valid_to": valid_to, "reason": reason})
        return {"member_id": member_id, "valid_to": valid_to}

    def members_as_of(self, context: AccessContext, family_id: str, *, at: str) -> list[dict]:
        self._can_read_family(context, family_id)
        instant = canonical_instant(at)
        with self.database.connect() as c:
            rows = c.execute("""SELECT * FROM hh_members WHERE family_id=? AND valid_from<=?
                                AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from,person_id""",
                             (family_id, instant, instant)).fetchall()
            return [self._row(r) for r in rows]

    def get_family(self, context: AccessContext, family_id: str) -> dict:
        self._can_read_family(context, family_id)
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM hh_families WHERE family_id=?", (family_id,)).fetchone()
            if not row:
                raise NotFoundError("家庭不存在")
            result = self._row(row)
            result["members"] = [self._row(r) for r in c.execute(
                "SELECT * FROM hh_members WHERE family_id=? AND valid_to IS NULL ORDER BY valid_from", (family_id,))]
            return result

    # ---------- 房源项目与房间 ----------

    def create_project(self, context: AccessContext, program: str, name: str) -> dict:
        self._require_staff(context, "write:housing")
        policies.assert_program(program)
        name = name.strip()
        if not name:
            raise ValidationError("项目名称不能为空")
        project_id = new_id("project"); now = self._now()
        with self.database.transaction() as c:
            c.execute("INSERT INTO hh_projects(project_id,program,name,version,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?)",
                      (project_id, program, name, context.actor_id, now, now))
        return {"project_id": project_id, "program": program, "name": name}

    def add_room(self, context: AccessContext, project_id: str, code: str) -> dict:
        self._require_staff(context, "write:housing")
        code = code.strip()
        if not code:
            raise ValidationError("房间编号不能为空")
        room_id = new_id("room"); now = self._now()
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM hh_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("项目不存在")
            if c.execute("SELECT 1 FROM hh_rooms WHERE project_id=? AND code=?", (project_id, code)).fetchone():
                raise ConflictError("房间编号在项目内重复")
            c.execute("""INSERT INTO hh_rooms(room_id,project_id,code,status,version,created_by,created_at,updated_at)
                         VALUES(?,?,?,'available',1,?,?,?)""", (room_id, project_id, code, context.actor_id, now, now))
        return {"room_id": room_id, "project_id": project_id, "code": code, "status": "available"}

    def _get_room(self, c, room_id: str):
        row = c.execute("SELECT * FROM hh_rooms WHERE room_id=?", (room_id,)).fetchone()
        if not row:
            raise NotFoundError("房间不存在")
        return row

    # ---------- 证明 ----------

    def submit_document(self, context: AccessContext, family_id: str, person_id: str, doc_type: str,
                        content: dict, issued_at: str, *, doc_subtype: str = "") -> dict:
        self._require_staff(context, "write:housing")
        if doc_type not in ("employment", "social_security", "income", "housing_difficulty", "family"):
            raise ValidationError("证明类型不合法")
        if not isinstance(content, dict) or not content:
            raise ValidationError("证明内容不能为空")
        issued_at = canonical_instant(issued_at)
        digest = digest_json({"type": doc_type, "subtype": doc_subtype, "content": content})
        period = str(content.get("period", "")).strip()
        document_id = new_id("doc"); now = self._now()
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM hh_families WHERE family_id=?", (family_id,)).fetchone():
                raise NotFoundError("家庭不存在")
            # 同一证明重复提交：不产生第二次资格，直接幂等返回。
            same = c.execute("SELECT document_id,state FROM hh_documents WHERE person_id=? AND doc_type=? AND content_digest=?",
                             (person_id, doc_type, digest)).fetchone()
            if same:
                return {"state": "duplicate", "document_id": same["document_id"], "digest": digest}
            # 同周期但内容不同 => 冲突；不同周期视为新事实（如收入变化）。
            conflict_row = None
            if period:
                conflict_row = c.execute("""SELECT document_id FROM hh_documents
                    WHERE person_id=? AND doc_type=? AND state='active' AND json_extract(payload_json,'$.period')=?""",
                    (person_id, doc_type, period)).fetchone()
            else:
                conflict_row = c.execute("""SELECT document_id FROM hh_documents
                    WHERE person_id=? AND doc_type=? AND state='active'""", (person_id, doc_type)).fetchone()
            state = "active"
            c.execute("""INSERT INTO hh_documents(document_id,family_id,person_id,doc_type,doc_subtype,content_digest,
                         payload_json,issued_at,recorded_at,recorded_by,state)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (document_id, family_id, person_id, doc_type, doc_subtype, digest, canonical_json(content),
                       issued_at, now, context.actor_id, state))
            if conflict_row:
                c.execute("UPDATE hh_documents SET state='conflicted' WHERE document_id=?", (document_id,))
                conflict_id = new_id("conflict")
                detail = f"同一人同一证明类型({doc_type})在周期 {period or '未标注'} 出现相互冲突的内容"
                c.execute("""INSERT INTO hh_document_conflicts(conflict_id,person_id,doc_type,existing_document_id,
                             incoming_document_id,detail,recorded_at) VALUES(?,?,?,?,?,?,?)""",
                          (conflict_id, person_id, doc_type, conflict_row["document_id"], document_id, detail, now))
                # 暂停依赖该证明类型的在办申请。
                suspended = []
                rows = c.execute("SELECT application_id,program FROM hh_applications WHERE family_id=? AND state IN ('draft','incomplete','pending')",
                                 (family_id,)).fetchall()
                for r in rows:
                    if doc_type in REQUIRED_DOCS.get(r["program"], ()):
                        c.execute("UPDATE hh_applications SET state='suspended',suspension_reason=?,updated_at=? WHERE application_id=?",
                                  (f"证明冲突待核查: {doc_type}", now, r["application_id"]))
                        suspended.append(r["application_id"])
                self._emit("housing.document", document_id,
                           {"event": "conflict", "family_id": family_id, "person_id": person_id,
                            "doc_type": doc_type, "conflict_id": conflict_id, "suspended": suspended}, c=c)
                return {"state": "conflicted", "document_id": document_id, "conflict_id": conflict_id,
                        "suspended_applications": suspended, "digest": digest}
            # 有效证明补齐后，自动重评该家庭的缺件申请。
            resumed = self._rescan_incomplete(c, family_id)
            self._emit("housing.document", document_id,
                       {"event": "submitted", "family_id": family_id, "person_id": person_id,
                        "doc_type": doc_type, "resumed_applications": resumed}, c=c)
            return {"state": "active", "document_id": document_id, "digest": digest,
                    "resumed_applications": resumed}

    def _rescan_incomplete(self, c, family_id: str) -> list[str]:
        """缺件申请在补件后回到待审；仍有未决证明冲突的保持暂停。"""
        resumed: list[str] = []
        rows = c.execute("SELECT * FROM hh_applications WHERE family_id=? AND state='incomplete'", (family_id,)).fetchall()
        now = self._now()
        for app in rows:
            open_conflicts = set(self._open_conflict_types(c, family_id, app["applicant_id"]))
            if open_conflicts & set(REQUIRED_DOCS.get(app["program"], ())):
                continue
            if not self._missing_docs(c, app):
                c.execute("UPDATE hh_applications SET state='pending',missing_docs='',updated_at=? WHERE application_id=?",
                          (now, app["application_id"]))
                resumed.append(app["application_id"])
        return resumed

    def resolve_document_conflict(self, context: AccessContext, conflict_id: str, *, keep: str, resolution: str) -> dict:
        """核查证明冲突：keep='existing' 或 'incoming'，随后恢复被暂停的申请。"""
        self._require_staff(context, "write:housing")
        if keep not in ("existing", "incoming"):
            raise ValidationError("必须指明保留 existing 还是 incoming 证明")
        if not resolution.strip():
            raise ValidationError("冲突处理必须说明结论")
        now = self._now()
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM hh_document_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
            if not row:
                raise NotFoundError("冲突记录不存在")
            if row["resolved_at"]:
                raise ConflictError("该证明冲突已经处理")
            keep_id = row["existing_document_id"] if keep == "existing" else row["incoming_document_id"]
            drop_id = row["incoming_document_id"] if keep == "existing" else row["existing_document_id"]
            c.execute("UPDATE hh_documents SET state='active' WHERE document_id=?", (keep_id,))
            c.execute("UPDATE hh_documents SET state='superseded' WHERE document_id=?", (drop_id,))
            c.execute("UPDATE hh_document_conflicts SET resolved_by=?,resolved_at=?,resolution=? WHERE conflict_id=?",
                      (context.actor_id, now, resolution.strip(), conflict_id))
            # 恢复被暂停的申请：重新判定缺件。
            resumed = []
            apps = c.execute("SELECT * FROM hh_applications WHERE state='suspended' AND family_id=(SELECT family_id FROM hh_documents WHERE document_id=?)",
                             (keep_id,)).fetchall()
            for app in apps:
                missing = self._missing_docs(c, app)
                new_state = "incomplete" if missing else "pending"
                c.execute("UPDATE hh_applications SET state=?,suspension_reason='',missing_docs=?,updated_at=? WHERE application_id=?",
                          (new_state, ",".join(missing), now, app["application_id"]))
                resumed.append({"application_id": app["application_id"], "state": new_state})
        self._emit("housing.document", keep_id, {"event": "conflict_resolved", "conflict_id": conflict_id, "kept": keep})
        return {"conflict_id": conflict_id, "kept_document_id": keep_id, "resumed": resumed}

    def list_documents(self, context: AccessContext, family_id: str) -> list[dict]:
        self._can_read_family(context, family_id)
        with self.database.connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM hh_documents WHERE family_id=? ORDER BY issued_at,document_id", (family_id,))]

    # ---------- 申请 ----------

    def _latest_granted(self, c, family_id: str, applicant_id: str):
        return c.execute("""SELECT * FROM hh_eligibility_decisions WHERE family_id=? AND outcome='granted'
                            ORDER BY effective_from DESC,decided_at DESC LIMIT 1""", (family_id,)).fetchone()

    def _earliest_since(self, c, family_id: str, applicant_id: str) -> str | None:
        row = c.execute("""SELECT MIN(start_at) AS s FROM hh_leases WHERE family_id=? AND state IN ('active','expiring','ended','released')""",
                        (family_id,)).fetchone()
        if row and row["s"]:
            return row["s"]
        row = c.execute("SELECT MIN(effective_from) AS s FROM hh_eligibility_decisions WHERE family_id=? AND outcome='granted'",
                        (family_id,)).fetchone()
        return row["s"] if row and row["s"] else None

    def _missing_docs(self, c, app) -> list[str]:
        required = REQUIRED_DOCS.get(app["program"], ())
        missing = []
        for doc_type in required:
            row = c.execute("SELECT 1 FROM hh_documents WHERE family_id=? AND person_id=? AND doc_type=? AND state='active' LIMIT 1",
                            (app["family_id"], app["applicant_id"], doc_type)).fetchone()
            if not row:
                missing.append(doc_type)
        return missing

    def _open_conflict_types(self, c, family_id: str, person_id: str) -> list[str]:
        rows = c.execute("""SELECT DISTINCT dc.doc_type FROM hh_document_conflicts dc
                            JOIN hh_documents d ON d.document_id IN (dc.existing_document_id, dc.incoming_document_id)
                            WHERE dc.resolved_at IS NULL AND d.family_id=?
                              AND (dc.person_id=? OR d.person_id=?)""",
                         (family_id, person_id, person_id)).fetchall()
        return [r["doc_type"] for r in rows]

    def apply(self, context: AccessContext, family_id: str, applicant_id: str, program: str, round_id: str,
              factors: dict, *, request_key: str) -> dict:
        self._require_staff(context, "write:housing")
        policies.assert_program(program)
        round_id = round_id.strip()
        if not round_id:
            raise ValidationError("轮次标识不能为空")
        parsed = policies.factors_from_mapping(factors)
        application_id = new_id("app"); now = self._now()
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM hh_families WHERE family_id=?", (family_id,)).fetchone():
                raise NotFoundError("家庭不存在")
            dup = c.execute("SELECT application_id FROM hh_applications WHERE applicant_id=? AND program=? AND state IN ('draft','incomplete','pending','suspended','approved')",
                            (applicant_id, program)).fetchone()
            if dup:
                raise ConflictError("该保障类型已有在办申请，重复申请不产生第二次资格")
            granted = self._latest_granted(c, family_id, applicant_id)
            previous_program = granted["program"] if granted else None
            if not policies.can_migrate(previous_program, program):
                raise ValidationError(f"不允许从 {policies.PROGRAM_LABELS.get(previous_program, previous_program)} 迁回 {policies.PROGRAM_LABELS[program]}")
            base = self._earliest_since(c, family_id, applicant_id)
            carry_days = 0
            if base:
                carry_days = max(0, (parse_instant(now) - parse_instant(base)).days)
            c.execute("""INSERT INTO hh_applications(application_id,family_id,applicant_id,program,round_id,state,version,
                         recorded_by,recorded_at,updated_at,prev_application_id,wait_time_before_days,carrying_text,factors_json)
                         VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                      (application_id, family_id, applicant_id, program, round_id, "pending", context.actor_id, now, now,
                       granted["application_id"] if granted else None, carry_days,
                       f"携带既往轮候 {carry_days} 天；已缴租金与已履行租期继续保留" if granted else "",
                       canonical_json(parsed.snapshot())))
            app = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (application_id,)).fetchone()
            missing = self._missing_docs(c, app)
            open_conflicts = sorted(set(self._open_conflict_types(c, family_id, applicant_id))
                                    & set(REQUIRED_DOCS.get(program, ())))
            if open_conflicts:
                # 内容冲突先暂停相关申请，即使材料表面齐全也不进入审核。
                c.execute("UPDATE hh_applications SET state='suspended',missing_docs=?,suspension_reason=? WHERE application_id=?",
                          (",".join(missing), "证明冲突待核查: " + ",".join(open_conflicts), application_id))
            elif missing:
                c.execute("UPDATE hh_applications SET state='incomplete',missing_docs=? WHERE application_id=?",
                          (",".join(missing), application_id))
                self._schedule("missing_docs", application_id,
                               (parse_instant(now) + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                               {"application_id": application_id, "missing": missing}, c=c)
        state = "suspended" if open_conflicts else ("incomplete" if missing else "pending")
        self._emit("housing.application", application_id,
                   {"event": "submitted", "family_id": family_id, "program": program, "state": state,
                    "carry_wait_days": carry_days, "missing_docs": missing,
                    "open_conflicts": open_conflicts})
        return {"application_id": application_id, "state": state, "program": program,
                "previous_program": previous_program, "carry_wait_days": carry_days,
                "missing_docs": missing, "open_conflicts": open_conflicts}

    def get_application(self, context: AccessContext, application_id: str) -> dict:
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (application_id,)).fetchone()
            if not row:
                raise NotFoundError("申请不存在")
            self._can_read_family(context, row["family_id"])
            result = self._row(row)
            result["factors"] = json.loads(row["factors_json"])
            return result

    def list_applications(self, context: AccessContext, family_id: str) -> list[dict]:
        self._can_read_family(context, family_id)
        with self.database.connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM hh_applications WHERE family_id=? ORDER BY recorded_at", (family_id,))]

    # ---------- 资格认定 ----------

    def decide_eligibility(self, context: AccessContext, application_id: str, outcome: str, reason: str, *,
                           effective_from: str | None = None, policy_version: str | None = None) -> dict:
        """审批资格。经办人(recorded_by)不能审批自己录入的申请。"""
        self._require_staff(context, "decide:housing")
        if outcome not in ("granted", "denied"):
            raise ValidationError("认定结论必须是 granted 或 denied")
        if not reason.strip():
            raise ValidationError("资格认定必须说明理由")
        effective_from = canonical_instant(effective_from) if effective_from else self._now()
        now = self._now()
        with self.database.transaction() as c:
            app = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (application_id,)).fetchone()
            if not app:
                raise NotFoundError("申请不存在")
            # 职责隔离：录入经办人不能审批自己录入的申请。
            assert_distinct(app["recorded_by"], context.actor_id)
            if app["state"] == "suspended":
                raise ConflictError("申请因证明冲突已暂停，需先核查证明")
            if app["state"] == "incomplete":
                raise ConflictError("申请材料不完整，不能作出资格认定")
            if c.execute("SELECT 1 FROM hh_eligibility_decisions WHERE application_id=?", (application_id,)).fetchone():
                raise ConflictError("该申请已有资格认定，不能重复认定")
            factors = policies.factors_from_mapping(json.loads(app["factors_json"]))
            criteria = policies.criteria_for(app["program"], policy_version)
            passed, fail_reasons = criteria.evaluate(factors)
            if outcome == "granted" and not passed:
                raise ValidationError("不符合准入条件：" + "；".join(fail_reasons))
            decision_id = new_id("elig")
            c.execute("""INSERT INTO hh_eligibility_decisions(decision_id,application_id,family_id,program,outcome,
                         effective_from,policy_version,criteria_snapshot_json,factors_json,carry_wait_days,decided_by,decided_at,reason)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (decision_id, application_id, app["family_id"], app["program"], outcome, effective_from,
                       criteria.version, canonical_json(criteria.snapshot()), canonical_json(factors.snapshot()),
                       app["wait_time_before_days"], context.actor_id, now, reason.strip()))
            new_state = "approved" if outcome == "granted" else "rejected"
            c.execute("UPDATE hh_applications SET state=?,decision_by=?,decision_at=?,decision_reason=?,updated_at=? WHERE application_id=?",
                      (new_state, context.actor_id, now, reason.strip(), now, application_id))
        self._emit("housing.application", application_id,
                   {"event": "decided", "outcome": outcome, "program": app["program"],
                    "effective_from": effective_from, "policy_version": criteria.version})
        return {"decision_id": decision_id, "application_id": application_id, "outcome": outcome,
                "effective_from": effective_from, "policy_version": criteria.version,
                "passed": passed, "fail_reasons": fail_reasons, "carry_wait_days": app["wait_time_before_days"]}

    # ---------- 租约与房间占用 ----------

    def admit(self, context: AccessContext, application_id: str, room_id: str, start_at: str, end_at: str) -> dict:
        """凭已批准申请建立待生效租约并锁定房间（reserved）。激活需无在住租约或已完成交接。"""
        self._require_staff(context, "write:housing")
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        if parse_instant(end_at) <= parse_instant(start_at):
            raise ValidationError("租约结束时间必须晚于开始时间")
        now = self._now()
        with self.database.transaction() as c:
            app = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (application_id,)).fetchone()
            if not app:
                raise NotFoundError("申请不存在")
            if app["state"] != "approved":
                raise ConflictError("只有审批通过的申请可以办理入住")
            room = self._get_room(c, room_id)
            project = c.execute("SELECT * FROM hh_projects WHERE project_id=?", (room["project_id"],)).fetchone()
            if project["program"] != app["program"]:
                raise ValidationError("房间所属保障类型与申请不一致")
            if room["status"] != "available":
                raise ConflictError(f"房间当前状态 {room['status']}，不能重复占用")
            overlap = c.execute("""SELECT 1 FROM hh_leases WHERE room_id=? AND state IN ('pending','active','expiring')
                                   AND start_at<? AND end_at>?""", (room_id, end_at, start_at)).fetchone()
            if overlap:
                raise ConflictError("该房间在所选时段已有未结束租约")
            pending_for_family = c.execute("SELECT 1 FROM hh_leases WHERE family_id=? AND state='pending'",
                                           (app["family_id"],)).fetchone()
            if pending_for_family:
                raise ConflictError("家庭已有待生效租约，请先完成交接或取消")
            lease_id = new_id("lease")
            c.execute("""INSERT INTO hh_leases(lease_id,family_id,applicant_id,program,project_id,room_id,start_at,end_at,
                         state,version,application_id,created_by,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,'pending',1,?,?,?,?)""",
                      (lease_id, app["family_id"], app["applicant_id"], app["program"], room["project_id"], room_id,
                       start_at, end_at, application_id, context.actor_id, now, now))
            c.execute("UPDATE hh_rooms SET status='reserved',version=version+1,updated_at=? WHERE room_id=?", (now, room_id))
            # 到期与房源释放检查任务（崩溃恢复后继续）。
            self._schedule("lease_expiry", lease_id, end_at, {"lease_id": lease_id, "room_id": room_id}, c=c)
            self._schedule("room_release", lease_id, end_at,
                           {"lease_id": lease_id, "room_id": room_id, "family_id": app["family_id"]}, c=c)
        self._emit("housing.lease", lease_id, {"event": "reserved", "room_id": room_id, "application_id": application_id})
        return {"lease_id": lease_id, "state": "pending", "room_id": room_id, "start_at": start_at, "end_at": end_at}

    def _active_lease_of_family(self, c, family_id: str, exclude: str | None = None):
        sql = "SELECT * FROM hh_leases WHERE family_id=? AND state IN ('active','expiring')"
        params: list[object] = [family_id]
        if exclude:
            sql += " AND lease_id<>?"; params.append(exclude)
        sql += " ORDER BY end_at DESC LIMIT 1"
        return c.execute(sql, params).fetchone()

    def activate_lease(self, context: AccessContext, lease_id: str) -> dict:
        self._require_staff(context, "write:housing")
        with self.database.transaction() as c:
            lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not lease:
                raise NotFoundError("租约不存在")
            if lease["state"] != "pending":
                raise ConflictError(f"租约状态为 {lease['state']}，不能激活")
            blocking = self._active_lease_of_family(c, lease["family_id"])
            if blocking:
                done = c.execute("""SELECT 1 FROM hh_handovers WHERE from_lease_id=? AND state='completed'
                                    AND (to_lease_id=? OR to_lease_id IS NULL)""",
                                 (blocking["lease_id"], lease_id)).fetchone()
                if not done:
                    raise ConflictError("原住房尚未完成交接，不能激活新租约")
            now = self._now()
            c.execute("UPDATE hh_leases SET state='active',start_at=?,version=version+1,updated_at=? WHERE lease_id=?",
                      (now, now, lease_id))
            c.execute("UPDATE hh_rooms SET status='occupied',version=version+1,updated_at=? WHERE room_id=?",
                      (now, lease["room_id"]))
        self._emit("housing.lease", lease_id, {"event": "activated", "room_id": lease["room_id"]})
        return {"lease_id": lease_id, "state": "active", "activated_at": now}

    def get_lease(self, context: AccessContext, lease_id: str) -> dict:
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not row:
                raise NotFoundError("租约不存在")
            self._can_read_family(context, row["family_id"])
            return self._row(row)

    def list_leases(self, context: AccessContext, family_id: str) -> list[dict]:
        self._can_read_family(context, family_id)
        with self.database.connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM hh_leases WHERE family_id=? ORDER BY start_at", (family_id,))]

    # ---------- 租金与减免 ----------

    def apply_reduction(self, context: AccessContext, lease_id: str, percent: int, effective_from: str, *, reason: str) -> dict:
        """经办人录入减免例外（待审批），不能自己审批。"""
        self._require_staff(context, "write:housing")
        if not isinstance(percent, int) or not (0 < percent <= 100):
            raise ValidationError("减免比例必须是 1..100 的整数")
        if not reason.strip():
            raise ValidationError("减免必须说明原因")
        effective_from = canonical_instant(effective_from)
        reduction_id = new_id("reduction"); now = self._now()
        with self.database.transaction() as c:
            lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not lease:
                raise NotFoundError("租约不存在")
            c.execute("""INSERT INTO hh_reductions(reduction_id,lease_id,family_id,percent,effective_from,state,recorded_by,recorded_at)
                         VALUES(?,?,?,?,?,'pending',?,?)""",
                      (reduction_id, lease_id, lease["family_id"], percent, effective_from, context.actor_id, now))
        self._emit("housing.lease", lease_id, {"event": "reduction_requested", "reduction_id": reduction_id, "percent": percent})
        return {"reduction_id": reduction_id, "state": "pending", "percent": percent, "effective_from": effective_from}

    def approve_reduction(self, context: AccessContext, reduction_id: str, *, approve: bool = True) -> dict:
        self._require_staff(context, "approve:housing")
        now = self._now()
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM hh_reductions WHERE reduction_id=?", (reduction_id,)).fetchone()
            if not row:
                raise NotFoundError("减免记录不存在")
            if row["state"] != "pending":
                raise ConflictError("该减免已经审批")
            # 经办人不能审批自己录入的例外。
            assert_distinct(row["recorded_by"], context.actor_id)
            state = "approved" if approve else "rejected"
            c.execute("UPDATE hh_reductions SET state=?,approved_by=?,approved_at=? WHERE reduction_id=?",
                      (state, context.actor_id, now, reduction_id))
        self._emit("housing.lease", row["lease_id"], {"event": "reduction_decided", "reduction_id": reduction_id, "state": state})
        return {"reduction_id": reduction_id, "state": state}

    def _current_reduction(self, c, lease_id: str, period_start: str):
        return c.execute("""SELECT * FROM hh_reductions WHERE lease_id=? AND state='approved' AND effective_from<=?
                            ORDER BY effective_from DESC,reduction_id DESC LIMIT 1""",
                         (lease_id, period_start)).fetchone()

    def bill_rent(self, context: AccessContext, lease_id: str, period_start: str, period_end: str, list_rent_minor: int) -> dict:
        self._require_staff(context, "write:housing")
        period_start = canonical_instant(period_start); period_end = canonical_instant(period_end)
        if parse_instant(period_end) <= parse_instant(period_start):
            raise ValidationError("账期结束时间必须晚于开始时间")
        if not isinstance(list_rent_minor, int) or list_rent_minor <= 0:
            raise ValidationError("租金必须是以分为单位的正整数")
        rent_id = new_id("rent"); now = self._now()
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM hh_leases WHERE lease_id=?", (lease_id,)).fetchone():
                raise NotFoundError("租约不存在")
            dup = c.execute("SELECT 1 FROM hh_rent_records WHERE lease_id=? AND period_start=? AND period_end=?",
                            (lease_id, period_start, period_end)).fetchone()
            if dup:
                raise ConflictError("该账期租金已经生成，不能重复出账")
            reduction = self._current_reduction(c, lease_id, period_start)
            reduction_id = reduction["reduction_id"] if reduction else None
            due = list_rent_minor - list_rent_minor * reduction["percent"] // 100 if reduction else list_rent_minor
            c.execute("""INSERT INTO hh_rent_records(rent_id,lease_id,period_start,period_end,list_rent_minor,due_minor,
                         reduction_id,billed_by,billed_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (rent_id, lease_id, period_start, period_end, list_rent_minor, due, reduction_id, context.actor_id, now))
        return {"rent_id": rent_id, "list_rent_minor": list_rent_minor, "due_minor": due,
                "reduction_id": reduction_id}

    def record_payment(self, context: AccessContext, rent_id: str, paid_minor: int, paid_at: str) -> dict:
        """登记已缴租金——只追加事实，任何后续资格/减免变化都不改写。"""
        self._require_staff(context, "write:housing")
        paid_at = canonical_instant(paid_at)
        if not isinstance(paid_minor, int) or paid_minor <= 0:
            raise ValidationError("缴费金额必须是以分为单位的正整数")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM hh_rent_records WHERE rent_id=?", (rent_id,)).fetchone()
            if not row:
                raise NotFoundError("租金账单不存在")
            if row["paid_minor"]:
                raise ConflictError("该账单已登记缴费，缴费事实不可改写")
            c.execute("UPDATE hh_rent_records SET paid_minor=?,paid_at=?,paid_by=? WHERE rent_id=?",
                      (paid_minor, paid_at, context.actor_id, rent_id))
            family_id = c.execute("SELECT family_id FROM hh_leases WHERE lease_id=?", (row["lease_id"],)).fetchone()["family_id"]
        self._emit("housing.lease", row["lease_id"], {"event": "rent_paid", "rent_id": rent_id, "amount_minor": paid_minor})
        return {"rent_id": rent_id, "paid_minor": paid_minor, "paid_at": paid_at, "family_id": family_id}

    def list_rent(self, context: AccessContext, lease_id: str) -> list[dict]:
        with self.database.connect() as c:
            lease = c.execute("SELECT family_id FROM hh_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not lease:
                raise NotFoundError("租约不存在")
            self._can_read_family(context, lease["family_id"])
            return [self._row(r) for r in c.execute(
                "SELECT * FROM hh_rent_records WHERE lease_id=? ORDER BY period_start", (lease_id,))]

    def total_paid(self, family_id: str) -> int:
        with self.database.connect() as c:
            row = c.execute("""SELECT COALESCE(SUM(r.paid_minor),0) AS n FROM hh_rent_records r
                               JOIN hh_leases l ON l.lease_id=r.lease_id WHERE l.family_id=?""", (family_id,)).fetchone()
            return int(row["n"])

    # ---------- 交接 ----------

    def request_handover(self, context: AccessContext, from_lease_id: str, kind: str, *,
                         to_lease_id: str | None = None, checklist: list[str] | None = None,
                         scheduled_at: str) -> dict:
        self._require_staff(context, "write:housing")
        if kind not in ("transfer", "exit"):
            raise ValidationError("交接类型必须是 transfer 或 exit")
        scheduled_at = canonical_instant(scheduled_at)
        items = checklist or ["钥匙归还", "费用结清", "房屋查验"]
        if not all(isinstance(i, str) and i.strip() for i in items):
            raise ValidationError("交接清单项必须是非空字符串")
        handover_id = new_id("handover"); now = self._now()
        with self.database.transaction() as c:
            frm = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (from_lease_id,)).fetchone()
            if not frm:
                raise NotFoundError("原租约不存在")
            if frm["state"] not in ("active", "expiring"):
                raise ConflictError(f"原租约状态 {frm['state']}，不能发起交接")
            if kind == "transfer":
                if not to_lease_id:
                    raise ValidationError("换房交接必须指定新租约")
                to = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (to_lease_id,)).fetchone()
                if not to or to["state"] != "pending":
                    raise ValidationError("新租约必须处于待生效状态")
                if to["family_id"] != frm["family_id"]:
                    raise ValidationError("新旧租约必须属于同一家庭")
            else:
                to_lease_id = None
            if c.execute("SELECT 1 FROM hh_handovers WHERE from_lease_id=? AND state='pending'", (from_lease_id,)).fetchone():
                raise ConflictError("该租约已有未完成交接")
            c.execute("""INSERT INTO hh_handovers(handover_id,family_id,from_lease_id,to_lease_id,kind,state,checklist_json,
                         scheduled_at,recorded_by,recorded_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)""",
                      (handover_id, frm["family_id"], from_lease_id, to_lease_id, kind, canonical_json(items),
                       scheduled_at, context.actor_id, now))
            c.execute("UPDATE hh_rooms SET status='releasing',version=version+1,updated_at=? WHERE room_id=?",
                      (now, frm["room_id"]))
        self._emit("housing.lease", from_lease_id, {"event": "handover_requested", "handover_id": handover_id, "kind": kind})
        return {"handover_id": handover_id, "state": "pending", "kind": kind, "checklist": items}

    def complete_handover(self, context: AccessContext, handover_id: str) -> dict:
        self._require_staff(context, "write:housing")
        with self.database.transaction() as c:
            h = c.execute("SELECT * FROM hh_handovers WHERE handover_id=?", (handover_id,)).fetchone()
            if not h:
                raise NotFoundError("交接记录不存在")
            if h["state"] != "pending":
                raise ConflictError("交接已经完成")
            now = self._now()
            frm = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (h["from_lease_id"],)).fetchone()
            c.execute("UPDATE hh_handovers SET state='completed',completed_at=?,completed_by=? WHERE handover_id=?",
                      (now, context.actor_id, handover_id))
            c.execute("UPDATE hh_leases SET state='released',version=version+1,updated_at=? WHERE lease_id=?",
                      (now, h["from_lease_id"]))
            old_room = frm["room_id"]
            c.execute("UPDATE hh_rooms SET status='available',version=version+1,updated_at=? WHERE room_id=?", (now, old_room))
            new_lease_id = h["to_lease_id"]
            if new_lease_id:
                to = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (new_lease_id,)).fetchone()
                if to["state"] != "pending":
                    raise ConflictError("新租约状态异常，无法随交接激活")
                c.execute("UPDATE hh_leases SET state='active',start_at=?,version=version+1,updated_at=? WHERE lease_id=?",
                          (now, now, new_lease_id))
                c.execute("UPDATE hh_rooms SET status='occupied',version=version+1,updated_at=? WHERE room_id=?",
                          (now, to["room_id"]))
            released = {"room_id": old_room, "project_id": frm["project_id"], "program": frm["program"]}
            # 房源释放后立即按冻结顺位递补。
            offered = self._backfill_room(c, old_room)
        self._emit("housing.lease", h["from_lease_id"],
                   {"event": "handover_completed", "handover_id": handover_id, "new_lease_id": new_lease_id,
                    "released_room": old_room})
        return {"handover_id": handover_id, "state": "completed", "released": released, "backfill": offered}

    # ---------- 轮候 ----------

    def create_waitlist(self, context: AccessContext, program: str, round_id: str, *,
                        project_id: str | None = None, ranking_rule: dict | None = None) -> dict:
        self._require_staff(context, "write:housing")
        policies.assert_program(program)
        rule = ranking_rule or {"order": ["wait_since", "needs_care_desc", "enqueued_at"],
                                "note": "轮候时间优先，需照料成员优先，同条件按入队先后；规则入队后冻结"}
        waitlist_id = new_id("waitlist"); now = self._now()
        with self.database.transaction() as c:
            c.execute("""INSERT INTO hh_waitlists(waitlist_id,program,project_id,round_id,policy_version,ranking_rule_json,frozen_at,created_by,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (waitlist_id, program, project_id, round_id, policies.DEFAULT_POLICY_VERSION,
                       canonical_json(rule), now, context.actor_id, now))
        return {"waitlist_id": waitlist_id, "program": program, "round_id": round_id,
                "project_id": project_id, "ranking_rule": rule, "frozen_at": now}

    def enqueue(self, context: AccessContext, waitlist_id: str, family_id: str, application_id: str,
                *, base_since: str | None = None) -> dict:
        self._require_staff(context, "write:housing")
        entry_id = new_id("wlentry"); now = self._now()
        with self.database.transaction() as c:
            wl = c.execute("SELECT * FROM hh_waitlists WHERE waitlist_id=?", (waitlist_id,)).fetchone()
            if not wl:
                raise NotFoundError("轮候册不存在")
            app = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (application_id,)).fetchone()
            if not app or app["family_id"] != family_id:
                raise ValidationError("申请与家庭不匹配")
            if app["program"] != wl["program"]:
                raise ValidationError("申请保障类型与轮候册不一致")
            if c.execute("SELECT 1 FROM hh_waitlist_entries WHERE waitlist_id=? AND family_id=?",
                         (waitlist_id, family_id)).fetchone():
                raise ConflictError("家庭已在该轮候册中")
            rank_row = c.execute("SELECT COALESCE(MAX(rank),0)+1 AS r FROM hh_waitlist_entries WHERE waitlist_id=?",
                                 (waitlist_id,)).fetchone()
            rank = int(rank_row["r"])
            if base_since is None:
                base = self._earliest_since(c, family_id, app["applicant_id"]) or app["recorded_at"]
            else:
                base = canonical_instant(base_since)
            c.execute("""INSERT INTO hh_waitlist_entries(entry_id,waitlist_id,family_id,application_id,rank,enqueued_at,
                         base_since,state,updated_at) VALUES(?,?,?,?,?,?,?,'waiting',?)""",
                      (entry_id, waitlist_id, family_id, application_id, rank, now, base, now))
            c.execute("""INSERT INTO hh_waitlist_events(event_id,waitlist_id,family_id,kind,rank_snapshot,occurred_at,actor)
                         VALUES(?,?,?, 'enqueued',?,?,?)""",
                      (new_id("wlevt"), waitlist_id, family_id, rank, now, context.actor_id))
        return {"entry_id": entry_id, "rank": rank, "state": "waiting", "base_since": base}

    def _head_waiting(self, c, waitlist_id: str):
        return c.execute("""SELECT * FROM hh_waitlist_entries WHERE waitlist_id=? AND state='waiting'
                            ORDER BY rank LIMIT 1""", (waitlist_id,)).fetchone()

    def _waitlists_for(self, c, program: str, project_id: str):
        rows = c.execute("""SELECT * FROM hh_waitlists WHERE program=? AND (project_id IS NULL OR project_id=?)
                            ORDER BY frozen_at""", (program, project_id)).fetchall()
        return rows

    def make_offer(self, context: AccessContext, waitlist_id: str, room_id: str, *, valid_hours: int = 48) -> dict:
        self._require_staff(context, "write:housing")
        if valid_hours < 1:
            raise ValidationError("offer 有效期不合法")
        now_dt = parse_instant(self._now()); expires = (now_dt + timedelta(hours=valid_hours)).isoformat().replace("+00:00", "Z")
        with self.database.transaction() as c:
            wl = c.execute("SELECT * FROM hh_waitlists WHERE waitlist_id=?", (waitlist_id,)).fetchone()
            if not wl:
                raise NotFoundError("轮候册不存在")
            room = self._get_room(c, room_id)
            if room["status"] != "available":
                raise ConflictError("房间当前不可配售")
            project = c.execute("SELECT * FROM hh_projects WHERE project_id=?", (room["project_id"],)).fetchone()
            if project["program"] != wl["program"]:
                raise ValidationError("房间保障类型与轮候册不一致")
            head = self._head_waiting(c, waitlist_id)
            if not head:
                raise ConflictError("轮候册中没有等待中的家庭")
            end = (now_dt + timedelta(days=365)).isoformat().replace("+00:00", "Z")
            lease_id = new_id("lease")
            c.execute("""INSERT INTO hh_leases(lease_id,family_id,applicant_id,program,project_id,room_id,start_at,end_at,
                         state,version,application_id,created_by,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,'pending',1,?,?,?,?)""",
                      (lease_id, head["family_id"],
                       c.execute("SELECT applicant_id FROM hh_applications WHERE application_id=?",
                                 (head["application_id"],)).fetchone()["applicant_id"],
                       wl["program"], room["project_id"], room_id, self._now(), end,
                       head["application_id"], context.actor_id, self._now(), self._now()))
            c.execute("UPDATE hh_rooms SET status='reserved',version=version+1,updated_at=? WHERE room_id=?",
                      (self._now(), room_id))
            c.execute("""UPDATE hh_waitlist_entries SET state='offered',offer_lease_id=?,offer_expires_at=?,offered_at=?,updated_at=?
                         WHERE entry_id=?""", (lease_id, expires, self._now(), self._now(), head["entry_id"]))
            c.execute("""INSERT INTO hh_waitlist_events(event_id,waitlist_id,family_id,kind,rank_snapshot,detail,occurred_at,actor)
                         VALUES(?,?,?, 'offered',?,?,?,?)""",
                      (new_id("wlevt"), waitlist_id, head["family_id"], head["rank"], f"房间 {room_id}", self._now(), context.actor_id))
            self._schedule("offer_expiry", head["entry_id"], expires,
                           {"entry_id": head["entry_id"], "waitlist_id": waitlist_id, "lease_id": lease_id, "room_id": room_id}, c=c)
        self._emit("housing.waitlist", waitlist_id, {"event": "offered", "entry_id": head["entry_id"], "room_id": room_id})
        return {"entry_id": head["entry_id"], "rank": head["rank"], "lease_id": lease_id, "offer_expires_at": expires}

    def accept_offer(self, context: AccessContext, entry_id: str) -> dict:
        self._require_staff(context, "write:housing")
        with self.database.transaction() as c:
            entry = c.execute("SELECT * FROM hh_waitlist_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if not entry:
                raise NotFoundError("轮候记录不存在")
            if entry["state"] != "offered":
                raise ConflictError(f"轮候记录状态为 {entry['state']}")
            if parse_instant(entry["offer_expires_at"]) < parse_instant(self._now()):
                raise ConflictError("offer 已过期，按冻结顺位重新等待")
            lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (entry["offer_lease_id"],)).fetchone()
            blocking = self._active_lease_of_family(c, entry["family_id"], exclude=lease["lease_id"])
            if blocking:
                done = c.execute("SELECT 1 FROM hh_handovers WHERE from_lease_id=? AND to_lease_id=? AND state='completed'",
                                 (blocking["lease_id"], lease["lease_id"])).fetchone()
                if not done:
                    raise ConflictError("须先完成原住房交接，才能接受新住房")
            now = self._now()
            c.execute("UPDATE hh_leases SET state='active',start_at=?,version=version+1,updated_at=? WHERE lease_id=?",
                      (now, now, lease["lease_id"]))
            c.execute("UPDATE hh_rooms SET status='occupied',version=version+1,updated_at=? WHERE room_id=?",
                      (now, lease["room_id"]))
            c.execute("UPDATE hh_waitlist_entries SET state='admitted',updated_at=? WHERE entry_id=?", (now, entry_id))
            c.execute("""INSERT INTO hh_waitlist_events(event_id,waitlist_id,family_id,kind,rank_snapshot,occurred_at,actor)
                         VALUES(?,?,?, 'admitted',?,?,?)""",
                      (new_id("wlevt"), entry["waitlist_id"], entry["family_id"], entry["rank"], now, context.actor_id))
            self._schedule("lease_expiry", lease["lease_id"], lease["end_at"],
                           {"lease_id": lease["lease_id"], "room_id": lease["room_id"]}, c=c)
            self._schedule("room_release", lease["lease_id"], lease["end_at"],
                           {"lease_id": lease["lease_id"], "room_id": lease["room_id"], "family_id": entry["family_id"]}, c=c)
            self._emit("housing.waitlist", entry["waitlist_id"], {"event": "admitted", "entry_id": entry_id}, c=c)
        return {"entry_id": entry_id, "state": "admitted", "lease_id": lease["lease_id"]}

    def _expire_offer(self, c, entry) -> None:
        """offer 过期：保留冻结顺位，回到 waiting，释放被锁定的房间。"""
        now = self._now()
        c.execute("UPDATE hh_waitlist_entries SET state='waiting',offer_lease_id=NULL,offer_expires_at=NULL,offered_at=NULL,updated_at=? WHERE entry_id=?",
                  (now, entry["entry_id"]))
        c.execute("""INSERT INTO hh_waitlist_events(event_id,waitlist_id,family_id,kind,rank_snapshot,detail,occurred_at,actor)
                     VALUES(?,?,?, 'offer_expired',?,'未按时应答，保留原顺位',?,'system')""",
                  (new_id("wlevt"), entry["waitlist_id"], entry["family_id"], entry["rank"], now))
        lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (entry["offer_lease_id"],)).fetchone()
        if lease and lease["state"] == "pending":
            c.execute("UPDATE hh_leases SET state='released',version=version+1,updated_at=? WHERE lease_id=?",
                      (now, lease["lease_id"]))
            c.execute("UPDATE hh_rooms SET status='available',version=version+1,updated_at=? WHERE room_id=?",
                      (now, lease["room_id"]))

    def _backfill_room(self, c, room_id: str) -> dict | None:
        """房源释放后，按冻结顺位向匹配轮候册的首位家庭递补。"""
        room = c.execute("SELECT * FROM hh_rooms WHERE room_id=?", (room_id,)).fetchone()
        if not room or room["status"] != "available":
            return None
        project = c.execute("SELECT * FROM hh_projects WHERE project_id=?", (room["project_id"],)).fetchone()
        for wl in self._waitlists_for(c, project["program"], room["project_id"]):
            head = self._head_waiting(c, wl["waitlist_id"])
            if not head:
                continue
            now = self._now()
            expires = (parse_instant(now) + timedelta(hours=48)).isoformat().replace("+00:00", "Z")
            app = c.execute("SELECT * FROM hh_applications WHERE application_id=?", (head["application_id"],)).fetchone()
            end = (parse_instant(now) + timedelta(days=365)).isoformat().replace("+00:00", "Z")
            lease_id = new_id("lease")
            c.execute("""INSERT INTO hh_leases(lease_id,family_id,applicant_id,program,project_id,room_id,start_at,end_at,
                         state,version,application_id,created_by,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,'pending',1,?,?,?,?)""",
                      (lease_id, head["family_id"], app["applicant_id"], wl["program"], room["project_id"], room_id,
                       now, end, head["application_id"], "system", now, now))
            c.execute("UPDATE hh_rooms SET status='reserved',version=version+1,updated_at=? WHERE room_id=?", (now, room_id))
            c.execute("""UPDATE hh_waitlist_entries SET state='offered',offer_lease_id=?,offer_expires_at=?,offered_at=?,updated_at=?
                         WHERE entry_id=?""", (lease_id, expires, now, now, head["entry_id"]))
            c.execute("""INSERT INTO hh_waitlist_events(event_id,waitlist_id,family_id,kind,rank_snapshot,detail,occurred_at,actor)
                         VALUES(?,?,?, 'offered',?,'房源释放自动递补',?,'system')""",
                      (new_id("wlevt"), wl["waitlist_id"], head["family_id"], head["rank"], now))
            self._schedule("offer_expiry", head["entry_id"], expires,
                           {"entry_id": head["entry_id"], "waitlist_id": wl["waitlist_id"],
                            "lease_id": lease_id, "room_id": room_id}, c=c)
            return {"entry_id": head["entry_id"], "rank": head["rank"], "lease_id": lease_id, "room_id": room_id}
        return None

    def waitlist_view(self, context: AccessContext, waitlist_id: str) -> dict:
        context.require("housing:staff")
        with self.database.connect() as c:
            wl = c.execute("SELECT * FROM hh_waitlists WHERE waitlist_id=?", (waitlist_id,)).fetchone()
            if not wl:
                raise NotFoundError("轮候册不存在")
            result = self._row(wl)
            result["ranking_rule"] = json.loads(wl["ranking_rule_json"])
            result["entries"] = [self._row(r) for r in c.execute(
                "SELECT * FROM hh_waitlist_entries WHERE waitlist_id=? ORDER BY rank", (waitlist_id,))]
            result["events"] = [self._row(r) for r in c.execute(
                "SELECT * FROM hh_waitlist_events WHERE waitlist_id=? ORDER BY occurred_at,event_id", (waitlist_id,))]
            return result

    # ---------- 崩溃恢复：处理到期任务 ----------

    def process_due_jobs(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        """系统恢复后继续：即将到期租约、缺件提醒、offer 过期、房源释放。"""
        self._require_staff(context, "housing:staff")
        claimed = self.jobs.claim_due(limit=limit)
        outcomes: list[dict] = []
        for job in claimed:
            payload = json.loads(job["payload_json"])
            try:
                outcome = self._run_job(job["job_type"], payload)
                self.jobs.finish(job["job_id"])
                outcomes.append({"job_id": job["job_id"], "job_type": job["job_type"], "outcome": outcome})
            except Exception as exc:  # 任务保留可重试，不影响其他任务
                retry_at = (parse_instant(self._now()) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
                self.jobs.retry(job["job_id"], error=f"{type(exc).__name__}: {exc}", retry_at=retry_at)
                outcomes.append({"job_id": job["job_id"], "job_type": job["job_type"], "error": str(exc)})
        return outcomes

    def _run_job(self, job_type: str, payload: dict) -> dict:
        if job_type == "missing_docs":
            self._emit("housing.application", payload["application_id"],
                       {"event": "missing_docs_reminder", "missing": payload.get("missing", [])})
            return {"reminded": payload["application_id"]}
        with self.database.transaction() as c:
            if job_type == "lease_expiry":
                lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (payload["lease_id"],)).fetchone()
                if lease and lease["state"] in ("active", "expiring"):
                    c.execute("UPDATE hh_leases SET state='expiring',version=version+1,updated_at=? WHERE lease_id=?",
                              (self._now(), lease["lease_id"]))
                    self._emit("housing.lease", lease["lease_id"], {"event": "expiring", "room_id": lease["room_id"]}, c=c)
                    return {"lease_id": lease["lease_id"], "state": "expiring"}
                return {"noop": True}
            if job_type == "room_release":
                lease = c.execute("SELECT * FROM hh_leases WHERE lease_id=?", (payload["lease_id"],)).fetchone()
                if not lease or lease["state"] == "released":
                    return {"noop": True}
                handover = c.execute("SELECT * FROM hh_handovers WHERE from_lease_id=? AND state='completed'",
                                     (payload["lease_id"],)).fetchone()
                if not handover:
                    # 交接尚未完成：抛出以便任务稍后重试，房源在交接完成前不释放。
                    raise ConflictError("交接未完成，房源暂不释放")
                room = c.execute("SELECT * FROM hh_rooms WHERE room_id=?", (payload["room_id"],)).fetchone()
                if room["status"] != "available":
                    c.execute("UPDATE hh_rooms SET status='available',version=version+1,updated_at=? WHERE room_id=?",
                              (self._now(), payload["room_id"]))
                c.execute("UPDATE hh_leases SET state='released',version=version+1,updated_at=? WHERE lease_id=?",
                          (self._now(), payload["lease_id"]))
                offered = self._backfill_room(c, payload["room_id"])
                return {"released": payload["room_id"], "backfill": offered}
            if job_type == "offer_expiry":
                entry = c.execute("SELECT * FROM hh_waitlist_entries WHERE entry_id=?", (payload["entry_id"],)).fetchone()
                if entry and entry["state"] == "offered" and parse_instant(entry["offer_expires_at"]) <= parse_instant(self._now()):
                    self._expire_offer(c, entry)
                    return {"entry_id": entry["entry_id"], "expired": True, "rank_kept": entry["rank"]}
                return {"noop": True}
        raise ValidationError(f"未知任务类型 {job_type}")

    # ---------- 家庭时间线与解释 ----------

    def family_timeline(self, context: AccessContext, family_id: str) -> list[dict]:
        self._can_read_family(context, family_id)
        events: list[dict] = []
        with self.database.connect() as c:
            for r in c.execute("SELECT * FROM hh_members WHERE family_id=? ORDER BY valid_from", (family_id,)):
                events.append({"at": r["valid_from"], "kind": "member",
                               "title": f"家庭成员 {r['person_id']}（{r['role']}{'，需照料' if r['needs_care'] else ''}）生效",
                               "detail": {"member_id": r["member_id"], "valid_to": r["valid_to"]}})
            for r in c.execute("SELECT * FROM hh_documents WHERE family_id=? ORDER BY recorded_at", (family_id,)):
                events.append({"at": r["recorded_at"], "kind": "document",
                               "title": f"证明 {r['doc_type']} 提交（{r['state']}）",
                               "detail": {"document_id": r["document_id"], "doc_type": r["doc_type"]}})
            for r in c.execute("SELECT * FROM hh_applications WHERE family_id=? ORDER BY recorded_at", (family_id,)):
                events.append({"at": r["recorded_at"], "kind": "application",
                               "title": f"申请{policies.PROGRAM_LABELS[r['program']]}（{r['state']}）",
                               "detail": {"application_id": r["application_id"], "program": r["program"],
                                          "carry_wait_days": r["wait_time_before_days"]}})
            for r in c.execute("SELECT * FROM hh_eligibility_decisions WHERE family_id=? ORDER BY effective_from", (family_id,)):
                title = "获得" if r["outcome"] == "granted" else "未获"
                events.append({"at": r["effective_from"], "kind": "eligibility",
                               "title": f"{title}{policies.PROGRAM_LABELS[r['program']]}资格：{r['reason']}",
                               "detail": {"decision_id": r["decision_id"], "program": r["program"],
                                          "outcome": r["outcome"], "policy_version": r["policy_version"],
                                          "carry_wait_days": r["carry_wait_days"]}})
            for r in c.execute("SELECT * FROM hh_leases WHERE family_id=? ORDER BY start_at", (family_id,)):
                events.append({"at": r["start_at"], "kind": "lease",
                               "title": f"{policies.PROGRAM_LABELS[r['program']]}租约 {r['state']}（房间 {r['room_id']}）",
                               "detail": {"lease_id": r["lease_id"], "state": r["state"], "start_at": r["start_at"],
                                          "end_at": r["end_at"], "room_id": r["room_id"]}})
            for r in c.execute("SELECT * FROM hh_rent_records r JOIN hh_leases l ON l.lease_id=r.lease_id WHERE l.family_id=? AND r.paid_minor>0 ORDER BY r.paid_at",
                               (family_id,)):
                events.append({"at": r["paid_at"], "kind": "payment",
                               "title": f"缴纳租金 {r['paid_minor']} 分（{policies.PROGRAM_LABELS[r['program']]}）",
                               "detail": {"rent_id": r["rent_id"], "paid_minor": r["paid_minor"]}})
            for r in c.execute("SELECT * FROM hh_reductions WHERE family_id=? ORDER BY effective_from", (family_id,)):
                events.append({"at": r["effective_from"], "kind": "reduction",
                               "title": f"租金减免 {r['percent']}%（{r['state']}）",
                               "detail": {"reduction_id": r["reduction_id"], "percent": r["percent"]}})
            for r in c.execute("SELECT * FROM hh_handovers WHERE family_id=? ORDER BY recorded_at", (family_id,)):
                events.append({"at": r["completed_at"] or r["scheduled_at"], "kind": "handover",
                               "title": f"{'换房' if r['kind'] == 'transfer' else '退租'}交接（{r['state']}）",
                               "detail": {"handover_id": r["handover_id"], "from_lease_id": r["from_lease_id"],
                                          "to_lease_id": r["to_lease_id"]}})
            for r in c.execute("SELECT * FROM hh_waitlist_events WHERE family_id=? ORDER BY occurred_at", (family_id,)):
                labels = {"enqueued": "进入轮候", "offered": "获得配售通知", "offer_expired": "通知过期保留顺位",
                          "admitted": "递补入住", "skipped": "跳过"}
                events.append({"at": r["occurred_at"], "kind": "waitlist",
                               "title": f"轮候：{labels.get(r['kind'], r['kind'])}（冻结顺位 {r['rank_snapshot']}）",
                               "detail": {"event_id": r["event_id"], "kind": r["kind"], "waitlist_id": r["waitlist_id"]}})
        events.sort(key=lambda e: (e["at"], e["kind"]))
        return events

    def explain_support(self, context: AccessContext, family_id: str) -> dict:
        """向审核人员解释一个家庭为何在不同阶段获得、失去或转换住房支持。"""
        self._can_read_family(context, family_id)
        with self.database.connect() as c:
            family = c.execute("SELECT * FROM hh_families WHERE family_id=?", (family_id,)).fetchone()
            if not family:
                raise NotFoundError("家庭不存在")
            stages = []
            decisions = c.execute("SELECT * FROM hh_eligibility_decisions WHERE family_id=? ORDER BY effective_from",
                                  (family_id,)).fetchall()
            for d in decisions:
                lease = c.execute("SELECT * FROM hh_leases WHERE application_id=? ORDER BY start_at LIMIT 1",
                                  (d["application_id"],)).fetchone()
                paid = 0
                if lease:
                    paid = int(c.execute("SELECT COALESCE(SUM(paid_minor),0) AS n FROM hh_rent_records WHERE lease_id=?",
                                         (lease["lease_id"],)).fetchone()["n"])
                factors = json.loads(d["factors_json"])
                stages.append({
                    "program": d["program"],
                    "program_label": policies.PROGRAM_LABELS[d["program"]],
                    "outcome": d["outcome"],
                    "effective_from": d["effective_from"],
                    "policy_version": d["policy_version"],
                    "reason": d["reason"],
                    "factors": factors,
                    "carried_wait_days": d["carry_wait_days"],
                    "lease": ({"lease_id": lease["lease_id"], "room_id": lease["room_id"],
                               "start_at": lease["start_at"], "end_at": lease["end_at"], "state": lease["state"]}
                              if lease else None),
                    "rent_paid_minor": paid,
                })
            current = self._active_lease_of_family(c, family_id)
            total_paid = int(c.execute("""SELECT COALESCE(SUM(r.paid_minor),0) AS n FROM hh_rent_records r
                                          JOIN hh_leases l ON l.lease_id=r.lease_id WHERE l.family_id=?""",
                                       (family_id,)).fetchone()["n"])
            waits = c.execute("""SELECT wl.program, e.rank, e.state, e.base_since FROM hh_waitlist_entries e
                                 JOIN hh_waitlists wl ON wl.waitlist_id=e.waitlist_id WHERE e.family_id=? ORDER BY e.rank""",
                              (family_id,)).fetchall()
        narrative = []
        previous = None
        for s in stages:
            if s["outcome"] == "granted":
                if previous is None:
                    narrative.append(f"{s['effective_from']} 首次获得{s['program_label']}：{s['reason']}。")
                else:
                    narrative.append(
                        f"{s['effective_from']} 由{policies.PROGRAM_LABELS[previous]}转换为{s['program_label']}：{s['reason']}；"
                        f"携带既往轮候 {s['carried_wait_days']} 天，已履行租期与缴费事实保留（本阶段实缴 {s['rent_paid_minor']} 分）。")
                previous = s["program"]
            else:
                narrative.append(f"{s['effective_from']} 未获{s['program_label']}：{s['reason']}（不影响此前已享受的支持）。")
        return {
            "family_id": family_id,
            "family_name": family["name"],
            "stages": stages,
            "current_lease": ({"lease_id": current["lease_id"], "program": current["program"],
                               "room_id": current["room_id"], "end_at": current["end_at"]} if current else None),
            "total_rent_paid_minor": total_paid,
            "waitlist_positions": [dict(w) for w in waits],
            "narrative": narrative,
            "timeline_url_hint": "family_timeline",
        }
