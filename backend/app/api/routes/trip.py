"""旅行规划相关接口（P4：全部改成 async def）"""

import asyncio
import json
import logging
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from app.agents.deps import get_planner_deps
from app.agents.graph_planner import EmitFn, no_emit, plan_with_graph
from app.agents.output import recompute_budget
from app.config import get_settings
from app.graph.state import PlannerDeps
from app.models.schemas import FeedbackRequest, TripPlanResponse, TripRequest
from app.observability.tracing import score_trace, trace_span
from app.services.mock_planner import build_mock_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trip", tags=["旅行规划"])

# P2：用户标识。现在还没有登录系统，前端生成一个匿名 ID 存在 localStorage 里，每次请求放在请求头上
# Annotated[str, Header(alias=...)]：从请求头 X-User-Id 读取，alias 指定请求头的名字
UserId = Annotated[str, Header(alias="X-User-Id", max_length=64)]

# 多久没有事件就发一次心跳（秒）
HEARTBEAT_SECONDS = 15

# 最终地址：/api/trip/health
# 和根路径的 /health 不同：这里检查的是"旅行规划这个业务"依赖的配置是否齐全
@router.get("/health", summary="旅行规划健康检查")
def trip_health():
    settings = get_settings()
    return {
        "status": "healthy",
        "service": "trip-planner",
        # bool("") → False，bool("xxx") → True
        # 只返回"有没有配置"，绝对不要把密钥本身返回给前端
        "amap_configured": bool(settings.amap_api_key),
        "llm_configured": bool(settings.llm_api_key),
        "tracing_configured": bool(settings.langfuse_public_key),
    }


async def run_planner(
    request: TripRequest, deps: PlannerDeps | None, user_id: str = "anonymous", emit: EmitFn = no_emit
) -> TripPlanResponse:
    """普通接口和流式接口共用的核心逻辑"""
    if deps is None:
        # 依赖注入返回 None，说明大模型没配置好
        # 退回 mock 数据，方便没有 Key 时也能开发前端
        plan = build_mock_plan(request)
        plan.budget = recompute_budget(plan, request)  # 第 9 课：mock 数据也按统一口径计算预算（酒店要乘房间数）
        emit({"type": "progress", "stage": "完成", "percent": 100})
        return TripPlanResponse(success=True, message="未配置大模型，返回 mock 数据", data=plan)

    # P2：整个规划过程包在一个根 span 里。tags 用来在 Langfuse 里按规划模式筛选、对比
    with trace_span("trip_plan", user_id=user_id, tags=["graph"], input=request.model_dump(mode="json")) as span:
        result = await plan_with_graph(request, deps, emit)
        if span is not None:
            # 输出里只放"摘要"，完整行程在图节点的 span 里已经有了，没必要再存一份
            span.update(
                output={"source": result.source, "attempts": result.attempts,
                        "errors": result.errors, "warnings": result.warnings},
                # 兜底方案标成 WARNING：在 Langfuse 里按 level 一筛就能找出所有失败的请求
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
    return TripPlanResponse(success=True, message=message, data=result.plan, trace_id=trace_id)


# P4：async def。等待高德、等待大模型的时候，事件循环可以去处理其他请求，一个进程就能同时服务很多用户
# 前提是里面的所有 IO 都是异步的（await），一旦混进同步阻塞调用（time.sleep、requests.get、同步 httpx），
# 整个服务都会被卡住，这就是第 11 课 Part 3 说的"参考仓库的 async 坑"
@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
async def plan_trip(
    request: TripRequest,
    # 依赖注入：没配大模型时拿到的是 None
    deps: PlannerDeps | None = Depends(get_planner_deps),
    user_id: UserId = "anonymous",
):
    return run_planner(request, deps, user_id)

def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

# ---------- 第 11 课：SSE 流式接口，推送真实进度 ----------
# SSE（Server-Sent Events）：服务端通过一个一直不关闭的 HTTP 响应持续推送消息，格式是
#   data: {...JSON...}\n\n
# 比 WebSocket 简单：单向推送（服务端 → 浏览器）、基于普通 HTTP、经过代理也没问题
@router.post("/plan/stream", summary="生成旅行计划（SSE 推送进度和逐天草稿")
async def plan_trip_stream(
    request: TripRequest,
    deps: PlannerDeps | None = Depends(get_planner_deps),
    user_id: UserId = "anonymous",
):
    # 和第 11 课一样是"生产者 + 队列 + 消费者"，只是把线程换成了 asyncio 任务：
    #   生产者：worker 任务运行规划图，把事件放进队列
    #   消费者：stream 生成器从队列里取事件，发给浏览器
    # asyncio.Queue 不是线程安全的，但这里两边都在同一个事件循环里，不需要线程安全
    events: asyncio.Queue[dict | None] = asyncio.Queue()

    async def worker() -> None:
        try:
            response = await run_planner(request, deps, user_id, emit=events.put_nowait)
            # mode="json"：保证所有字段都能被 json.dumps 序列化
            events.put_nowait({"type": "result", "data": response.model_dump(mode="json")})
        except Exception as exc:  # 高德 Key 错误等异常，也要通知前端，否则前端会一直等下去
            logger.exception("规划失败")
            events.put_nowait({"type": "error", "message": str(exc)})
        finally:
            events.put_nowait(None)  # None 表示"结束了"

    # create_task：把 worker 放到事件循环里"后台"运行，立刻返回一个 Task 对象（相当于不 await 的 Promise）
    # create_task 会自动复制当前的 ContextVar，request_id 不会丢（对比 P2：自己开线程时要手动 copy_context）
    task = asyncio.create_task(worker())

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    # 最多等 HEARTBEAT_SECONDS 秒；超时说明这段时间没有事件（大模型在慢慢生成）
                    event = await asyncio.wait_for(events.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    # 心跳：以冒号开头的行是 SSE 注释，浏览器会忽略它
                    # 作用是让连接上一直有数据流过，避免被 Nginx、负载均衡器当成空闲连接断开（常见默认 60 秒）
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield sse(event)
        finally:
            # 浏览器断开连接时，Starlette 会取消这个生成器，执行到 finally
            # 如果规划还没结束，就取消它：正在进行的大模型请求会被中断，不再继续花钱
            if not task.done():
                task.cancel()
                logger.info("客户端已断开，取消规划任务")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",  # 不要缓存
            "X-Accel-Buffering": "no",  # 部署在 Nginx 后面时，告诉 Nginx 不要缓冲，否则进度会攒到最后一起发
        },
    )

# ---------- P2：用户反馈 ----------
@router.post("/feedback", summary="对生成的行程打分（👍 / 👎）")
def submit_feedback(body: FeedbackRequest):
    # BOOLEAN 类型的分数：1 = 有帮助，0 = 没帮助。在 Langfuse 里可以统计"好评率"随版本的变化
    ok = score_trace(body.trace_id, "user_feedback", 1 if body.helpful else 0, body.comment, data_type="BOOLEAN")
    return {"success": ok, "message": "感谢反馈" if ok else "未启用 Langfuse，反馈未记录"}