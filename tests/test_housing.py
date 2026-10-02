from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


def staff(actor: str) -> AccessContext:
    return AccessContext(actor_id=actor, permissions=frozenset({"*"}),
                         scopes=frozenset({"*"}), reveal_sensitive=True)


def applicant(actor: str, family_id: str) -> AccessContext:
    return AccessContext(actor_id=actor, permissions=frozenset({"read:housing"}),
                         scopes=frozenset({f"family:{family_id}"}))


DOCS = {
    "id_card": {"number": "ID-001", "name": "林晓"},
    "job_seeker": {"status": "unemployed", "registered_at": "2026-09-25"},
    "employment": {"employer": "星河科技", "since": "2026-11-10"},
    "employment_status": {"status": "employed", "employer": "星河科技"},
    "social_security": {"months": 9, "city": "本市"},
    "income_proof": {"monthly_total_minor": 800000},
    "housing_difficulty_proof": {"kind": "无房", "issued_by": "住建窗口"},
}


class HousingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "housing.sqlite3"
        self.app = CivicFlow.open(self.db, fixed_now="2026-10-01T09:00:00+08:00")
        self.op1 = staff("op1")  # 经办人
        self.op2 = staff("op2")  # 审批人

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now: str) -> CivicFlow:
        self.app = CivicFlow.open(self.db, fixed_now=now)
        return self.app

    @property
    def h(self):
        return self.app.housing

    def submit_docs(self, family_id: str, person: str, program: str, op: AccessContext,
                    tag: str = "") -> list[str]:
        from civicflow.housing_policy import default_policy
        ids = []
        for i, doc_type in enumerate(default_policy(program)["required_docs"]):
            result = self.h.submit_document(
                op, family_id, person_id=person, doc_type=doc_type,
                dedup_key=f"{doc_type}:{person}{tag}", payload=DOCS[doc_type],
                request_key=f"doc-{family_id}-{doc_type}-{i}")
            if result.get("acceptance") == "accepted":
                self.h.verify_document(op, result["document_id"],
                                       request_key=f"verify-{doc_type}")
                ids.append(result["document_id"])
        return ids

    def prepare_program(self, program: str, name: str, rents: list[int]):
        project = self.h.register_project(
            self.op1, program=program, name=name, request_key=f"proj-{program}")
        rooms = []
        for i, rent in enumerate(rents):
            rooms.append(self.h.add_room(
                self.op1, project["project_id"], label=f"{program[0].upper()}{i+1}",
                monthly_rent=rent, request_key=f"room-{program}-{i}"))
        return project, rooms

    def approve(self, family_id: str, person: str, program: str, project_id: str,
                facts: dict, effective: str, recorded: AccessContext, reviewer: AccessContext,
                key: str) -> dict:
        app = self.h.apply(recorded, family_id, applicant_id=person, program=program,
                           project_id=project_id, request_key=f"app-{key}")
        return self.h.decide_application(
            reviewer, app["application_id"], decision="approved", facts=facts,
            note=f"准予{program}", effective_from=effective, request_key=f"dec-{key}")

    # ------------------------------------------------------------ 全链路

    def test_youth_journey_station_to_baozu_to_gongzu(self):
        # 家庭：青年林晓与需照顾的母亲
        family = self.h.create_family(
            self.op1, applicant_id="person:lin", label="林晓家庭", request_key="fam-a")
        family_id = family["family_id"]
        self.h.add_member(self.op1, family_id, person_id="person:ma", role="elder",
                          care_need="失能照护", request_key="rel-ma")

        station, (sroom,) = self.prepare_program("station", "筑梦驿站东站", [50000])
        baozu, (broom,) = self.prepare_program("affordable_rental", "青年保租房社区", [120000])
        gongzu, (groom1, groom2) = self.prepare_program("public_rental", "馨和公租房小区", [80000, 80000])

        # 阶段一：求职者入住驿站（2026-10-01）
        self.submit_docs(family_id, "person:lin", "station", self.op1)
        self.approve(family_id, "person:lin", "station", station["project_id"],
                     {"social_security_months": 0,
                      "monthly_income_per_capita_minor": 0,
                      "housing_difficulty": True},
                     "2026-10-01T12:00:00+08:00", self.op1, self.op2, "station")
        lease_s = self.h.check_in(
            self.op1, family_id, person_id="person:lin", room_id=sroom["room_id"],
            start_at="2026-10-01T12:00:00+08:00", request_key="lease-s")
        self.assertEqual(lease_s["state"], "active")
        self.assertEqual(self.h.list_rooms(self.op1, state="occupied")[0]["room_id"],
                         sroom["room_id"])

        # 已缴租金是事实：逐月入账，重复缴费被拒绝
        for period in ("2026-10", "2026-11", "2026-12"):
            self.h.record_payment(self.op1, lease_s["lease_id"], period=period, amount="500.00",
                                  request_key=f"pay-s-{period}")
        with self.assertRaises(ConflictError):
            self.h.record_payment(self.op1, lease_s["lease_id"], period="2026-10", amount="500.00",
                                  request_key="pay-dup")

        # 同一证明重复提交：幂等返回，不新增材料，也不产生第二次资格
        again = self.h.submit_document(
            self.op1, family_id, person_id="person:lin", doc_type="id_card",
            dedup_key="id_card:person:lin", payload=DOCS["id_card"], request_key="dup-id")
        self.assertEqual(again["acceptance"], "duplicate")
        self.assertEqual(len(self.h.list_documents(self.op1, family_id)), 3)

        # 阶段二：11 月中找到工作，申请保租房；驿站资格在新生效日截止
        self.reopen("2026-11-15T09:00:00+08:00")
        self.submit_docs(family_id, "person:lin", "affordable_rental", self.op1)
        self.approve(family_id, "person:lin", "affordable_rental", baozu["project_id"],
                     {"social_security_months": 2,
                      "monthly_income_per_capita_minor": 900000,
                      "housing_difficulty": True},
                     "2026-11-15T12:00:00+08:00", self.op1, self.op2, "baozu")
        elligibility = self.h.list_eligibility(self.applicant_view(family_id), family_id)
        station_elig = next(e for e in elligibility if e["program"] == "station")
        self.assertEqual(station_elig["state"], "superseded")
        self.assertEqual(station_elig["effective_to"], "2026-11-15T04:00:00Z")

        # 资格变化只影响后续安排：驿站租约仍在履行、房间仍占用、缴费保留，
        # 且家庭不能在未交接前重复占用第二处房源
        live_lease = self.h.get_lease(self.op1, lease_s["lease_id"])
        self.assertEqual(live_lease["state"], "active")
        with self.assertRaises(ConflictError):
            self.h.check_in(self.op1, family_id, person_id="person:lin",
                            room_id=broom["room_id"],
                            start_at="2026-11-20T12:00:00+08:00", request_key="double")

        # 跨保障类型不能直接换房，须先退出再按新资格入住
        with self.assertRaises(ValidationError):
            self.h.begin_handover(
                self.op1, lease_s["lease_id"], kind="transfer",
                target_room_id=groom1["room_id"], checklist=["验房"], request_key="xfer-cross")

        # 驿站退出交接：清单未完成、费用未结清不放房
        handover = self.h.begin_handover(
            self.op1, lease_s["lease_id"], kind="exit",
            checklist=["费用结清", "钥匙归还", "房屋验收"], request_key="handover-s")
        self.assertEqual(self.h.get_room_state(sroom["room_id"]), "occupied")
        with self.assertRaises(ConflictError):
            self.h.complete_handover(self.op1, handover["handover_id"], request_key="complete-s-1")
        self.h.complete_handover_item(self.op1, handover["handover_id"], "费用结清",
                                      request_key="item-1")
        self.h.complete_handover_item(self.op1, handover["handover_id"], "钥匙归还",
                                      request_key="item-2")
        with self.assertRaises(ConflictError):
            self.h.complete_handover(self.op1, handover["handover_id"], request_key="complete-s-2")
        self.h.complete_handover_item(self.op1, handover["handover_id"], "房屋验收",
                                      request_key="item-3")
        completed = self.h.complete_handover(self.op1, handover["handover_id"],
                                             request_key="complete-s-3")
        self.assertEqual(self.h.get_room_state(sroom["room_id"]), "available")
        self.assertEqual(completed["state"], "completed")

        # 交接完成后按保租房资格入住；驿站三个月租约事实原样保留
        lease_b = self.h.check_in(
            self.op1, family_id, person_id="person:lin", room_id=broom["room_id"],
            start_at="2026-12-20T12:00:00+08:00", request_key="lease-b")
        self.assertTrue(lease_b["end_at"].startswith("2028"))
        station_payments = [p for p in self.h.list_payments(self.op1, family_id)
                            if p["lease_id"] == lease_s["lease_id"]]
        self.assertEqual(len(station_payments), 3)

        # 阶段三：2027-07 家庭收入下降，符合公租房；先按冻结轮候排序
        self.reopen("2027-07-15T09:00:00+08:00")
        family_b = self.h.create_family(
            self.op1, applicant_id="person:zhao", label="赵刚家庭", request_key="fam-b")
        b_id = family_b["family_id"]
        self.submit_docs(b_id, "person:zhao", "public_rental", self.op1, tag="-b")
        self.approve(b_id, "person:zhao", "public_rental", gongzu["project_id"],
                     {"social_security_months": 12,
                      "monthly_income_per_capita_minor": 300000,
                      "housing_difficulty": True},
                     "2027-07-01T00:00:00+08:00", self.op1, self.op2, "gongzu-b")
        self.submit_docs(family_id, "person:lin", "public_rental", self.op1)
        self.approve(family_id, "person:lin", "public_rental", gongzu["project_id"],
                     {"social_security_months": 9,
                      "monthly_income_per_capita_minor": 400000,
                      "housing_difficulty": True},
                     "2027-07-15T00:00:00+08:00", self.op1, self.op2, "gongzu-a")

        waitlist = self.h.freeze_waitlist(
            self.op1, gongzu["project_id"],
            entries=[{"family_id": b_id, "joined_at": "2027-01-01T00:00:00+08:00"},
                     {"family_id": family_id, "joined_at": "2027-07-01T00:00:00+08:00"}],
            request_key="wl-1")
        ranking = {r["family_id"]: r for r in waitlist["ranking"]}
        # 赵家入列更早，但林家累计保障天数+照顾加分优先级更高，冻结名次第一
        self.assertEqual(ranking[family_id]["rank"], 1)
        self.assertEqual(ranking[family_id]["care_bonus"], 30)
        self.assertGreater(ranking[family_id]["carry_days"], 200)
        self.assertEqual(ranking[b_id]["rank"], 2)

        # 递补前须先交接保租房；同类型内未完成交接不得退出后立即占公房的规则由家庭唯一索引保证
        for period in ("2026-12", "2027-01", "2027-02", "2027-03", "2027-04",
                       "2027-05", "2027-06", "2027-07"):
            self.h.record_payment(self.op1, lease_b["lease_id"], period=period, amount="1200.00",
                                  request_key=f"pay-b-{period}")
        handover_b = self.h.begin_handover(
            self.op1, lease_b["lease_id"], kind="exit",
            checklist=["费用结清", "房屋验收"], request_key="handover-b")
        for item in ("费用结清", "房屋验收"):
            self.h.complete_handover_item(self.op1, handover_b["handover_id"], item,
                                          request_key=f"b-item-{item}")
        self.h.complete_handover(self.op1, handover_b["handover_id"], request_key="complete-b")
        self.assertEqual(self.h.get_room_state(broom["room_id"]), "available")

        # 严格按冻结名次递补：林家居首，房间转为预留
        promo = self.h.promote(self.op1, waitlist["waitlist_id"], room_id=groom1["room_id"],
                               request_key="promo-a")
        self.assertEqual(promo["family_id"], family_id)
        self.assertEqual(promo["rank"], 1)
        self.assertEqual(self.h.get_room_state(groom1["room_id"]), "reserved")
        lease_g = self.h.activate_lease(self.op1, promo["lease_id"], request_key="activate-a")
        self.assertEqual(lease_g["state"], "active")
        self.assertEqual(self.h.get_room_state(groom1["room_id"]), "occupied")

        # 赵家下一名递补后放弃：预备租约撤销、房间释放，名次记录留痕
        promo_b = self.h.promote(self.op1, waitlist["waitlist_id"], room_id=groom2["room_id"],
                                 request_key="promo-b")
        self.assertEqual(promo_b["rank"], 2)
        forfeited = self.h.forfeit_placement(
            self.op1, waitlist["waitlist_id"], b_id, reason="异地就业", request_key="forfeit-b")
        self.assertEqual(forfeited["state"], "forfeited")
        self.assertEqual(self.h.get_room_state(groom2["room_id"]), "available")

        # 租金减免是例外：经办人不能自批；批准后只影响后续账期
        adjustment = self.h.request_rent_adjustment(
            self.op1, lease_g["lease_id"], reason="家庭收入进一步下降",
            reduction_minor=30000, effective_from_period="2027-08", request_key="adj-1")
        with self.assertRaises(PermissionDenied):
            self.h.decide_exception(self.op1, self.exception_id_for(adjustment["adjustment_id"]),
                                    decision="approved", request_key="self-approve")
        self.h.decide_exception(self.op2, self.exception_id_for(adjustment["adjustment_id"]),
                                decision="approved", note="同意减租", request_key="adj-ok")
        self.reopen("2027-07-20T09:00:00+08:00")
        position_jul = self.h.rent_position(self.op1, lease_g["lease_id"])
        self.assertEqual(position_jul["due_minor"], 80000)  # 7 月账期不受 8 月起减免影响
        self.reopen("2027-08-20T09:00:00+08:00")
        position_aug = self.h.rent_position(self.op1, lease_g["lease_id"])
        self.assertEqual(position_aug["due_minor"], 80000 + 50000)

        # 政策升级不改写历史认定
        revised = self.h.revise_project_policy(
            self.op1, gongzu["project_id"], override={"version": "gongzu-2027.2",
                                                      "max_income_per_capita_minor": 400000},
            reason="年度调标", request_key="policy-2027")
        gongzu_elig = next(e for e in self.h.list_eligibility(self.op1, family_id)
                           if e["program"] == "public_rental")
        self.assertNotEqual(gongzu_elig["policy_digest"], revised["policy_digest"])

        # 解释轨迹：三个阶段的获得、转换与实缴都可追溯
        explanation = self.h.explain_support(self.op1, family_id)
        programs = [s["program"] for s in explanation["stages"]]
        self.assertEqual(programs, ["station", "affordable_rental", "public_rental"])
        station_stage = explanation["stages"][0]
        self.assertEqual(station_stage["rent_paid_minor"], 150000)
        self.assertTrue(any("轮候" in line for line in explanation["narrative"]))
        self.assertTrue(any("实缴租金 15000" in line for line in explanation["narrative"]))
        self.assertGreaterEqual(len(explanation["events"]), 15)

    def exception_id_for(self, adjustment_id: str) -> str:
        rows = self.app.database
        with rows.connect() as conn:
            row = conn.execute(
                "SELECT exception_id FROM hs_exceptions WHERE subject_id=?",
                (adjustment_id,)).fetchone()
        return row["exception_id"]

    def get_room_state(self, room_id: str) -> str:
        with self.app.database.connect() as conn:
            return conn.execute("SELECT state FROM hs_rooms WHERE room_id=?",
                                (room_id,)).fetchone()["state"]

    def applicant_view(self, family_id: str) -> AccessContext:
        return applicant("person:lin", family_id)

    # ------------------------------------------------------------ 证明冲突

    def test_conflicting_document_pauses_applications(self):
        family = self.h.create_family(self.op1, applicant_id="person:he",
                                      label="何岚家庭", request_key="fam-e")
        family_id = family["family_id"]
        project, _ = self.prepare_program("station", "冲突测试驿站", [10000])
        self.submit_docs(family_id, "person:he", "station", self.op1, tag="-e")
        app = self.h.apply(self.op1, family_id, applicant_id="person:he", program="station",
                           project_id=project["project_id"], request_key="app-e")
        self.assertEqual(app["state"], "ready")

        # 同一去重键提交内容不同的身份证 → 挂起冲突并暂停申请
        conflict = self.h.submit_document(
            self.op1, family_id, person_id="person:he", doc_type="id_card",
            dedup_key="id_card:person:he-e", payload={"number": "ID-999", "name": "何岚"},
            request_key="conflict-doc")
        self.assertEqual(conflict["acceptance"], "conflict")
        paused = self.h.get_application(self.op1, app["application_id"])
        self.assertEqual(paused["state"], "paused")
        with self.assertRaises(ConflictError):
            self.h.decide_application(
                self.op2, app["application_id"], decision="approved",
                facts={"housing_difficulty": True}, request_key="decide-paused")

        # 裁定保留原件后申请恢复；审批可继续
        self.h.resolve_document_conflict(
            self.op2, conflict["conflict_id"], resolution="keep_existing",
            note="原件有效", request_key="resolve-keep")
        resumed = self.h.get_application(self.op1, app["application_id"])
        self.assertEqual(resumed["state"], "ready")
        decided = self.h.decide_application(
            self.op2, app["application_id"], decision="approved",
            facts={"housing_difficulty": True}, request_key="decide-e")
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(self.h.list_conflicts(self.op1, family_id)[0]["state"], "resolved")

    # ------------------------------------------------------------ 权限隔离

    def test_applicant_sees_only_own_family(self):
        family = self.h.create_family(self.op1, applicant_id="person:luo",
                                      label="罗芸家庭", request_key="fam-x")
        other = self.h.create_family(self.op1, applicant_id="person:gao",
                                     label="高翔家庭", request_key="fam-y")
        mine = applicant("person:luo", family["family_id"])
        self.assertTrue(self.h.get_family(mine, family["family_id"])["family_id"])
        self.h.family_timeline(mine, family["family_id"])
        with self.assertRaises(PermissionDenied):
            self.h.get_family(mine, other["family_id"])
        with self.assertRaises(PermissionDenied):
            self.h.list_families(mine)
        with self.assertRaises(PermissionDenied):
            self.h.submit_document(
                mine, family["family_id"], person_id="person:luo", doc_type="id_card",
                dedup_key="x", payload={"n": 1}, request_key="applicant-write")

    # ------------------------------------------------------------ 断点恢复

    def test_recovery_resumes_expiry_nudge_and_release(self):
        project, (room,) = self.prepare_program("station", "恢复测试驿站", [30000])

        # 家庭 C：90 天后到期的驿站租约
        fc = self.h.create_family(self.op1, applicant_id="person:c",
                                  label="陈晨家庭", request_key="fam-c")
        self.submit_docs(fc["family_id"], "person:c", "station", self.op1, tag="-c")
        self.approve(fc["family_id"], "person:c", "station", project["project_id"],
                     {"housing_difficulty": True},
                     "2026-10-01T12:00:00+08:00", self.op1, self.op2, "station-c")
        lease_c = self.h.check_in(
            self.op1, fc["family_id"], person_id="person:c", room_id=room["room_id"],
            start_at="2026-10-01T12:00:00+08:00", request_key="lease-c")

        # 家庭 D：缺件申请，应当有催办任务
        fd = self.h.create_family(self.op1, applicant_id="person:d",
                                  label="邓佳家庭", request_key="fam-d")
        missing_app = self.h.apply(self.op1, fd["family_id"], applicant_id="person:d",
                                   program="station", project_id=project["project_id"],
                                   request_key="app-d")
        self.assertEqual(missing_app["state"], "received")

        # 恢复后任务补齐是幂等的
        self.reopen("2026-12-31T09:00:00+08:00")
        first = self.h.ensure_recovery_jobs(self.op1)
        second = self.h.ensure_recovery_jobs(self.op1)
        self.assertEqual(len(first["scheduled"]), len(second["scheduled"]))

        results = self.h.run_recovery(self.op1)
        kinds = {r["job_type"] for r in results}
        self.assertIn("housing.lease_expiry", kinds)
        self.assertIn("housing.doc_nudge", kinds)
        expiry = next(r for r in results if r["job_type"] == "housing.lease_expiry")
        self.assertEqual(expiry["outcome"], "handover_begun")

        # 到期后自动发起交接，但欠费与清单未完成时房间不释放，任务重试
        self.reopen("2027-01-04T09:00:00+08:00")
        self.h.ensure_recovery_jobs(self.op1)
        retry = self.h.run_recovery(self.op1)
        self.assertTrue(any("retry" in r for r in retry))
        self.assertEqual(self.h.get_room_state(room["room_id"]), "occupied")

        # 补缴三个月费用并完成清单后，恢复任务释放房源
        handovers = self.h.list_handovers(self.op1, fc["family_id"])
        handover_id = handovers[0]["handover_id"]
        for period in ("2026-10", "2026-11", "2026-12"):
            self.h.record_payment(self.op1, lease_c["lease_id"], period=period, amount="300.00",
                                  paid_at="2027-01-04T10:00:00+08:00", request_key=f"pay-c-{period}")
        for item in ("费用结清", "房屋验收"):
            self.h.complete_handover_item(self.op1, handover_id, item,
                                          request_key=f"c-item-{item}")
        done = self.h.run_recovery(self.op1)
        self.assertTrue(any(r.get("outcome") == "room_released" for r in done))
        self.assertEqual(self.h.get_room_state(room["room_id"]), "available")
        self.assertEqual(self.h.get_lease(self.op1, lease_c["lease_id"])["state"], "handed_over")

        # 缺件催办事件也已留痕
        events = self.h.family_timeline(self.op1, fd["family_id"])
        self.assertTrue(any(e["event_type"] == "application.doc_nudge" for e in events))

    # ------------------------------------------------------------ 资格到期与边界

    def test_reapply_after_eligibility_expired_and_no_payment_on_cancelled_lease(self):
        project, (room,) = self.prepare_program("station", "到期重申驿站", [10000])
        family = self.h.create_family(self.op1, applicant_id="person:wan",
                                      label="万青家庭", request_key="fam-w")
        fid = family["family_id"]
        self.submit_docs(fid, "person:wan", "station", self.op1, tag="-w")
        decided = self.approve(fid, "person:wan", "station", project["project_id"],
                               {"housing_difficulty": True},
                               "2026-10-01T12:00:00+08:00", self.op1, self.op2, "w1")
        eligibility_id = decided["eligibility_id"]

        # 资格有效期内重复申请被拒绝
        self.reopen("2026-11-01T09:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.h.apply(self.op1, fid, applicant_id="person:wan", program="station",
                         project_id=project["project_id"], request_key="app-dup")

        # 到期后可以重新申请并作出新的认定，旧记录保留
        self.reopen("2027-01-15T09:00:00+08:00")
        app2 = self.h.apply(self.op1, fid, applicant_id="person:wan", program="station",
                            project_id=project["project_id"], request_key="app-w2")
        decided2 = self.h.decide_application(
            self.op2, app2["application_id"], decision="approved",
            facts={"housing_difficulty": True},
            effective_from="2027-01-15T12:00:00+08:00", request_key="dec-w2")
        self.assertNotEqual(decided2["eligibility_id"], eligibility_id)
        elligibility = self.h.list_eligibility(self.op1, fid)
        old = next(e for e in elligibility if e["eligibility_id"] == eligibility_id)
        self.assertEqual(old["state"], "active")  # 自然到期不改写状态，由生效区间表达
        explanation = self.h.explain_support(self.op1, fid)
        self.assertEqual(explanation["stages"][0]["display_state"], "expired")

        # 新资格入住后，欠费月份未缴时交接不能完成；补缴后可以退房
        lease = self.h.check_in(self.op2, fid, person_id="person:wan",
                                room_id=room["room_id"],
                                start_at="2027-01-16T12:00:00+08:00",
                                request_key="lease-w")
        self.reopen("2027-02-20T09:00:00+08:00")
        handover = self.h.begin_handover(self.op2, lease["lease_id"], kind="exit",
                                         checklist=["验房"], request_key="h-w")
        self.h.complete_handover_item(self.op1, handover["handover_id"], "验房",
                                      request_key="w-item")
        with self.assertRaises(ConflictError):
            self.h.complete_handover(self.op1, handover["handover_id"],
                                     request_key="w-complete-bad")
        for period in ("2027-01", "2027-02"):
            self.h.record_payment(self.op1, lease["lease_id"], period=period,
                                  amount="100.00", request_key=f"pay-w-{period}")
        self.h.complete_handover(self.op1, handover["handover_id"],
                                 request_key="w-complete")
        self.assertEqual(self.h.get_room_state(room["room_id"]), "available")

    # ------------------------------------------------------------ 同类型换房

    def test_transfer_room_requires_handover_and_preserves_history(self):
        project = self.h.register_project(
            self.op1, program="affordable_rental", name="换房测试社区",
            request_key="proj-t")
        r1 = self.h.add_room(self.op1, project["project_id"], label="T1",
                             monthly_rent=120000, request_key="room-t1")
        r2 = self.h.add_room(self.op1, project["project_id"], label="T2",
                             monthly_rent=120000, request_key="room-t2")
        family = self.h.create_family(self.op1, applicant_id="person:tang",
                                      label="唐宁家庭", request_key="fam-t")
        fid = family["family_id"]
        self.submit_docs(fid, "person:tang", "affordable_rental", self.op1, tag="-t")
        self.approve(fid, "person:tang", "affordable_rental", project["project_id"],
                     {"social_security_months": 6,
                      "monthly_income_per_capita_minor": 900000,
                      "housing_difficulty": True},
                     "2026-10-01T12:00:00+08:00", self.op1, self.op2, "ar-t")
        lease1 = self.h.check_in(
            self.op1, fid, person_id="person:tang", room_id=r1["room_id"],
            start_at="2026-10-01T12:00:00+08:00", request_key="lease-t1")
        self.reopen("2026-11-20T09:00:00+08:00")
        self.h.record_payment(self.op1, lease1["lease_id"], period="2026-10",
                              amount="1200.00", request_key="pay-t-10")
        self.h.record_payment(self.op1, lease1["lease_id"], period="2026-11",
                              amount="1200.00", request_key="pay-t-11")

        # 换房交接完成前，目标房保留、原房不释放
        handover = self.h.begin_handover(
            self.op1, lease1["lease_id"], kind="transfer",
            target_room_id=r2["room_id"],
            checklist=["费用结清", "房屋验收", "钥匙更换"], request_key="handover-t")
        self.assertEqual(self.h.get_room_state(r1["room_id"]), "occupied")
        with self.assertRaises(ConflictError):
            self.h.complete_handover(self.op1, handover["handover_id"],
                                     request_key="complete-t-bad")
        for item in ("费用结清", "房屋验收", "钥匙更换"):
            self.h.complete_handover_item(self.op1, handover["handover_id"], item,
                                          request_key=f"t-item-{item}")
        result = self.h.complete_handover(self.op1, handover["handover_id"],
                                          request_key="complete-t")
        self.assertEqual(self.h.get_room_state(r1["room_id"]), "available")
        self.assertEqual(self.h.get_room_state(r2["room_id"]), "occupied")

        # 新租约接续旧租约，缴费与履行事实保留在旧租约上
        lease2 = self.h.get_lease(self.op1, result["new_lease_id"])
        self.assertEqual(lease2["state"], "active")
        self.assertEqual(lease2["predecessor_lease_id"], lease1["lease_id"])
        self.assertEqual(self.h.get_lease(self.op1, lease1["lease_id"])["state"], "handed_over")
        old_payments = [p for p in self.h.list_payments(self.op1, fid)
                        if p["lease_id"] == lease1["lease_id"]]
        self.assertEqual(len(old_payments), 2)
        explanation = self.h.explain_support(self.op1, fid)
        stage = next(s for s in explanation["stages"] if s["program"] == "affordable_rental")
        self.assertEqual(stage["rent_paid_minor"], 240000)
        self.assertEqual(len(stage["leases"]), 2)

    # ------------------------------------------------------------ 轮候脱敏

    def test_applicant_waitlist_view_masks_other_families(self):
        project = self.h.register_project(self.op1, program="public_rental",
                                          name="脱敏测试公租房", request_key="proj-m")
        fa = self.h.create_family(self.op1, applicant_id="person:m1",
                                  label="甲家庭", request_key="fam-m1")
        fb = self.h.create_family(self.op1, applicant_id="person:m2",
                                  label="乙家庭", request_key="fam-m2")
        waitlist = self.h.freeze_waitlist(
            self.op1, project["project_id"],
            entries=[{"family_id": fa["family_id"], "joined_at": "2026-10-01T00:00:00+08:00"},
                     {"family_id": fb["family_id"], "joined_at": "2026-10-02T00:00:00+08:00"}],
            request_key="wl-m")
        view = self.h.get_waitlist(applicant("person:m1", fa["family_id"]),
                                   waitlist["waitlist_id"])
        ranks = {r["rank"]: r for r in view["ranking"]}
        own = next(r for r in view["ranking"] if r["family_id"] == fa["family_id"])
        self.assertEqual(own["family_id"], fa["family_id"])
        other = [r for r in view["ranking"] if r["family_id"] == "***"]
        self.assertEqual(len(other), 1)
        self.assertIsNone(other[0]["priority"])

    # ------------------------------------------------------------ 录入与审批分离

    def test_recorder_cannot_approve_own_application(self):
        family = self.h.create_family(self.op1, applicant_id="person:shen",
                                      label="申悦家庭", request_key="fam-s")
        project, _ = self.prepare_program("station", "回避测试驿站", [10000])
        self.submit_docs(family["family_id"], "person:shen", "station", self.op1, tag="-s")
        app = self.h.apply(self.op1, family["family_id"], applicant_id="person:shen",
                           program="station", project_id=project["project_id"],
                           request_key="app-s")
        with self.assertRaises(PermissionDenied):
            self.h.decide_application(
                self.op1, app["application_id"], decision="approved",
                facts={"housing_difficulty": True}, request_key="self-decide")


if __name__ == "__main__":
    unittest.main()
