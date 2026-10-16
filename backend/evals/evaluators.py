"""规则评估器：每个函数检查行程的一个方面，返回 0~1 的分数；返回 None 表示"这一项不适用于这个用例"

规则评估器 = 确定性、免费、快。能用规则判断的，就不要交给 LLM 评委
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from app.models.schemas import TripPlan, TripRequest
from app.planner.grounding import find_candidate


@dataclass
class CaseOutput:
    """一个用例跑完之后，评估器需要的全部信息"""

    request: TripRequest
    context: dict[str, Any]
    plan: TripPlan
    source: str  # "llm" | "fallback"
    attempts: int


def success(out: CaseOutput) -> float | None:
    """大模型成功生成（没有走兜底）"""
    return 1.0 if out.source == "llm" else 0.0


def first_try(out: CaseOutput) -> float | None:
    """第一次就生成成功。重试越多，延迟和成本越高"""
    if out.source != "llm":
        return None
    return 1.0 if out.attempts == 1 else 0.0


def grounding(out: CaseOutput) -> float | None:
    """景点、酒店、午晚餐中，能在高德候选里找到的比例。越低说明模型越爱编"""
    if out.source != "llm":
        return None
    snapshot = out.context["tool_snapshot"]
    total = matched = 0
    for day in out.plan.days:
        items = [(a.name, "attraction_pois") for a in day.attractions]
        if day.hotel:
            items.append((day.hotel.name, "hotel_pois"))
        items += [(m.name, "food_pois") for m in day.meals if m.type in ("lunch", "dinner")]
        for name, key in items:
            total += 1
            matched += find_candidate(name, snapshot[key]) is not None
    return matched / total if total else None


def budget_fit(out: CaseOutput) -> float | None:
    """有预算约束时：不超预算得 1 分；超了按超出比例扣分，硬约束超了直接 0 分"""
    constraint = out.request.budget_constraint
    if out.source != "llm" or constraint.amount is None or constraint.strictness == "none" or out.plan.budget is None:
        return None
    total, amount = out.plan.budget.total, constraint.amount
    if total <= amount:
        return 1.0
    if constraint.strictness == "hard":
        return 0.0
    return max(0.0, 1 - (total - amount) / amount)


def pace(out: CaseOutput) -> float | None:
    """节奏：每天游览总时长不超过上限的天数占比。带婴儿或儿童时上限更低"""
    if out.source != "llm":
        return None
    party = out.request.party
    limit = 300 if party.infants or party.children else 480  # 分钟
    ok_days = sum(sum(a.visit_duration for a in day.attractions) <= limit for day in out.plan.days)
    return ok_days / len(out.plan.days)


def meal_variety(out: CaseOutput) -> float | None:
    """同一天午餐和晚餐不是同一家的天数占比"""
    if out.source != "llm":
        return None
    ok_days = 0
    for day in out.plan.days:
        names = [m.name for m in day.meals if m.type in ("lunch", "dinner")]
        ok_days += len(names) == len(set(names))  # 有重复时 set 会变短
    return ok_days / len(out.plan.days)


def no_repeat_attractions(out: CaseOutput) -> float | None:
    """整个行程里景点不重复的比例（同一个景点安排两天，多半是候选不够、模型在凑数）"""
    if out.source != "llm":
        return None
    names = [a.name for day in out.plan.days for a in day.attractions]
    if not names:
        return None
    repeats = sum(count - 1 for count in Counter(names).values())
    return 1 - repeats / len(names)


def compact_route(out: CaseOutput) -> float | None:
    """每天的景点分布在不超过 2 个区县的天数占比（区县来自高德候选）"""
    if out.source != "llm":
        return None
    pois = out.context["tool_snapshot"]["attraction_pois"]
    ok_days = 0
    for day in out.plan.days:
        districts = {row["district"] for a in day.attractions if (row := find_candidate(a.name, pois))}
        ok_days += len(districts) <= 2
    return ok_days / len(out.plan.days)


# 名字 → 评估器。加一个新指标 = 写一个函数 + 在这里登记
EVALUATORS: dict[str, Callable[[CaseOutput], float | None]] = {
    "success": success,
    "first_try": first_try,
    "grounding": grounding,
    "budget_fit": budget_fit,
    "pace": pace,
    "meal_variety": meal_variety,
    "no_repeat_attractions": no_repeat_attractions,
    "compact_route": compact_route,
}


def evaluate(out: CaseOutput) -> dict[str, float | None]:
    return {name: func(out) for name, func in EVALUATORS.items()}
