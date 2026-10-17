"""LangGraph 版规划器（P1 创建，P4 改成异步 + 事件流）

P4 之后，路由只通过这里调用规划图。旧的规划器（direct / context / multi_agent）不再被路由使用
"""

from typing import Any, Callable

from app.agents.generation import PlanResult
from app.graph.state import PlannerDeps, TripState
from app.graph.trip_graph import trip_graph
from app.models.schemas import TripRequest
from app.observability.tracing import langchain_callbacks

# 事件回调：每产生一个事件（进度、工具结果、一天的草稿……）就调用一次
# 取代第 11 课的 ProgressFn(stage, percent)：事件不止"进度"一种了，统一用字典表示，靠 type 区分
EmitFn = Callable[[dict[str, Any]], None]

def no_emit(event: dict[str, Any]) -> None:
    """默认回调：什么都不做（和第 11 课的 no_progress 一样）"""
    
async def plan_with_graph(request: TripRequest, deps: PlannerDeps, emit: EmitFn = no_emit) -> PlanResult:
    emit({"type": "progress", "stage": "正在查询景点、酒店、餐厅和天气", "percent": 10})
    final: TripState = {}
    config = {"callbacks": langchain_callbacks(), "run_name": "trip_graph", "recursion_limit": 20}

    # astream：stream 的异步版本，用 async for 遍历
    #   "custom"：节点里 get_stream_writer() 发出的事件，原样转发给调用方
    #   "values"：每一步之后的完整 state，最后一次就是最终结果
    # P1 用来推进度的 "updates" 不需要了：节点自己在开始时就能发进度，比"结束后才知道"更及时
    # subgraphs=True：子图（collect）里发出的事件也要。不加的话，子图内部的事件不会出现在外层的流里
    #   加了之后每次产出变成三元组 (namespace, 模式, 数据)，namespace 表示事件来自哪一层：
    #   () 是主图，("collect:<id>",) 是 collect 子图
    async for namespace, mode, chunk in trip_graph.astream(
        {"request": request}, config=config, context=deps, stream_mode=["custom", "values"], subgraphs=True
    ):
        if mode == "custom":
            emit(chunk)
        elif namespace == ():  # 只要主图的 state；子图的 state 里没有 plan
            final = chunk
    # 注意缩进：要等 async for 把图跑完（循环结束）才能返回，写在循环里面会在第一条事件后就提前返回
    emit({"type": "progress", "stage": "完成", "percent": 100})
    return PlanResult(
        plan=final["plan"],
        source=final["source"],
        attempts=final.get("attempts", 0),
        errors=final.get("errors", []),
        warnings=final.get("warnings", []),
    )