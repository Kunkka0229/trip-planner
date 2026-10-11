"""PlannerContext：把规划需要的所有信息，整理成一份结构化、可审计的"案卷"再交给大模型

和第 8 课多 Agent 版的对比：
  多 Agent：大模型决定调用什么工具 → 结果被总结成自然语言 → 工具出错也没人知道
  Context：代码决定调用什么工具（并行执行）→ 保留结构化数据 → 每个工具的成败都记在 tool_status 里
"""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.output import trip_dates
from app.models.schemas import POIInfo, TripRequest, WeatherInfo
from app.planner.pricing import hotel_cost_hint, meal_cost_hint, ticket_price_hint
from app.services.amap_client import AmapClient

def poi_row(poi: POIInfo, **hints: Any) -> dict[str, Any]:
    """POI → 给大模型看的精简 dict，**hints 用来附加价格提示等字段（相当于 JS 的 ...rest）"""
    return {
        "name": poi.name,
        "district": poi.district,
        "address": poi.address[:40],  # 截断过长的地址，节省 token
        "location": poi.location.model_dump(),
        "rating": poi.rating,
        **hints,
    }

def align_weather(request: TripRequest, forecasts: list[WeatherInfo]) -> list[dict[str, Any]]:
    """把高德的预报（只有今天起 4 天）对齐到行程的每一天，没有预报的日期明确标注出来（第 5 课作业 2）"""
    by_date = {w.date: w for w in forecasts}
    rows = []
    for date in trip_dates(request):
        if date in by_date:
            rows.append(by_date[date].model_dump() | {"source": "amap_forecast"})  # | 合并两个字典
        else:
            rows.append({"date": date, "day_weather": "暂无预报", "day_temp": "未知", "night_temp": "未知", "source": "unavailable"})
    return rows

class PlannerContextBuilder:
    def __init__(self, amap: AmapClient):
        self.amap = amap
        
    # ---------- 每个工具一个方法，返回 {字段名: 数据} ----------
    def _collect_attractions(self, request: TripRequest) -> dict[str, Any]:
        keywords = request.preferences[:3] or ["景点"]
        pois: dict[str, POIInfo] = {}
        for keyword in keywords:
            for poi in self.amap.search_poi(keyword, request.city, limit=10):
                pois.setdefault(poi.id, poi)  # setdefault：key 不存在才写入，实现按 id 去重并保留先出现的
        
        rows = []
        for poi in pois.values():
            price, source = ticket_price_hint(poi, request)  # 查表要知道城市和出发日期，所以多传一个 request
            rows.append(poi_row(poi, ticket_price_hint=price, price_source=source))
        return {"attraction_pois": rows}
    
    def _collect_hotels(self, request: TripRequest) -> dict[str, Any]:
        rows = []
        for poi in self.amap.search_poi(request.accommodation, request.city, limit=8):
            price, source = hotel_cost_hint(poi, request)
            rows.append(poi_row(poi, estimated_cost_hint=price, price_source=source))
        return {"hotel_pois": rows}
    
    def _collect_foods(self, request: TripRequest) -> dict[str, Any]:
        rows = []
        for poi in self.amap.search_poi("美食", request.city, limit=20):
            price, source = meal_cost_hint(poi)
            rows.append(poi_row(poi, meal_cost_hint=price, price_source=source))
        return {"food_pois": rows}
    
    def _collect_weather(self, request: TripRequest) -> dict[str, Any]:
        return {"trip_weather": align_weather(request, self.amap.get_weather(request.city))}

    # ---------- 并行执行所有工具 ----------
    def collect(self, request: TripRequest) -> dict[str, Any]:
        jobs: dict[str, Callable[[TripRequest], dict[str, Any]]] = {
            "attractions": self._collect_attractions,
            "hotels": self._collect_hotels,
            "foods": self._collect_foods,
            "weather": self._collect_weather,
        }
        
        # 先放好空值：即使某个工具失败，context 的结构也是完整的（下游代码不用判断 key 是否存在）
        snapshot: dict[str, Any] = {
            "attraction_pois": [],
            "hotel_pois": [],
            "food_pois": [],
            "trip_weather": align_weather(request, []),
        }
        
        tool_status: dict[str, dict[str, Any]] = {}
        # 线程池：4 个工具同时调高德，总耗时 ≈ 最慢的那一个，而不是 4 个相加
        # 相当于 JS 的 Promise.allSettled（每个任务单独成功或失败，互不影响）
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(func, request) for name, func in jobs.items()}
            for name, future in futures.items():
                try:
                    result = future.result()  # 等待这个任务完成；任务里抛出的异常会在这里重新抛出
                    snapshot.update(result)
                    count = len(next(iter(result.values())))  # 取出结果里唯一的那个列表，统计条数
                    tool_status[name] = {"ok": True, "count": count}
                except Exception as exc:
                    # 关键：失败被明确记录下来，而不是像多 Agent 版那样变成一段自然语言
                    tool_status[name] = {"ok": False, "error": str(exc)}
                    print(f"⚠️  工具 {name} 失败: {exc}")
        
        party = request.party
        return {
            "version": "planner_context_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "request": request.model_dump(mode="json"),  # mode="json"：date 等类型转成字符串，保证能 json.dumps
            "party": {"total": party.total, "rooms": math.ceil(party.total / 2)},
            "tool_snapshot": snapshot,
            "tool_status": tool_status,
            "planner_constraints": {
                "expected_dates": trip_dates(request),
                "attractions_per_day": "1-3",
                "meals_per_day": ["breakfast", "lunch", "dinner"],
                "pricing_rule": (
                    "ticket_price 必须等于所选景点的 ticket_price_hint；"
                    "hotel.estimated_cost 必须等于 estimated_cost_hint；"
                    "lunch/dinner 的 estimated_cost 必须等于所选餐厅的 meal_cost_hint"
                ),
            },
        }
        
def compact_for_prompt(context: dict[str, Any]) -> dict[str, Any]:
    """给大模型看的版本：去掉 generated_at、tool_status、price_source 等只给人和代码看的字段"""
    snapshot = context["tool_snapshot"]
    
    def strip(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in row.items() if k != "price_source"} for row in rows]
    
    return {
    "request": context["request"],
    "party": context["party"],
    "tool_snapshot": {
        "attraction_pois": strip(snapshot["attraction_pois"]),
        "hotel_pois": strip(snapshot["hotel_pois"]),
        "food_pois": strip(snapshot["food_pois"]),
        "trip_weather": [{k: v for k, v in w.items() if k != "source"} for w in snapshot["trip_weather"]],
    },
    "planner_constraints": context["planner_constraints"],
}