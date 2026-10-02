"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .security import AccessContext
from .housing import staff_access
from . import housing_policies as P


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def housing_demo(app: CivicFlow) -> dict:
    """青年求职入住驿站 -> 就业转保租房 -> 家庭收入下降转公租房的资格迁移。"""
    h = app.housing
    clerk = staff_access("demo-clerk")
    reviewer = staff_access("demo-reviewer")
    person = "person:demo-youth"
    family = h.create_family(clerk, "青年家庭")["family_id"]
    h.add_member(clerk, family, person, "申请人", app.clock.now(), needs_care=True)

    def program_room(program: str, project_name: str, code: str) -> tuple[str, str]:
        project = h.create_project(clerk, program, project_name)["project_id"]
        room = h.add_room(clerk, project, code)["room_id"]
        return project, room

    factors = {"family_size": 1, "needs_care_members": 1}

    # 阶段一：短期驿站（不审收入）
    _, station_room = program_room(P.STATION, "筑梦驿站", "S-101")
    a0 = h.apply(clerk, family, person, P.STATION, "demo-station",
                 {**factors, "has_employment": False, "housing_difficulty": False, "settled": False,
                  "income_per_capita_minor": None}, request_key="demo-station")
    h.decide_eligibility(reviewer, a0["application_id"], "granted", "求职者短期过渡入住驿站")
    lease0 = h.admit(clerk, a0["application_id"], station_room, app.clock.now(), "2026-12-31T09:00:00+08:00")
    h.activate_lease(clerk, lease0["lease_id"])
    bill0 = h.bill_rent(clerk, lease0["lease_id"], "2026-10-02T00:00:00+08:00", "2026-11-02T00:00:00+08:00", 80000)
    h.record_payment(clerk, bill0["rent_id"], 80000, app.clock.now())

    # 阶段二：就业，提交就业与社保证明，申请保租房（携带既往轮候时间）
    h.submit_document(clerk, family, person, "employment", {"period": "2026-10", "employer": "某科技公司"}, app.clock.now())
    h.submit_document(clerk, family, person, "social_security", {"period": "2026-10", "months": 3}, app.clock.now())
    _, aff_room = program_room(P.AFFORDABLE_RENTAL, "青年保租房", "A-202")
    a1 = h.apply(clerk, family, person, P.AFFORDABLE_RENTAL, "demo-aff",
                 {**factors, "has_employment": True, "housing_difficulty": False, "settled": False,
                  "income_per_capita_minor": 900000}, request_key="demo-aff")
    h.decide_eligibility(reviewer, a1["application_id"], "granted", "稳定就业，符合保障性租赁住房条件")
    lease1 = h.admit(clerk, a1["application_id"], aff_room, app.clock.now(), "2027-10-02T09:00:00+08:00")
    handover = h.request_handover(clerk, lease0["lease_id"], "transfer", to_lease_id=lease1["lease_id"],
                                  scheduled_at=app.clock.now())
    h.complete_handover(clerk, handover["handover_id"])
    bill1 = h.bill_rent(clerk, lease1["lease_id"], "2026-10-02T00:00:00+08:00", "2026-11-02T00:00:00+08:00", 200000)
    h.record_payment(clerk, bill1["rent_id"], 200000, app.clock.now())

    # 阶段三：家庭收入下降并认定住房困难，申请公租房（驿站/保租房阶段事实保留）
    h.submit_document(clerk, family, person, "income", {"period": "2026-10", "monthly_per_capita": 450000}, app.clock.now())
    h.submit_document(clerk, family, person, "housing_difficulty", {"period": "2026-10", "area_sqm": 8}, app.clock.now())
    _, pr_room = program_room(P.PUBLIC_RENTAL, "公租房小区", "G-303")
    a2 = h.apply(clerk, family, person, P.PUBLIC_RENTAL, "demo-pr",
                 {**factors, "has_employment": True, "housing_difficulty": True, "settled": False,
                  "income_per_capita_minor": 450000}, request_key="demo-pr")
    h.decide_eligibility(reviewer, a2["application_id"], "granted", "家庭人均收入下降至公租房线以下且住房困难")
    lease2 = h.admit(clerk, a2["application_id"], pr_room, app.clock.now(), "2028-10-02T09:00:00+08:00")

    explanation = h.explain_support(reviewer, family)
    return {"family_id": family, "stages": explanation["stages"],
            "total_rent_paid_minor": explanation["total_rent_paid_minor"],
            "current_pending_lease": lease2["lease_id"], "narrative": explanation["narrative"],
            "timeline_events": len(h.family_timeline(reviewer, family))}


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("housing-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "housing-demo": emit(housing_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
