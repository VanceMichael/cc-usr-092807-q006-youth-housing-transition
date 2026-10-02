"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .housing_policy import default_policy
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


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


def housing_demo(app: CivicFlow) -> dict:
    """演示青年求职者驿站 → 保租房 → 公租房的资格迁移完整轨迹。"""
    from .security import AccessContext
    recorder = AccessContext.system("staff:wang")
    reviewer = AccessContext.system("staff:chen")
    h = app.housing
    family = h.create_family(recorder, applicant_id="person:demo-lin", label="林晓演示家庭",
                             request_key="hd-fam")
    fid = family["family_id"]
    h.add_member(recorder, fid, person_id="person:demo-ma", role="elder",
                 care_need="失能照护", request_key="hd-rel")

    docs = {
        "id_card": {"number": "DEMO-ID", "name": "林晓"},
        "job_seeker": {"status": "unemployed"},
        "employment": {"employer": "星河科技"},
        "employment_status": {"status": "employed", "employer": "星河科技"},
        "social_security": {"months": 9},
        "income_proof": {"monthly_total_minor": 800000},
        "housing_difficulty_proof": {"kind": "无房"},
    }

    def submit(program: str) -> None:
        for doc_type in default_policy(program)["required_docs"]:
            result = h.submit_document(
                recorder, fid, person_id="person:demo-lin", doc_type=doc_type,
                dedup_key=f"demo:{program}:{doc_type}", payload=docs[doc_type],
                request_key=f"hd-doc-{program}-{doc_type}")
            if result.get("acceptance") == "accepted":
                h.verify_document(recorder, result["document_id"],
                                  request_key=f"hd-verify-{program}-{doc_type}")

    rooms = {}
    for program, name, rent in (("station", "演示筑梦驿站", "30.00"),
                                ("affordable_rental", "演示青年保租房", "1200.00"),
                                ("public_rental", "演示馨和公租房", "800.00")):
        project = h.register_project(recorder, program=program, name=name,
                                     request_key=f"hd-proj-{program}")
        room = h.add_room(recorder, project["project_id"], label=f"demo-{program[0]}",
                          monthly_rent=rent, request_key=f"hd-room-{program}")
        rooms[program] = (project["project_id"], room["room_id"])

    # 驿站阶段
    submit("station")
    app_s = h.apply(recorder, fid, applicant_id="person:demo-lin", program="station",
                    project_id=rooms["station"][0], request_key="hd-app-s")
    h.decide_application(reviewer, app_s["application_id"], decision="approved",
                         facts={"social_security_months": 0,
                                "monthly_income_per_capita_minor": 0,
                                "housing_difficulty": True},
                         effective_from="2026-10-01T12:00:00+08:00",
                         request_key="hd-dec-s")
    lease_s = h.check_in(recorder, fid, person_id="person:demo-lin",
                         room_id=rooms["station"][1],
                         start_at="2026-10-01T12:00:00+08:00", request_key="hd-lease-s")
    for period in ("2026-10", "2026-11"):
        h.record_payment(recorder, lease_s["lease_id"], period=period, amount="30.00",
                         request_key=f"hd-pay-s-{period}")

    # 退出驿站，入住保租房
    handover_s = h.begin_handover(
        recorder, lease_s["lease_id"], kind="exit",
        checklist=["费用结清", "房屋验收"], request_key="hd-hand-s")
    for item in ("费用结清", "房屋验收"):
        h.complete_handover_item(recorder, handover_s["handover_id"], item,
                                 request_key=f"hd-item-s-{item}")
    h.complete_handover(recorder, handover_s["handover_id"], request_key="hd-comp-s")
    submit("affordable_rental")
    app_b = h.apply(recorder, fid, applicant_id="person:demo-lin", program="affordable_rental",
                    project_id=rooms["affordable_rental"][0], request_key="hd-app-b")
    h.decide_application(reviewer, app_b["application_id"], decision="approved",
                         facts={"social_security_months": 2,
                                "monthly_income_per_capita_minor": 900000,
                                "housing_difficulty": True},
                         effective_from="2026-12-01T12:00:00+08:00",
                         request_key="hd-dec-b")
    lease_b = h.check_in(recorder, fid, person_id="person:demo-lin",
                         room_id=rooms["affordable_rental"][1],
                         start_at="2026-12-20T12:00:00+08:00", request_key="hd-lease-b")
    h.record_payment(recorder, lease_b["lease_id"], period="2026-12", amount="1200.00",
                     request_key="hd-pay-b-12")

    explanation = h.explain_support(reviewer, fid)
    return {
        "family_id": fid,
        "stages": [{"program": s["program"], "state": s["state"],
                    "effective_from": s["effective_from"], "effective_to": s["effective_to"],
                    "rent_paid_minor": s["rent_paid_minor"]} for s in explanation["stages"]],
        "narrative": explanation["narrative"],
        "verification": app.verify(),
    }


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
    if args.command == "housing-demo" and args.now is None:
        args.now = "2026-10-01T09:00:00+08:00"
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "housing-demo": emit(housing_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
