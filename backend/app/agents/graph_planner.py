"""LangGraph 版规划器（P1 创建，P4 改成异步 + 事件流，P5 加上会话：生成、修改、撤销）"""

from typing import Any, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from app.agents.generation import PlanResult
from app.graph.state import PlannerDeps, TripState
from app.graph.trip_graph import trip_graph
from app.models.schemas import TripPlan, TripRequest
from app.observability.tracing import langchain_callbacks

# 事件回调：每产生一个事件（进度、工具结果、一天的草稿……）就调用一次
# 取代第 11 课的 ProgressFn(stage, percent)：事件不止"进度"一种了，统一用字典表示，靠 type 区分
EmitFn = Callable[[dict[str, Any]], None]

def no_emit(event: dict[str, Any]) -> None:
    """默认回调：什么都不做（和第 11 课的 no_progress 一样）"""

def thread_config(thread_id: str | None) -> dict[str, Any]:
    """P5：LangGraph 用 config["configurable"]["thread_id"] 区分会话。同一个 thread_id 的调用共享 state"""
    config: dict[str, Any] = {"callbacks": langchain_callbacks(), "run_name": "trip_graph", "recursion_limit": 30}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    return config
    
async def plan_with_graph(
    request: TripRequest,
    deps: PlannerDeps,
    emit: EmitFn = no_emit,
    graph: CompiledStateGraph = trip_graph,
    thread_id: str | None = None,
    user_id: str = "anonymous",    
) -> PlanResult:
    emit({"type": "progress", "stage": "正在查询景点、酒店、餐厅和天气", "percent": 10})
    final: TripState = {}

    # astream：stream 的异步版本，用 async for 遍历
    #   "custom"：节点里 get_stream_writer() 发出的事件，原样转发给调用方
    #   "values"：每一步之后的完整 state，最后一次就是最终结果
    # P1 用来推进度的 "updates" 不需要了：节点自己在开始时就能发进度，比"结束后才知道"更及时
    # subgraphs=True：子图（collect）里发出的事件也要。不加的话，子图内部的事件不会出现在外层的流里
    #   加了之后每次产出变成三元组 (namespace, 模式, 数据)，namespace 表示事件来自哪一层：
    #   () 是主图，("collect:<id>",) 是 collect 子图
    async for namespace, mode, chunk in graph.astream(
        {"request": request, "user_id": user_id},
        config=thread_config(thread_id),
        context=deps,
        stream_mode=["custom", "values"],
        subgraphs=True,
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
    
async def chat_with_graph(
    graph: CompiledStateGraph, 
    thread_id: str, 
    message: str, 
    deps: PlannerDeps, 
    emit: EmitFn = no_emit
) -> tuple[str, TripPlan]:
    """P5：在已有的会话上发一条消息，返回 (助手的回复, 修改后的行程)

    stream_mode="messages"：模型每生成一小段就产出一次 (消息块, 元数据)，用来做打字机效果
    这次和 P4 不同：修改 Agent 回复的是给人看的自然语言，逐字显示正合适
    """
    config = thread_config(thread_id)
    async for _namespace, mode, chunk in graph.astream(
        {"messages": [HumanMessage(message)]},  # 只传新消息：其他字段从 checkpointer 里读出上一次的 state
        config=config,
        context=deps,
        stream_mode=["messages"],
        subgraphs=True,
    ):
        message_chunk, metadata = chunk
        if isinstance(message_chunk, AIMessageChunk) and message_chunk.text:
            emit({"type": "token", "text": message_chunk.text})
        elif isinstance(message_chunk, ToolMessage):  # 工具执行完的结果，前端可以显示"🔧 已把第 2 天……"
            emit({"type": "tool_result", "name": message_chunk.name, "content": message_chunk.text})
        
    snapshot = await graph.aget_state(config)  # 读出这个会话最新的 state
    messages = snapshot.values["messages"]
    last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    replies = [m for m in messages[last_human + 1:] if isinstance(m, AIMessage) and m.text]
    return (replies[-1].text if replies else ""), snapshot.values["plan"]