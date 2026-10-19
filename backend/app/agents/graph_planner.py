"""LangGraph 版规划器（P1 创建，P4 改成异步 + 事件流，P5 加上会话，P6 支持暂停 / 恢复）"""

from typing import Any, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt

from app.agents.generation import PlanResult
from app.graph.state import PlannerDeps
from app.graph.trip_graph import trip_graph
from app.models.schemas import TripRequest
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

def describe_interrupt(item: Interrupt) -> dict[str, Any]:
    """P6：把 LangGraph 的 Interrupt 转成给前端的格式

    两种来源：
    budget_check 节点自己 interrupt 的：value 里有 kind="budget"
    HumanInTheLoopMiddleware 发起的：value 是 {"action_requests": [...], "review_configs": [...]}
    """
    value = item.value
    if isinstance(value, dict) and value.get("kind") == "budget":
        return {"id": item.id, **value}
    action = value["action_requests"][0]
    return {
        "id": item.id, 
        "kind": "approval", 
        "question": action.get("description", ""),
        "action": {
            "name": action["name"], 
            "args": action["args"]
        }
    }

async def drive(
    graph: CompiledStateGraph, 
    graph_input: Any, 
    thread_id: str | None, 
    deps: PlannerDeps, 
    emit: EmitFn
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """运行图（新的输入，或者 Command(resume=...)），把过程中的事件转发给 emit

    返回 (最终 state, 等待用户回答的问题列表)。问题列表不为空，说明图暂停了
    生成、对话、恢复三个场景都走这里（P6 从 plan_with_graph 和 chat_with_graph 里合并出来）
    """
    final: dict[str, Any] = {}
    async for namespace, mode, chunk in graph.astream(
        graph_input,
        config=thread_config(thread_id),
        context=deps,
        stream_mode=["custom", "messages", "values"],
        subgraphs=True,
    ):
        if mode == "custom":
            emit(chunk)
        elif mode == "messages":
            message, metadata = chunk
            if isinstance(message, ToolMessage):
                emit({"type": "tool_result", "name": message.name, "content": message.text})
            # 只推修改 Agent 的 model 节点的文字。generate 节点输出的是 JSON，P4 已经用逐天草稿的方式推送了
            elif isinstance(message, AIMessageChunk) and message.text and metadata.get("langgraph_node") == "model":
                emit({"type": "token", "text": message.text})
        elif namespace == ():
            final = chunk
            
    # 图暂停时，最后一次 values 里会带上 __interrupt__
    pending = [describe_interrupt(item) for item in final.get("__interrupt__", [])]
    for item in pending:
        emit({"type": "interrupt", **item})
    return final, pending

def result_of(final: dict[str, Any], pending: list[dict[str, Any]]) -> PlanResult:
    return PlanResult(
        plan=final["plan"],
        source=final["source"],
        attempts=final.get("attempts", 0),
        errors=final.get("errors", []),
        warnings=final.get("warnings", []),
        pending=pending,
    )
    
async def plan_with_graph(
    request: TripRequest,
    deps: PlannerDeps,
    emit: EmitFn = no_emit,
    graph: CompiledStateGraph = trip_graph,
    thread_id: str | None = None,
    user_id: str = "anonymous",    
) -> PlanResult:
    emit({"type": "progress", "stage": "正在查询景点、酒店、餐厅和天气", "percent": 10})
    final, pending = await drive(graph, {"request": request, "user_id": user_id}, thread_id, deps, emit)

    emit({"type": "progress", "stage": "等待你的确认" if pending else "完成", "percent": 100})
    return result_of(final, pending)

def last_reply(final: dict[str, Any]) -> str:
    replies = [m for m in final.get("messages", []) if isinstance(m, AIMessage) and m.text and not m.tool_calls]
    return replies[-1].text if replies else ""
    
async def chat_with_graph(
    graph: CompiledStateGraph, 
    thread_id: str, 
    message: str, 
    deps: PlannerDeps, 
    emit: EmitFn = no_emit
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """P5：在已有的会话上发一条消息。只传新消息，其他字段从 checkpointer 里读出上一次的 state"""
    return await drive(graph, {"messages": [HumanMessage(message)]}, thread_id, deps, emit)

async def resume_graph(
    graph: CompiledStateGraph, 
    thread_id: str, 
    interrupt_id: str, 
    value: Any, 
    deps: PlannerDeps, 
    emit: EmitFn = no_emit
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """P6：用户回答了问题，从暂停的地方继续
    Command(resume={中断 id: 回答})：指明回答的是哪一个中断（同时有多个中断时不会弄混）
    """
    return await drive(graph, Command(resume={interrupt_id: value}), thread_id, deps, emit)