"""collect 子图：4 个工具节点并行调高德，再汇总成 PlannerContext

        ┌→ attractions ┐
        ├→ hotels      ┤
START ──┼→ foods       ┼→ assemble → END
        └→ weather     ┘

对比第 9 课的 ThreadPoolExecutor：并行、汇总、错误隔离都由 LangGraph 负责，
每个工具还会在 trace（P2）里单独显示成一个节点，耗时和报错一目了然
"""

import logging
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graph.state import PlannerDeps
from app.models.schemas import TripRequest
from app.planner.context import PlannerContextBuilder, assemble_context, empty_snapshot

logger = logging.getLogger(__name__)  # __name__ 是模块名，例如 app.graph.collect，日志里会显示出来

def merge_dict(old: dict, new: dict) -> dict:
    """reducer：把新字典合并进旧字典。4 个并行节点各自返回一部分，最后拼成一个完整的字典"""
    return {**old, **new}

class CollectState(TypedDict, total=False):
    request: TripRequest
    # 4 个节点在同一步里同时写 snapshot：没有 reducer 的话 LangGraph 会直接报错
    # （InvalidUpdateError：同一步里有多个节点更新了同一个字段，它不知道该听谁的）
    snapshot: Annotated[dict[str, Any], merge_dict]
    tool_status: Annotated[dict[str, Any], merge_dict]
    context: dict[str, Any]
    
class CollectInput(TypedDict):
    request: TripRequest

class CollectOutput(TypedDict):
    context: dict[str, Any]

# 工具名 → PlannerContextBuilder 上的方法
# PlannerContextBuilder.acollect_hotels 是"未绑定方法"，调用时要手动传 self：func(builder, request)
# P4：换成异步版本（acollect_xxx），返回值是协程，要 await
TOOLS: dict[str, Callable[[PlannerContextBuilder, TripRequest], Awaitable[dict[str, Any]]]] = {
    "attractions": PlannerContextBuilder.acollect_attractions,
    "hotels": PlannerContextBuilder.acollect_hotels,
    "foods": PlannerContextBuilder.acollect_foods,
    "weather": PlannerContextBuilder.acollect_weather,
}

def make_tool_node(name: str, func: Callable):
    """工厂函数：为每个工具生成一个节点函数（4 个节点逻辑一样，只是调的方法不同）"""
    
    # runtime: Runtime[PlannerDeps]：LangGraph 看到名为 runtime 的参数，会自动把运行时依赖注入进来
    # （只认参数名；类型注解只是给编辑器补全用的，写成 rt 就注入不进来）
    async def node(state: CollectState, runtime: Runtime[PlannerDeps]) -> dict[str, Any]:
        # get_stream_writer：拿到"自定义事件"的发送函数，发出去的事件会出现在 stream_mode="custom" 里
        # 调用方没订阅 custom 时，writer 什么也不做，所以节点里可以放心地调用
        
        writer = get_stream_writer()
        builder = PlannerContextBuilder(runtime.context.amap)
        try:
            result = await func(builder, state["request"])
        except Exception as exc:
            # 和第 9 课一样：失败只记录在 tool_status 里，不影响其他工具
            logger.warning("工具 %s 失败: %s", name, exc)
            writer({"type": "tool", "name": name, "ok": False})
            return {"tool_status": {name: {"ok": False, "error": str(exc)}}}
        
        count = len(next(iter(result.values())))
        writer({"type": "tool", "name": name, "ok": True, "count": count})  # 前端可以显示"✅ 景点 12 个"
        return {"snapshot": result, "tool_status": {name: {"ok": True, "count": count}}}
    
    return node
        
def assemble(state: CollectState) -> dict[str, Any]:
    """4 个工具都结束后执行（成功或失败都算结束）：组装成完整的 PlannerContext"""
    request = state["request"]
    # empty_snapshot 打底：失败的工具没有写 snapshot，对应字段保持空值
    snapshot = empty_snapshot(request) | state.get("snapshot", {})
    return {"context": assemble_context(request, snapshot, state.get("tool_status", {}))}
    
def build_collect_graph():
    # input_schema / output_schema：对外只暴露 request（入）和 context（出），
    # snapshot、tool_status 是子图内部的"私有状态"，不会泄漏到主图
    builder = StateGraph(
        CollectState,
        context_schema=PlannerDeps,
        input_schema=CollectInput,
        output_schema=CollectOutput,
    )
    for name, func in TOOLS.items():
        builder.add_node(name, make_tool_node(name, func))
        builder.add_edge(START, name)  # START 连出 4 条边 = 4 个节点在同一步并行执行（fan-out）
    
    builder.add_node("assemble", assemble)
    # 第一个参数是列表：等列表里的节点全部执行完，才执行 assemble（fan-in，相当于 Promise.all 之后）
    builder.add_edge(list(TOOLS), "assemble")
    builder.add_edge("assemble", END)
    return builder.compile()