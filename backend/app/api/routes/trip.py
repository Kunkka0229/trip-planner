"""旅行规划相关接口（P4：全部改成 async def；P5：会话、多轮修改、撤销）"""

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from app.agents.deps import get_planner_deps
from app.agents.graph_planner import EmitFn, chat_with_graph, no_emit, plan_with_graph, thread_config
from app.agents.output import recompute_budget
from app.config import get_settings
from app.graph.state import PlannerDeps
from app.models.schemas import (
    ChatMessage, ChatRequest, FeedbackRequest, TripPlanResponse, TripRequest, TripSession,
)
from app.observability.tracing import score_trace, trace_span
from app.services.mock_planner import build_mock_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trip", tags=["旅行规划"])

# P2：用户标识。现在还没有登录系统，前端生成一个匿名 ID 存在 localStorage 里，每次请求放在请求头上
# Annotated[str, Header(alias=...)]：从请求头 X-User-Id 读取，alias 指定请求头的名字
UserId = Annotated[str, Header(alias="X-User-Id", max_length=64)]

# 多久没有事件就发一次心跳（秒）
HEARTBEAT_SECONDS = 15


def get_graph(request: Request) -> CompiledStateGraph:
    """P5：带 checkpointer 的图在启动时编译好，挂在 app.state 上（见 main.py 的 lifespan）"""
    return request.app.state.trip_graph


Graph = Annotated[CompiledStateGraph, Depends(get_graph)]
Deps = Annotated[PlannerDeps | None, Depends(get_planner_deps)]


@router.get("/health", summary="旅行规划健康检查")
def trip_health():
    settings = get_settings()
    return {
        "status": "healthy",
        "service": "trip-planner",
        "amap_configured": bool(settings.amap_api_key),
        "llm_configured": bool(settings.llm_api_key),
        "tracing_configured": bool(settings.langfuse_public_key),
    }


# ---------- SSE 的公共部分（P5 从 P4 的 plan_trip_stream 里抽出来，生成和修改两个接口共用）----------

def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_response(job: Callable[[EmitFn], Awaitable[dict[str, Any]]]) -> StreamingResponse:
    """job：一个异步函数，执行过程中通过 emit 发事件，最后返回"结果事件"

    和 P4 一样：后台任务 + 队列 + 心跳 + 客户端断开时取消
    """
    events: asyncio.Queue[dict | None] = asyncio.Queue()

    async def worker() -> None:
        try:
            events.put_nowait(await job(events.put_nowait))
        except HTTPException as exc:
            events.put_nowait({"type": "error", "message": exc.detail})
        except Exception as exc:
            logger.exception("任务失败")
            events.put_nowait({"type": "error", "message": str(exc)})
        finally:
            events.put_nowait(None)

    task = asyncio.create_task(worker())

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield sse(event)
        finally:
            if not task.done():
                task.cancel()
                logger.info("客户端已断开，取消任务")

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------- 生成行程 ----------

async def run_planner(
    request: TripRequest, deps: PlannerDeps | None, graph: CompiledStateGraph,
    user_id: str = "anonymous", emit: EmitFn = no_emit,
) -> TripPlanResponse:
    """普通接口和流式接口共用的核心逻辑"""
    if deps is None:
        plan = build_mock_plan(request)
        plan.budget = recompute_budget(plan, request)
        emit({"type": "progress", "stage": "完成", "percent": 100})
        return TripPlanResponse(success=True, message="未配置大模型，返回 mock 数据", data=plan)

    # P5：每次生成都开一个新会话。uuid4 是随机生成的，别人猜不到
    thread_id = uuid.uuid4().hex
    emit({"type": "thread", "thread_id": thread_id})
    with trace_span("trip_plan", user_id=user_id, session_id=thread_id, tags=["graph"],
                    input=request.model_dump(mode="json")) as span:
        result = await plan_with_graph(request, deps, emit, graph=graph, thread_id=thread_id, user_id=user_id)
        if span is not None:
            span.update(
                output={"source": result.source, "attempts": result.attempts,
                        "errors": result.errors, "warnings": result.warnings},
                level="WARNING" if result.source == "fallback" else "DEFAULT",
            )
        trace_id = span.trace_id if span is not None else None

    if result.source == "fallback":
        reason = result.errors[-1] if result.errors else "未知原因"
        message = f"智能规划失败，返回基础行程。原因：{reason}"
    else:
        message = f"旅行计划生成成功（第 {result.attempts} 次生成）"
    if result.warnings:
        message += "；注意：" + "；".join(result.warnings[:3])
    return TripPlanResponse(success=True, message=message, data=result.plan, trace_id=trace_id, thread_id=thread_id)


@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
async def plan_trip(request: TripRequest, deps: Deps, graph: Graph, user_id: UserId = "anonymous"):
    return await run_planner(request, deps, graph, user_id)


@router.post("/plan/stream", summary="生成旅行计划（SSE 推送进度和逐天草稿）")
async def plan_trip_stream(request: TripRequest, deps: Deps, graph: Graph, user_id: UserId = "anonymous"):
    async def job(emit: EmitFn) -> dict[str, Any]:
        response = await run_planner(request, deps, graph, user_id, emit)
        return {"type": "result", "data": response.model_dump(mode="json")}

    return sse_response(job)


# ---------- P5：会话 ----------

async def load_session(graph: CompiledStateGraph, thread_id: str, user_id: str) -> StateSnapshot:
    """读出会话的最新 state，并检查它是不是这个用户的

    不存在和"不是你的"都返回 404，而不是 403：不告诉别人"这个 thread_id 存在"，减少信息泄露
    """
    snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot.values.get("plan") or snapshot.values.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return snapshot


def to_chat_messages(messages: list) -> list[ChatMessage]:
    """只返回用户和助手说的话；工具调用、工具结果是内部过程，不展示"""
    result = []
    for m in messages:
        if isinstance(m, HumanMessage):
            result.append(ChatMessage(role="user", content=m.text))
        elif isinstance(m, AIMessage) and m.text and not m.tool_calls:
            result.append(ChatMessage(role="assistant", content=m.text))
    return result


@router.get("/{thread_id}", response_model=TripSession, summary="获取会话（刷新页面后恢复）")
async def get_session(thread_id: str, graph: Graph, user_id: UserId = "anonymous"):
    snapshot = await load_session(graph, thread_id, user_id)
    values = snapshot.values
    return TripSession(thread_id=thread_id, plan=values["plan"], messages=to_chat_messages(values.get("messages", [])))


@router.post("/{thread_id}/chat", summary="用自然语言修改行程（SSE）")
async def chat(thread_id: str, body: ChatRequest, deps: Deps, graph: Graph, user_id: UserId = "anonymous"):
    if deps is None:
        raise HTTPException(status_code=503, detail="未配置大模型，无法修改行程")
    await load_session(graph, thread_id, user_id)  # 先检查权限，不通过直接返回 404，不会开始 SSE

    async def job(emit: EmitFn) -> dict[str, Any]:
        with trace_span("trip_chat", user_id=user_id, session_id=thread_id, input=body.message) as span:
            reply, plan = await chat_with_graph(graph, thread_id, body.message, deps, emit)
            if span is not None:
                span.update(output=reply)
        return {"type": "done", "reply": reply, "plan": plan.model_dump(mode="json")}

    return sse_response(job)


@router.post("/{thread_id}/undo", response_model=TripSession, summary="撤销上一次修改")
async def undo(thread_id: str, graph: Graph, user_id: UserId = "anonymous"):
    """时间旅行（time travel）：checkpointer 保存了每一步的 state，沿着"父 checkpoint"往回找，
    找到行程和现在不一样的、上一轮对话结束时的那个 state，从那里"分叉"出一个新的 checkpoint

    为什么不直接把旧行程写回去？因为对话记录也要一起回到那个时刻，否则模型会看到"我改过了"的对话，
    却面对着没改过的行程，下一轮对话就乱了
    """
    current = await load_session(graph, thread_id, user_id)
    current_plan = current.values["plan"]
    snapshot = current
    while snapshot.parent_config is not None:
        snapshot = await graph.aget_state(snapshot.parent_config)
        plan = snapshot.values.get("plan")
        if plan is None:  # 已经回到生成行程之前了
            break
        # snapshot.next == ()：这一步之后没有要执行的节点了，说明这是某一轮对话"结束时"的状态
        if snapshot.next == () and plan != current_plan:
            # 以那个 checkpoint 为起点写入一条新消息，图的最新状态就变成了"那时的 state + 这条消息"
            # as_node="assistant"：假装是 assistant 节点写的，这样图知道下一步是 END，不会再执行别的节点
            await graph.aupdate_state(
                snapshot.config, {"messages": [AIMessage("已撤销上一次修改。")]}, as_node="assistant"
            )
            new = await graph.aget_state(thread_config(thread_id))
            return TripSession(thread_id=thread_id, plan=new.values["plan"],
                               messages=to_chat_messages(new.values["messages"]))
    raise HTTPException(status_code=400, detail="没有可以撤销的修改")


@router.post("/feedback", summary="对生成的行程打分（👍 / 👎）")
def submit_feedback(body: FeedbackRequest):
    ok = score_trace(body.trace_id, "user_feedback", 1 if body.helpful else 0, body.comment, data_type="BOOLEAN")
    return {"success": ok, "message": "感谢反馈" if ok else "未启用 Langfuse，反馈未记录"}