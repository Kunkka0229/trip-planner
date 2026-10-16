"""LangGraph 版规划器（P1）：对外接口和 ContextTripPlanner 完全一样，路由层不需要任何修改"""

from langchain_core.language_models import BaseChatModel

from app.agents.generation import PlanResult
from app.agents.progress import ProgressFn, no_progress
from app.graph.state import PlannerDeps, TripState
from app.graph.trip_graph import trip_graph
from app.models.schemas import TripRequest
from app.services.amap_client import AmapClient
from app.observability.tracing import langchain_callbacks

def progress_of(node: str, update: dict) -> tuple[str, int] | None:
    """节点执行完 → 要上报的进度。返回 None 表示这一步不上报"""
    if node == "collect":
        return "数据收集完成，正在生成行程", 40
    if node == "generate":
        attempt = update["attempts"]
        return f"第 {attempt} 次生成完成，正在校验", 50 + 10 * attempt
    if node == "validate" and "plan" in update:
        return "校验通过", 90
    if node == "fallback":
        return "智能规划失败，使用基础行程", 90
    return None

class GraphTripPlanner:
    def __init__(self, amap: AmapClient, llm: BaseChatModel):
        self.deps = PlannerDeps(amap=amap, llm=llm)
        
    def plan(self, request: TripRequest, on_progress: ProgressFn = no_progress) -> PlanResult:
        on_progress("正在查询景点、酒店、餐厅和天气", 10)
        final: TripState = {}
        # stream_mode 传列表时，每次产出 (模式, 数据)：
        #   "updates"：某个节点刚执行完，数据是 {节点名: 这个节点返回的更新}，用来上报进度
        #   "values"：每一步执行完后的完整 state，最后一次就是最终结果
        
        # config：LangChain 体系里通用的"调用配置"，回调、标签、递归上限都放这里
        #   callbacks：P2 接入 Langfuse，图的每一步都会被记录下来；没配置时是空列表
        #   run_name：这一层在 trace 里显示的名字
        #   recursion_limit：P1 讲过的兜底，防止图里的环失控
        config = {"callbacks": langchain_callbacks(), "run_name": "trip_graph", "recursion_limit": 20}
        stream = trip_graph.stream(
            {"request": request}, config=config, context=self.deps, stream_mode=["updates", "values"]
        )
        for mode, chunk in stream:
            if mode == "values":
                final = chunk
                continue
            for node, update in chunk.items():
                if progress := progress_of(node, update or {}):
                    on_progress(*progress)
        
        on_progress("完成", 100)
        return PlanResult(
            plan=final["plan"],
            source=final["source"],
            attempts=final.get("attempts", 0),
            errors=final.get("errors", []),
            warnings=final.get("warnings", []),
        )
    