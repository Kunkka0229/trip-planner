"""PlannerContext 版规划器（第 9 课，参考仓库的现行架构）

TripRequest
  → PlannerContextBuilder.collect：并行调高德，附上价格 hint，记录每个工具的成败
  → 关键数据缺失？→ 直接返回兜底方案，不浪费大模型调用
  → compact_for_prompt：精简后交给规划 Agent（只调用 1 次大模型，失败才重试）
  → parse_and_check：结构和业务校验（第 7 课）
  → ground_plan：匹配回真实候选，用真实数据覆盖坐标和价格
  → recompute_budget：按统一口径重算预算
"""
import json

from langchain_core.language_models import BaseChatModel

from app.agents.generation import PlanResult, build_fallback, generate_plan_with_retries
from app.agents.prompts import build_context_prompt
from app.agents.progress import ProgressFn, no_progress
from app.models.schemas import TripPlan, TripRequest, WeatherInfo
from app.planner.context import PlannerContextBuilder, compact_for_prompt
from app.planner.grounding import ground_plan
from app.services.amap_client import AmapClient

class ContextTripPlanner:
    def __init__(self, amap: AmapClient, llm: BaseChatModel):
        self.builder = PlannerContextBuilder(amap)
        self.llm = llm
        
    def plan(self, request: TripRequest, on_progress: ProgressFn = no_progress) -> PlanResult:
        on_progress("正在查询景点、酒店、餐厅和天气", 10)
        context = self.builder.collect(request)
        on_progress("数据收集完成", 40)
        status = context["tool_status"]
        print(f"🧰 工具状态: {status}")
        
        # 景点是行程的核心：没有景点候选，就不让大模型"凭空发挥"
        # 对比第 8 课：那时工具全部失败，大模型照样编出了一份行程
        if not context["tool_snapshot"]["attraction_pois"]:
            reason = f"景点数据获取失败: {status.get('attractions', {}).get('error', '没有搜到景点')}"
            return PlanResult(plan=build_fallback(request, reason), source="fallback", attempts=0, errors=[reason])
        
        prompt_context = compact_for_prompt(context)
        # separators=(",", ":")：去掉 JSON 里多余的空格，又能省一些 token
        context_json = json.dumps(prompt_context, ensure_ascii=False, separators=(",", ":"))
        print(f"📦 PlannerContext: {len(json.dumps(context, ensure_ascii=False))} → 精简后 {len(context_json)} 字符")
        
        def postprocess(plan: TripPlan) -> list[str]:
            """闭包：内部函数可以直接访问外层的 context 变量"""
            stats = ground_plan(plan, context)
            print(f"📍 Grounding: 景点命中率 {stats['attraction_rate']:.0%}，未匹配 {stats['ungrounded']}")

            # 天气完全由代码决定：直接用对齐好的 trip_weather 覆盖，模型漏抄、编造都不影响
            plan.weather_info = [WeatherInfo(**w) for w in context["tool_snapshot"]["trip_weather"]]
            # 工具失败排在前面：比"某家餐厅不在候选里"重要得多
            warnings = [f"{name} 数据获取失败" for name, s in status.items() if not s["ok"]]
            # dict.fromkeys 去重并保持原有顺序（同一家餐厅可能被选了好几次）
            warnings += [f"{name} 不在高德候选中" for name in dict.fromkeys(stats["ungrounded"])]
            return warnings
        
        prompt = build_context_prompt(context_json, request.travel_days)
        result = generate_plan_with_retries(self.llm, prompt, request, postprocess=postprocess, on_progress=on_progress)
        on_progress("完成", 100)
        return result
