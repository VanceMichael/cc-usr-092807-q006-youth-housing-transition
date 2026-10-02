from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow import housing_policies as P
from civicflow.housing import HousingService, staff_access, applicant_access
from civicflow.errors import ConflictError, PermissionDenied, ValidationError

T0 = "2026-10-02T09:00:00+08:00"
T1 = "2026-11-02T09:00:00+08:00"
T2 = "2027-02-02T09:00:00+08:00"
T3 = "2027-03-02T09:00:00+08:00"

PS = "2026-10-02T00:00:00+08:00"
PE = "2026-11-02T00:00:00+08:00"


class HousingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "housing.sqlite3"
        self.app = CivicFlow.open(self.db, fixed_now=T0)
        self.h = self.app.housing
        self.c1 = staff_access("clerk:1")   # 录入经办
        self.c2 = staff_access("clerk:2")   # 审批经办
        self.c3 = staff_access("clerk:3")
        self.c4 = staff_access("clerk:4")

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now: str) -> HousingService:
        return CivicFlow.open(self.db, fixed_now=now).housing

    def factors(self, **over):
        base = {"has_employment": False, "housing_difficulty": False, "settled": False,
                "income_per_capita_minor": None, "family_size": 1, "needs_care_members": 0}
        base.update(over)
        return base

    # ---------- 场景一：青年三阶段资格迁移，历史事实保留 ----------

    def test_three_stage_migration_preserves_history(self):
        fam = self.h.create_family(self.c1, "李同学家")["family_id"]
        person = "person:li"
        self.h.add_member(self.c1, fam, person, "申请人", T0, needs_care=True)
        self.h.add_member(self.c1, fam, "person:mom", "母亲", T0, needs_care=True)

        # 阶段 0：驿站
        sp = self.h.create_project(self.c1, P.STATION, "筑梦驿站")["project_id"]
        sr = self.h.add_room(self.c1, sp, "S-101")["room_id"]
        a0 = self.h.apply(self.c1, fam, person, P.STATION, "round-station",
                          self.factors(family_size=2, needs_care_members=2), request_key="li-station")
        self.assertEqual(a0["state"], "pending")
        self.h.decide_eligibility(self.c2, a0["application_id"], "granted", "求职过渡，入住短期驿站")
        l0 = self.h.admit(self.c1, a0["application_id"], sr, T0, T1)
        self.h.activate_lease(self.c1, l0["lease_id"])
        bill0 = self.h.bill_rent(self.c1, l0["lease_id"], PS, PE, 80000)
        self.h.record_payment(self.c1, bill0["rent_id"], 80000, T1)

        # 阶段 1：保租房（重新打开库模拟时间推进，携带轮候天数）
        h = self.reopen(T1)
        h.submit_document(self.c1, fam, person, "employment",
                          {"period": "2026-11", "employer": "某公司"}, T1)
        h.submit_document(self.c1, fam, person, "social_security",
                          {"period": "2026-11", "months": 12}, T1)
        ap = h.create_project(self.c1, P.AFFORDABLE_RENTAL, "青年保租房")["project_id"]
        ar = h.add_room(self.c1, ap, "A-202")["room_id"]
        a1 = h.apply(self.c1, fam, person, P.AFFORDABLE_RENTAL, "round-aff",
                     self.factors(has_employment=True, income_per_capita_minor=900000,
                                  family_size=2, needs_care_members=2), request_key="li-aff")
        self.assertEqual(a1["previous_program"], P.STATION)
        self.assertGreaterEqual(a1["carry_wait_days"], 30)
        h.decide_eligibility(self.c2, a1["application_id"], "granted", "已稳定就业，符合保租房条件")
        l1 = h.admit(self.c1, a1["application_id"], ar, T1, T2)

        # 未完成交接不能激活新租约、旧房间仍占用
        with self.assertRaises(ConflictError):
            h.activate_lease(self.c1, l1["lease_id"])
        with self.assertRaises(ConflictError):
            h.admit(self.c1, a1["application_id"], ar, T1, T2)  # 房间已锁定
        ho = h.request_handover(self.c1, l0["lease_id"], "transfer",
                                to_lease_id=l1["lease_id"], scheduled_at=T1)
        done = h.complete_handover(self.c1, ho["handover_id"])
        self.assertEqual(done["released"]["room_id"], sr)
        self.assertEqual(h.get_lease(self.c1, l1["lease_id"])["state"], "active")
        self.assertEqual(h.get_lease(self.c1, l0["lease_id"])["state"], "released")

        bill1 = h.bill_rent(self.c1, l1["lease_id"], "2026-11-02T00:00:00+08:00",
                            "2026-12-02T00:00:00+08:00", 200000)
        h.record_payment(self.c1, bill1["rent_id"], 200000, "2026-12-02T09:00:00+08:00")

        # 阶段 2：家庭收入下降，符合公租房；已缴租金与租期不被重写
        h = self.reopen(T2)
        h.submit_document(self.c1, fam, person, "income",
                          {"period": "2027-01", "monthly_per_capita": 450000}, T2)
        h.submit_document(self.c1, fam, person, "housing_difficulty",
                          {"period": "2027-01", "area_sqm": 8}, T2)
        gp = h.create_project(self.c1, P.PUBLIC_RENTAL, "公租房小区")["project_id"]
        gr = h.add_room(self.c1, gp, "G-303")["room_id"]
        a2 = h.apply(self.c1, fam, person, P.PUBLIC_RENTAL, "round-pr",
                     self.factors(has_employment=True, housing_difficulty=True,
                                  income_per_capita_minor=450000, family_size=2,
                                  needs_care_members=2), request_key="li-pr")
        self.assertEqual(a2["previous_program"], P.AFFORDABLE_RENTAL)
        self.assertEqual(a2["state"], "pending")
        h.decide_eligibility(self.c2, a2["application_id"], "granted", "家庭收入下降且住房困难，转为公租房")
        l2 = h.admit(self.c1, a2["application_id"], gr, T2, T3)

        # 累计实缴租金跨阶段保留
        self.assertEqual(h.total_paid(fam), 280000)

        # 解释报告
        report = h.explain_support(self.c1, fam)
        self.assertEqual([s["program"] for s in report["stages"]],
                         [P.STATION, P.AFFORDABLE_RENTAL, P.PUBLIC_RENTAL])
        self.assertEqual(report["total_rent_paid_minor"], 280000)
        joined = "".join(report["narrative"])
        self.assertIn("转换", joined)
        self.assertIn("轮候", joined)
        timeline = h.family_timeline(self.c1, fam)
        kinds = {e["kind"] for e in timeline}
        self.assertIn("eligibility", kinds)
        self.assertIn("payment", kinds)
        self.assertIn("handover", kinds)

    # ---------- 场景二：重复证明不产生第二次资格，冲突先暂停 ----------

    def test_duplicate_document_is_idempotent_and_conflict_suspends(self):
        fam = self.h.create_family(self.c1, "陈家")["family_id"]
        person = "person:chen"
        self.h.add_member(self.c1, fam, person, "申请人", T0)
        emp = {"period": "2026-10", "employer": "甲公司"}
        d1 = self.h.submit_document(self.c1, fam, person, "employment", emp, T0)
        d2 = self.h.submit_document(self.c1, fam, person, "employment", dict(emp), T0)
        self.assertEqual(d1["state"], "active")
        self.assertEqual(d2["state"], "duplicate")
        self.assertEqual(d1["document_id"], d2["document_id"])

        # 缺件申请提交后是 incomplete，补齐后自动回 pending
        self.h.submit_document(self.c1, fam, person, "social_security",
                               {"period": "2026-10", "months": 6}, T0)
        proj = self.h.create_project(self.c1, P.AFFORDABLE_RENTAL, "保租房")["project_id"]
        self.h.add_room(self.c1, proj, "A1")
        a = self.h.apply(self.c1, fam, person, P.AFFORDABLE_RENTAL, "r",
                         self.factors(has_employment=True, income_per_capita_minor=800000),
                         request_key="chen-aff")
        self.assertEqual(a["state"], "pending")

        # 同周期内容冲突：相关申请被暂停
        conflict = self.h.submit_document(
            self.c1, fam, person, "employment",
            {"period": "2026-10", "employer": "乙公司（与甲冲突）"}, T0)
        self.assertEqual(conflict["state"], "conflicted")
        self.assertIn(a["application_id"], conflict["suspended_applications"])
        with self.assertRaises(ConflictError):
            self.h.decide_eligibility(self.c2, a["application_id"], "granted", "不应被审批")

        # 核查后保留原证明，申请恢复待审
        resolved = self.h.resolve_document_conflict(
            self.c2, conflict["conflict_id"], keep="existing", resolution="核实为甲公司在职")
        ids = [r["application_id"] for r in resolved["resumed"]]
        self.assertIn(a["application_id"], ids)
        self.assertEqual(self.h.get_application(self.c1, a["application_id"])["state"], "pending")

    def test_conflict_blocking_new_application(self):
        fam = self.h.create_family(self.c1, "周家")["family_id"]
        person = "person:zhou"
        self.h.add_member(self.c1, fam, person, "申请人", T0)
        # 先制造收入冲突
        self.h.submit_document(self.c1, fam, person, "income",
                               {"period": "2026-10", "v": 1}, T0)
        conflict = self.h.submit_document(self.c1, fam, person, "income",
                                          {"period": "2026-10", "v": 2}, T0)
        self.assertEqual(conflict["state"], "conflicted")
        # 其余材料齐全
        self.h.submit_document(self.c1, fam, person, "employment", {"period": "2026-10"}, T0)
        self.h.submit_document(self.c1, fam, person, "social_security", {"period": "2026-10"}, T0)
        self.h.submit_document(self.c1, fam, person, "housing_difficulty", {"period": "2026-10"}, T0)
        a = self.h.apply(self.c1, fam, person, P.PUBLIC_RENTAL, "r",
                         self.factors(has_employment=True, housing_difficulty=True,
                                      income_per_capita_minor=100), request_key="z")
        self.assertEqual(a["state"], "suspended")
        self.assertIn("income", a["open_conflicts"])

    # ---------- 场景三：经办与审批分离、减免只影响后续 ----------

    def test_segregation_of_duties_and_future_only_reduction(self):
        fam = self.h.create_family(self.c1, "张家")["family_id"]
        person = "person:zhang"
        self.h.add_member(self.c1, fam, person, "申请人", T0)
        proj = self.h.create_project(self.c1, P.AFFORDABLE_RENTAL, "保租房")["project_id"]
        room = self.h.add_room(self.c1, proj, "A9")["room_id"]
        a = self.h.apply(self.c1, fam, person, P.AFFORDABLE_RENTAL, "r",
                         self.factors(has_employment=True, income_per_capita_minor=800000),
                         request_key="zhang")
        self.h.submit_document(self.c1, fam, person, "employment", {"period": "2026-10"}, T0)
        self.h.submit_document(self.c1, fam, person, "social_security", {"period": "2026-10"}, T0)
        # 录入人自己不能审批
        with self.assertRaises(PermissionDenied):
            self.h.decide_eligibility(self.c1, a["application_id"], "granted", "自审")
        self.h.decide_eligibility(self.c2, a["application_id"], "granted", "符合条件")
        lease = self.h.admit(self.c1, a["application_id"], room, T0, T2)
        self.h.activate_lease(self.c1, lease["lease_id"])

        # 减免 30%，12 月起生效；11 月账单仍全额
        nov = self.h.bill_rent(self.c1, lease["lease_id"], "2026-11-01T00:00:00+08:00",
                               "2026-12-01T00:00:00+08:00", 200000)
        self.assertEqual(nov["due_minor"], 200000)
        red = self.h.apply_reduction(self.c3, lease["lease_id"], 30,
                                     "2026-12-01T00:00:00+08:00", reason="家庭收入下降")
        with self.assertRaises(PermissionDenied):
            self.h.approve_reduction(self.c3, red["reduction_id"])
        self.h.approve_reduction(self.c4, red["reduction_id"])
        dec_after = self.h.bill_rent(self.c1, lease["lease_id"], "2026-12-01T00:00:00+08:00",
                                    "2027-01-01T00:00:00+08:00", 200000)
        self.assertEqual(dec_after["due_minor"], 140000)

        # 缴费事实不可改写、不可重复登记
        self.h.record_payment(self.c1, nov["rent_id"], 200000, T1)
        with self.assertRaises(ConflictError):
            self.h.record_payment(self.c1, nov["rent_id"], 200000, T1)

    # ---------- 场景四：轮候冻结顺位、offer 过期保顺位、释放递补 ----------

    def test_waitlist_frozen_rank_offer_expiry_and_backfill(self):
        # 两个保租房家庭都获批，房间只有一间
        def make_family(name, pid):
            f = self.h.create_family(self.c1, name)["family_id"]
            self.h.add_member(self.c1, f, pid, "申请人", T0)
            self.h.submit_document(self.c1, f, pid, "employment", {"period": "2026-10"}, T0)
            self.h.submit_document(self.c1, f, pid, "social_security", {"period": "2026-10"}, T0)
            a = self.h.apply(self.c1, f, pid, P.AFFORDABLE_RENTAL, "round",
                             self.factors(has_employment=True, income_per_capita_minor=800000),
                             request_key=name)
            self.h.decide_eligibility(self.c2, a["application_id"], "granted", "符合")
            return f, a
        fA, aA = make_family("甲家", "person:A")
        fB, aB = make_family("乙家", "person:B")
        proj = self.h.create_project(self.c1, P.AFFORDABLE_RENTAL, "保租房")["project_id"]
        room1 = self.h.add_room(self.c1, proj, "A1")["room_id"]
        wl = self.h.create_waitlist(self.c1, P.AFFORDABLE_RENTAL, "round", project_id=proj)["waitlist_id"]
        eA = self.h.enqueue(self.c1, wl, fA, aA["application_id"])
        eB = self.h.enqueue(self.c1, wl, fB, aB["application_id"])
        self.assertEqual((eA["rank"], eB["rank"]), (1, 2))

        # 队首获得 offer
        offer = self.h.make_offer(self.c1, wl, room1, valid_hours=48)
        self.assertEqual(offer["entry_id"], eA["entry_id"])
        view = self.h.waitlist_view(self.c1, wl)
        self.assertEqual(view["entries"][0]["state"], "offered")

        # 超过有效期后系统恢复：offer 过期，顺位保留，房间释放回 available
        h = self.reopen("2026-10-05T09:00:00+08:00")
        results = h.process_due_jobs(self.c1)
        self.assertTrue(any(o["outcome"].get("expired") for o in results if "outcome" in o))
        view = h.waitlist_view(self.c1, wl)
        head = next(e for e in view["entries"] if e["family_id"] == fA)
        self.assertEqual(head["state"], "waiting")
        self.assertEqual(head["rank"], 1)
        # 再次配售仍是同一队首（顺位未被后来者顶替）
        offer2 = h.make_offer(self.c1, wl, room1, valid_hours=72)
        self.assertEqual(offer2["entry_id"], eA["entry_id"])
        h.accept_offer(self.c1, eA["entry_id"])
        leaseA = h.get_lease(self.c1, offer2["lease_id"])
        self.assertEqual(leaseA["state"], "active")

        # 甲退租交接完成释放房间，自动按冻结顺位递补给乙
        ho = h.request_handover(self.c1, offer2["lease_id"], "exit", scheduled_at=T1)
        done = h.complete_handover(self.c1, ho["handover_id"])
        self.assertIsNotNone(done["backfill"])
        self.assertEqual(done["backfill"]["entry_id"], eB["entry_id"])
        self.assertEqual(done["backfill"]["rank"], 2)
        view = h.waitlist_view(self.c1, wl)
        b_entry = next(e for e in view["entries"] if e["family_id"] == fB)
        self.assertEqual(b_entry["state"], "offered")

    # ---------- 场景五：申请人只能查看本家庭 ----------

    def test_applicant_scope_limited_to_own_family(self):
        f1 = self.h.create_family(self.c1, "我家")["family_id"]
        f2 = self.h.create_family(self.c1, "别家")["family_id"]
        self.h.add_member(self.c1, f1, "person:me", "申请人", T0)
        me = applicant_access("person:me", f1)
        self.assertTrue(self.h.get_family(me, f1))
        self.assertEqual([e["kind"] for e in self.h.family_timeline(me, f1)], ["member"])
        with self.assertRaises(PermissionDenied):
            self.h.get_family(me, f2)
        with self.assertRaises(PermissionDenied):
            self.h.create_family(me, "不允许")
        with self.assertRaises(PermissionDenied):
            self.h.waitlist_view(me, "waitlist:x")

    # ---------- 场景六：保障路径不可倒流 ----------

    def test_cannot_migrate_backward_to_station(self):
        fam = self.h.create_family(self.c1, "回家")["family_id"]
        person = "person:back"
        self.h.add_member(self.c1, fam, person, "申请人", T0)
        proj = self.h.create_project(self.c1, P.AFFORDABLE_RENTAL, "保租房")["project_id"]
        self.h.add_room(self.c1, proj, "A1")
        self.h.submit_document(self.c1, fam, person, "employment", {"period": "2026-10"}, T0)
        self.h.submit_document(self.c1, fam, person, "social_security", {"period": "2026-10"}, T0)
        a = self.h.apply(self.c1, fam, person, P.AFFORDABLE_RENTAL, "r",
                         self.factors(has_employment=True, income_per_capita_minor=800000),
                         request_key="back-aff")
        self.h.decide_eligibility(self.c2, a["application_id"], "granted", "符合")
        with self.assertRaises(ValidationError):
            self.h.apply(self.c1, fam, person, P.STATION, "r2", self.factors(), request_key="back-station")

    # ---------- 场景七：系统恢复任务 ----------

    def test_recovery_lease_expiry_and_missing_docs(self):
        fam = self.h.create_family(self.c1, "恢复家")["family_id"]
        person = "person:rec"
        self.h.add_member(self.c1, fam, person, "申请人", T0)
        # 缺件申请会登记缺件提醒任务
        a = self.h.apply(self.c1, fam, person, P.AFFORDABLE_RENTAL, "r",
                         self.factors(has_employment=True, income_per_capita_minor=800000),
                         request_key="rec")
        self.assertEqual(a["state"], "incomplete")
        proj = self.h.create_project(self.c1, P.AFFORDABLE_RENTAL, "保租房")["project_id"]
        room = self.h.add_room(self.c1, proj, "A1")["room_id"]
        self.h.submit_document(self.c1, fam, person, "employment", {"period": "2026-10"}, T0)
        self.h.submit_document(self.c1, fam, person, "social_security", {"period": "2026-10"}, T0)
        self.h.decide_eligibility(self.c2, a["application_id"], "granted", "符合")
        lease = self.h.admit(self.c1, a["application_id"], room, T0, T1)
        self.h.activate_lease(self.c1, lease["lease_id"])

        # 到达租约结束时间，恢复任务把租约标记为即将到期（交接未完成时不释放房源）
        h = self.reopen(T1)
        results = h.process_due_jobs(self.c1)
        types_run = {o["job_type"] for o in results}
        self.assertIn("lease_expiry", types_run)
        # 房源释放任务因无交接而进入重试，而不是错误释放
        release = [o for o in results if o["job_type"] == "room_release"]
        if release:
            self.assertIn("error", release[0])
        # 完成交接后再次跑恢复任务，房源正常释放
        ho = h.request_handover(self.c1, lease["lease_id"], "exit", scheduled_at=T1)
        h.complete_handover(self.c1, ho["handover_id"])
        h.process_due_jobs(self.c1)


if __name__ == "__main__":
    unittest.main()
