"""住房保障项目的政策条件与资格评估。

四类保障采用不同条件；政策以版本化 JSON 保存在项目上，认定时固化
policy_digest 与事实快照，事后政策调整不会重写历史认定。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .jsonutil import digest_json

PROGRAMS = ("station", "affordable_rental", "public_rental", "anju")

PROGRAM_LABELS = {
    "station": "筑梦驿站",
    "affordable_rental": "保障性租赁住房",
    "public_rental": "公共租赁住房",
    "anju": "安居房",
}

# 金额单位为人民币分（月人均收入）；None 表示该项目不设收入线。
DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "station": {
        "program": "station",
        "version": "station-2026.1",
        "title": "筑梦驿站短期住宿",
        "required_docs": ["id_card", "job_seeker", "housing_difficulty_proof"],
        "min_social_security_months": 0,
        "max_income_per_capita_minor": None,
        "eligibility_days": 90,
    },
    "affordable_rental": {
        "program": "affordable_rental",
        "version": "baozu-2026.1",
        "title": "保障性租赁住房",
        "required_docs": ["id_card", "employment", "social_security", "housing_difficulty_proof"],
        "min_social_security_months": 1,
        "max_income_per_capita_minor": 1_500_000,
        "eligibility_days": 730,
    },
    "public_rental": {
        "program": "public_rental",
        "version": "gongzu-2026.1",
        "title": "公共租赁住房",
        "required_docs": ["id_card", "employment_status", "social_security", "income_proof", "housing_difficulty_proof"],
        "min_social_security_months": 6,
        "max_income_per_capita_minor": 450_000,
        "eligibility_days": 730,
    },
    "anju": {
        "program": "anju",
        "version": "anju-2026.1",
        "title": "安居房",
        "required_docs": ["id_card", "employment", "social_security", "income_proof", "housing_difficulty_proof"],
        "min_social_security_months": 60,
        "max_income_per_capita_minor": 2_000_000,
        "eligibility_days": 1825,
    },
}

# 每名需照顾家庭成员在轮候中折算的优先天数，单户最多计两人。
CARE_BONUS_DAYS_PER_PERSON = 30
CARE_BONUS_MAX_PERSONS = 2


def default_policy(program: str) -> dict[str, Any]:
    if program not in DEFAULT_POLICIES:
        raise ValueError(f"未知住房保障类型: {program}")
    return deepcopy(DEFAULT_POLICIES[program])


def merge_policy(program: str, override: dict[str, Any] | None) -> dict[str, Any]:
    policy = default_policy(program)
    if override:
        for key in ("version", "required_docs", "min_social_security_months",
                    "max_income_per_capita_minor", "eligibility_days", "title"):
            if key in override and override[key] is not None:
                policy[key] = override[key]
    policy["program"] = program
    if sorted(policy["required_docs"]) != policy["required_docs"]:
        policy["required_docs"] = list(policy["required_docs"])
    return policy


def policy_digest(policy: dict[str, Any]) -> str:
    return digest_json({k: policy.get(k) for k in (
        "program", "version", "required_docs", "min_social_security_months",
        "max_income_per_capita_minor", "eligibility_days")})


def care_bonus_days(care_dependents: int) -> int:
    persons = max(0, min(int(care_dependents), CARE_BONUS_MAX_PERSONS))
    return persons * CARE_BONUS_DAYS_PER_PERSON


def evaluate(policy: dict[str, Any], facts: dict[str, Any], verified_docs: set[str]) -> dict[str, Any]:
    """按项目政策评估，返回是否符合及逐条理由（事实快照由调用方固化）。"""
    reasons: list[str] = []
    ok = True

    missing = [doc for doc in policy["required_docs"] if doc not in verified_docs]
    if missing:
        ok = False
        reasons.append("缺少已核验证明: " + ",".join(missing))

    ss_months = int(facts.get("social_security_months", 0) or 0)
    if ss_months < int(policy["min_social_security_months"]):
        ok = False
        reasons.append(
            f"社保连续缴纳 {ss_months} 个月，不足 {policy['min_social_security_months']} 个月")

    cap = policy.get("max_income_per_capita_minor")
    income = int(facts.get("monthly_income_per_capita_minor", 0) or 0)
    if cap is not None and income > int(cap):
        ok = False
        reasons.append(f"家庭月人均收入 {income} 分，超过收入线 {int(cap)} 分")

    if not bool(facts.get("housing_difficulty", False)):
        ok = False
        reasons.append("未认定住房困难")

    if ok:
        reasons.append(f"符合《{policy['title']}》{policy['version']} 条件")
    return {"eligible": ok, "reasons": reasons, "missing_docs": missing}
