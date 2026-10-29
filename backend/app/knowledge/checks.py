"""用知识库里的结构化事实检查行程（P9）"""

from datetime import date

from app.knowledge.base import KnowledgeBase
from app.models.schemas import TripPlan

WEEKDAY_NAMES = "一二三四五六日"

def closure_warnings(plan: TripPlan, kb: KnowledgeBase | None, city: str) -> list[str]:
    """P9：用知识库里的结构化事实检查行程：有没有安排在闭馆日去的景点

    这是知识库的另一种用法：不是"检索一段文字给模型看"，而是"读出确定的事实，由代码检查"
    "周一闭馆"这种事实，交给代码判断 100% 可靠；交给模型判断，它可能会忽略
    """
    
    if kb is None:
        return []
    
    closures = kb.closures(city)
    warnings = []
    for i, day in enumerate(plan.days):
        weekday = date.fromisoformat(day.date).isoweekday()  # 1=周一 … 7=周日

        for attraction in day.attractions:
            for name, closed in closures.items():
                if weekday in closed and (name in attraction.name or attraction.name in name):
                    warnings.append(f"第 {i + 1} 天（{day.date} 周{WEEKDAY_NAMES[weekday - 1]}）{attraction.name}闭馆")
    
    return warnings

