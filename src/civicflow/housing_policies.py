"""住房保障资格政策。

短期驿站、保障性租赁住房、公租房、安居房采用各自不同的准入条件。
政策带版本号；一次资格认定使用当时版本并把判定要素快照下来，
使后续规则调整不会改写已作出的认定（资格变化只影响后续安排）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import ValidationError

STATION = "station"
AFFORDABLE_RENTAL = "affordable_rental"
PUBLIC_RENTAL = "public_rental"
SETTLED = "settled"

PROGRAMS = (STATION, AFFORDABLE_RENTAL, PUBLIC_RENTAL, SETTLED)

PROGRAM_LABELS = {
    STATION: "短期驿站",
    AFFORDABLE_RENTAL: "保障性租赁住房",
    PUBLIC_RENTAL: "公租房",
    SETTLED: "安居房",
}

# 同一家庭在同一时间只能实际占有/锁定一种保障住房；但资格可以按时间先后迁移。
MIGRATION_PATH = (STATION, AFFORDABLE_RENTAL, PUBLIC_RENTAL, SETTLED)


@dataclass(frozen=True)
class Criteria:
    program: str
    version: str
    # 收入上限（家庭月人均，分）；None 表示不设收入门槛
    income_per_capita_ceiling_minor: int | None
    # 是否要求在本地连续缴纳社保/就业
    require_local_employment: bool
    # 是否要求住房困难认定
    require_housing_difficulty: bool
    # 是否要求户籍/长期居留（安居房）
    require_settlement: bool
    # 驿站只看“求职者”身份与短期过渡，不审收入
    description: str

    def evaluate(self, factors: "EligibilityFactors") -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self.require_local_employment and not factors.has_employment:
            reasons.append("缺少有效就业或社保证明")
        if self.require_housing_difficulty and not factors.housing_difficulty:
            reasons.append("缺少住房困难认定")
        if self.require_settlement and not factors.settled:
            reasons.append("不符合户籍或长期居留条件")
        if self.income_per_capita_ceiling_minor is not None:
            if factors.income_per_capita_minor is None:
                reasons.append("缺少收入认定")
            elif factors.income_per_capita_minor > self.income_per_capita_ceiling_minor:
                reasons.append("家庭人均收入超出准入标准")
        return (not reasons), reasons

    def snapshot(self) -> dict:
        return {
            "program": self.program,
            "version": self.version,
            "income_per_capita_ceiling_minor": self.income_per_capita_ceiling_minor,
            "require_local_employment": self.require_local_employment,
            "require_housing_difficulty": self.require_housing_difficulty,
            "require_settlement": self.require_settlement,
            "description": self.description,
        }


@dataclass(frozen=True)
class EligibilityFactors:
    """一次资格判定所依据的、由证明与家庭信息汇总出的要素。"""

    has_employment: bool
    housing_difficulty: bool
    settled: bool
    income_per_capita_minor: int | None
    family_size: int
    needs_care_members: int

    def snapshot(self) -> dict:
        return {
            "has_employment": self.has_employment,
            "housing_difficulty": self.housing_difficulty,
            "settled": self.settled,
            "income_per_capita_minor": self.income_per_capita_minor,
            "family_size": self.family_size,
            "needs_care_members": self.needs_care_members,
        }


# 政策版本：数值单位为“分/月/人”。公租房收入线低于保租房；驿站不设收入线。
CRITERIA: dict[str, dict[str, Criteria]] = {
    STATION: {
        "2026.1": Criteria(STATION, "2026.1", None, False, False, False,
                           "面向求职过渡人员，短期入住，不审核家庭收入"),
    },
    AFFORDABLE_RENTAL: {
        "2026.1": Criteria(AFFORDABLE_RENTAL, "2026.1", 12_000_00, True, False, False,
                           "新市民、青年人，有稳定就业，不限户籍，收入线较宽"),
    },
    PUBLIC_RENTAL: {
        "2026.1": Criteria(PUBLIC_RENTAL, "2026.1", 6_000_00, True, True, False,
                           "中低收入且住房困难家庭，需就业/社保与住房困难双认定"),
    },
    SETTLED: {
        "2026.1": Criteria(SETTLED, "2026.1", 15_000_00, True, False, True,
                           "符合长期居留条件的家庭购置或长期承租的安居房"),
    },
}

DEFAULT_POLICY_VERSION = "2026.1"


def criteria_for(program: str, version: str | None = None) -> Criteria:
    if program not in CRITERIA:
        raise ValidationError("未知保障类型")
    versions = CRITERIA[program]
    version = version or DEFAULT_POLICY_VERSION
    if version not in versions:
        raise ValidationError("未知政策版本")
    return versions[version]


def can_migrate(previous: str | None, target: str) -> bool:
    if previous is None:
        return True
    if previous not in MIGRATION_PATH or target not in MIGRATION_PATH:
        return False
    # 允许沿保障路径向前迁移（含跨越，如驿站后家庭困难直接申请公租房）；不允许后退到驿站。
    return MIGRATION_PATH.index(target) >= MIGRATION_PATH.index(previous)


def assert_program(program: str) -> str:
    if program not in PROGRAMS:
        raise ValidationError("未知保障类型: " + str(program))
    return program


def factors_from_mapping(values: Mapping[str, object]) -> EligibilityFactors:
    def as_bool(key: str) -> bool:
        value = values.get(key, False)
        if not isinstance(value, bool):
            raise ValidationError(f"{key} 必须是布尔值")
        return value

    income = values.get("income_per_capita_minor")
    if income is not None and not isinstance(income, int):
        raise ValidationError("收入必须是以分为单位的整数")
    family_size = values.get("family_size", 1)
    if not isinstance(family_size, int) or family_size < 1:
        raise ValidationError("家庭人数必须是正整数")
    needs_care = values.get("needs_care_members", 0)
    if not isinstance(needs_care, int) or needs_care < 0 or needs_care > family_size:
        raise ValidationError("需照料成员数不合法")
    return EligibilityFactors(
        has_employment=as_bool("has_employment"),
        housing_difficulty=as_bool("housing_difficulty"),
        settled=as_bool("settled"),
        income_per_capita_minor=income,
        family_size=family_size,
        needs_care_members=needs_care,
    )
