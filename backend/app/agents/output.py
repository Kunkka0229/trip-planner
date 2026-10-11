"""大模型输出的"安检门"：提取 JSON → 结构校验 → 业务校验 → 预算重算

核心思想：大模型的输出是"不可信的外部输入"，和用户提交的表单一样，必须校验
"""

import json
import math
from datetime import timedelta
from typing import Any

from app.models.schemas import Budget, TripPlan, TripRequest

# 包含的餐食类型
# set：无序不重复集合，适合用来判断一个元素是否在集合里
# set 可以使用 &、|、- 运算
REQUIRED_MEALS = {"breakfast", "lunch", "dinner"}

class PlanValidationError(ValueError):
    """行程不符合业务规则。错误信息会反馈给大模型，所以要写得具体、可操作"""

def extract_json_object(text: str) -> dict[str, Any]:
    """从大模型的回复里抠出第一个完整的 JSON 对象

    能处理：
        - 前后有多余的文字："好的，这是您的行程：{...} 祝您旅途愉快"
        - Markdown 代码块：```json {...} ```
    """
    
    # 1. 去掉 Markdown 代码块
    decoder = json.JSONDecoder()
    # 从每一个 "{" 的位置尝试解析，直到成功
    start = text.find("{")
    while start != -1:
        try:
            # raw_decode：从字符串开头解析出一个完整的 JSON 值，返回 (对象, 结束位置)
            # 和 json.loads 的区别是：后面跟着多余的文字也不会报错
            # decoder = json.JSONDecoder()
            # obj, end = decoder.raw_decode('{"city": "北京"} 祝您旅途愉快')
            # obj = {'city': '北京'}
            # end = 14
            obj, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            # 解析失败，说明这个 "{" 不是 JSON 的开头，继续找下一个 "{"
            start = text.find("{", start + 1)
            continue
        
        if isinstance(obj, dict) and "days" in obj:
            # 只要包含 "days" 字段，就认为是我们想要的行程 JSON
            return obj
        # 解析出来的是某个内层小对象（比如 location），说明外层 JSON 不完整，继续往后找
        start = text.find("{", start + 1)
    
    raise PlanValidationError("输出中没有找到完整的行程 JSON 对象（可能被截断，或者不是 JSON）")

def trip_dates(request: TripRequest) -> list[str]:
    """行程中每一天的日期字符串（从 trip_planner.py 挪到这里，两个文件共用）"""
    return[(request.start_date + timedelta(days=i)).isoformat() for i in range(request.travel_days)]

def validate_plan(plan: TripPlan, request: TripRequest) -> None:
    """Pydantic 只能保证"类型对"，这里检查"业务对"。发现问题就抛 PlanValidationError"""

    # 日期校验
    dates = trip_dates(request)
    if len(plan.days) != len(dates):
        raise PlanValidationError(f"days 应该有 {len(dates)} 天，实际有 {len(plan.days)} 天")
    
    # zip 把两个列表按位置配对，相当于 JS 的 plan.days.map((day, i) => [day, dates[i]])
    # enumerate 同时拿到下标和值：for (const [i, x] of arr.entries())
    for i, (day, date) in enumerate(zip(plan.days, dates)):
        label = f"第 {i + 1} 天"
        if day.date != date:
            raise PlanValidationError(f"{label} 的 date 应为 {date}，实际为 {day.date}")

        # Python 支持连续比较：1 <= x <= 3
        if not 1 <= len(day.attractions) <= 3:
            raise PlanValidationError(f"{label} 应安排 1-3 个景点，实际 {len(day.attractions)} 个")

        meal_types = {meal.type for meal in day.meals}  # 集合推导式，得到这一天出现过的餐别

        missing = REQUIRED_MEALS - meal_types  # 集合差集：应该有但是没有的
        if missing:
            raise PlanValidationError(f"{label} 缺少餐食: {', '.join(sorted(missing))}")
        
def recompute_budget(plan: TripPlan, request: TripRequest) -> Budget:
    """后端重新计算预算：大模型很不擅长算乘法和加法，单价可以让它填，总价必须由代码算

    计价口径：
      - 门票、餐饮：单价 × 人数
      - 酒店：单价（每间每晚）× 晚数 × 房间数；房间数按 2 人一间、向上取整
      - 交通：沿用大模型的估算（目前没有更可靠的数据来源）
    """
    
    # 计算总人数
    people = request.party.total
    # 计算房间数（2 人一间，向上取整）3 个人 → 2 间房
    rooms = math.ceil(people / 2)
    # 大模型没输出 budget 时用全 0 的默认值；下面只用它的交通费（其他三项由代码重算）
    llm_budget = plan.budget or Budget()
    
    return Budget(
        total_attractions=sum(a.ticket_price for d in plan.days for a in d.attractions) * people,
        total_meals=sum(m.estimated_cost for d in plan.days for m in d.meals) * people,
        total_hotels=sum(d.hotel.estimated_cost for d in plan.days if d.hotel) * rooms,
        total_transportation=llm_budget.total_transportation,
    )

def parse_and_check(text: str, request: TripRequest) -> TripPlan:
    """完整的安检流程：任何一步失败都会抛出 ValueError（或其子类）"""

    # 1. 抠出 JSON
    data = extract_json_object(text)
    # 2. Pydantic 校验类型（字段缺失、类型不对、格式不对都会抛 ValidationError）
    plan = TripPlan.model_validate(data)
    
    # 能直接修正的就修正，不必为此重试一次：顶层的城市和日期以用户请求为准
    plan.city = request.city
    plan.start_date = request.start_date.isoformat()
    plan.end_date = request.end_date.isoformat()
    
    # 3. 业务规则校验
    validate_plan(plan, request)
    
    # 4. 预算重算
    plan.budget = recompute_budget(plan, request)

    return plan
    