"""Grounding（接地）：检查大模型选的景点、酒店、餐厅是不是真的来自候选数据

做两件事：
  1. 找到对应的候选 → 用候选的真实数据覆盖模型写的坐标、地址、价格（模型抄错了也能纠正回来）
  2. 找不到 → 记下来，算出 grounding 率，作为质量指标
"""
import re
from typing import Any

from app.models.schemas import Location, TripPlan

def normalize_name(name: str) -> str:
    """去掉空格和括号里的分店信息，例如 "全聚德(前门店)" → "全聚德"，提高匹配成功率"""
    # 正则：删除中英文括号及其内容
    name = re.sub(r"[（(].*?[)）]", "", name)
    return re.sub(r"\s+", "", name).lower()

def find_candidate(name: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按名称查找候选：先精确匹配，再看两者是否互相包含"""
    target = normalize_name(name)
    if not target:
        return None
    for row in rows:
        if normalize_name(row["name"]) == target:
            return row
        
    for row in rows:
        candidate = normalize_name(row["name"])
        if target in candidate or candidate in target:
            return row
    
    return None

def ground_plan(plan: TripPlan, context: dict[str, Any]) -> dict[str, Any]:
    """原地修正 plan，返回统计信息 {"attraction_rate": 0.9, "ungrounded": [...]}"""

    # 从 context 里取出各个工具收集到的候选数据（景点、酒店、餐厅、天气）
    # 后面用大模型写的名字，去这里面找对应的真实候选
    snapshot = context["tool_snapshot"]
    # 记录"在候选里找不到"的项目，例如 ["景点:某某山", "餐厅:老王家"]
    # 最后会变成返回给前端的警告，也用来判断模型是不是在编造
    ungrounded: list[str] = []
    # 连续赋值：total 和 matched 都设为 0，相当于 JS 的 let total = 0, matched = 0
    # total   = 行程里一共有几个景点
    # matched = 其中有几个在候选里找到了
    # 最后用 matched / total 算出景点命中率（attraction_rate）
    total = matched = 0
    
    for day in plan.days:
        for attraction in day.attractions:
            total += 1
            row = find_candidate(attraction.name, snapshot["attraction_pois"])
            if row is None:
                ungrounded.append(f"景点:{attraction.name}")
                continue
            matched += 1
            attraction.location = Location(**row["location"])  # **dict 展开成关键字参数
            attraction.address = attraction.address or row["address"]
            attraction.ticket_price = row["ticket_price_hint"]  # 价格以 hint 为准，模型改价格也没用
        
        if day.hotel:
            row = find_candidate(day.hotel.name, snapshot["hotel_pois"])
            if row is None:
                ungrounded.append(f"酒店:{day.hotel.name}")
            else:
                day.hotel.location = Location(**row["location"])
                day.hotel.estimated_cost = row["estimated_cost_hint"]
            
        for meal in day.meals:
            if meal.type not in ("lunch", "dinner"):
                continue  # 早餐可能是酒店早餐，不强求来自候选
            row = find_candidate(meal.name, snapshot["food_pois"])
            if row is None:
                ungrounded.append(f"餐厅:{meal.name}")
            else:
                meal.estimated_cost = row["meal_cost_hint"]
        
    rate = matched / total if total else 0.0
    return {"attraction_rate": round(rate, 2), "ungrounded": ungrounded}
    