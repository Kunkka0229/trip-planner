"""旅行规划 Agent：查高德 → 调大模型 → 校验 → 失败重试 → 最终兜底"""

import json
from dataclasses import dataclass, field
from datetime import timedelta

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.generation import PlanResult, generate_plan_with_retries
from app.agents.output import parse_and_check, recompute_budget, trip_dates
from app.agents.progress import ProgressFn, no_progress
from app.agents.prompts import PLANNER_SYSTEM_PROMPT, build_planner_prompt
from app.models.schemas import POIInfo, TripPlan, TripRequest
from app.services.amap_client import AmapClient
from app.services.mock_planner import build_mock_plan

 # 最多调用大模型几次
MAX_ATTEMPTS = 3
# 大模型输出的随机性：temperature 越低输出越稳定；结构化输出一般用 0.2~0.5
BASE_TEMPERATURE = 0.3

@dataclass
class PlanResult:
    plan: TripPlan
    source: str  # "llm" = 大模型生成；"fallback" = 兜底方案
    attempts: int  # 一共调用了几次大模型
    errors: list[str] = field(default_factory=list)  # 每次失败的原因（用于日志和排查）

def compact_poi(poi: POIInfo) -> dict:
    """只保留大模型规划时需要的字段：字段越少，prompt 越短，速度越快、费用越低"""
    return {
        "name": poi.name,
        "address": poi.address,
        "district": poi.district,
        "location": poi.location.model_dump(),
        "rating": poi.rating,
        "cost": poi.cost,
    }

class TripPlanner:
    # 类型写 BaseChatModel（抽象）而不是 ChatOpenAI（具体实现），类似 TS 里参数类型写 interface 而不是 class：
    # 只要是 LangChain 的聊天模型都能传进来，第 11 课测试时就传一个假模型
    def __init__(self, llm: BaseChatModel, amap: AmapClient):
        self.llm = llm
        self.amap = amap
    
    def collect_candidates(self, request: TripRequest) -> dict[str, list[dict]]:
        """第一步：调高德，收集景点、酒店、餐厅候选"""

        # 偏好作为景点搜索关键词；没选偏好就搜"景点"
        keywords = request.preferences or ["景点"]
        # 例如：高德景点搜索接口返回的 JSON 里，pois 是一个数组，里面每一项都是一个 POIInfo
        attractions: list[POIInfo] = []
        for keyword in keywords[:3]: # 最多取 3 个偏好，避免调用次数太多
            # 例如：高德景点搜索接口返回的 JSON 里，pois 是一个数组，里面每一项都是一个 POIInfo
            attractions += self.amap.search_poi(keyword, request.city, limit=8)
        
        hotels = self.amap.search_poi(request.accommodation, request.city, limit=8)
        foods = self.amap.search_poi("美食", request.city, limit=15)

        # 不同关键词可能搜到同一个景点，用 dict 按 id 去重（后出现的覆盖先出现的）
        unique_attractions = list({poi.id: poi for poi in attractions}.values())

        return {
            "attractions": [compact_poi(poi) for poi in unique_attractions],
            "hotels": [compact_poi(poi) for poi in hotels],
            "foods": [compact_poi(poi) for poi in foods],
        }
    
    def plan(self, request: TripRequest, on_progress: ProgressFn = no_progress) -> PlanResult:
        on_progress("正在查询景点、酒店、餐厅和天气", 10)
        candidates = self.collect_candidates(request)
        weather = [w.model_dump() for w in self.amap.get_weather(request.city)]

        # ensure_ascii=False：中文按原样输出，否则会变成 北京，既浪费 token 又难读
        prompt = build_planner_prompt(
            request_json=request.model_dump_json(),
            candidates_json=json.dumps(candidates, ensure_ascii=False),
            weather_json=json.dumps(weather, ensure_ascii=False),
            dates=trip_dates(request),
        )
        
        # 重试循环挪到了 generation.py，这里只剩一行
        # 第 11 课：把进度回调透传下去，生成阶段的进度由 generate_plan_with_retries 上报
        result = generate_plan_with_retries(self.llm, prompt, request, on_progress=on_progress)
        on_progress("完成", 100)
        return result
