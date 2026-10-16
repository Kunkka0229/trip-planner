"""旅行规划相关接口"""

import contextvars
import json
import queue
import threading
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from app.agents.deps import Planner, get_trip_planner
from app.agents.generation import PlanResult
from app.agents.output import recompute_budget
from app.agents.progress import ProgressFn, no_progress
from app.config import get_settings
from app.models.schemas import FeedbackRequest, TripPlanResponse, TripRequest
from app.observability.tracing import score_trace, trace_span
from app.services.mock_planner import build_mock_plan

router = APIRouter(prefix="/trip", tags=["旅行规划"])

# P2：用户标识。现在还没有登录系统，前端生成一个匿名 ID 存在 localStorage 里，每次请求放在请求头上
# Annotated[str, Header(alias=...)]：从请求头 X-User-Id 读取，alias 指定请求头的名字
UserId = Annotated[str, Header(alias="X-User-Id", max_length=64)]


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


def run_planner(
    request: TripRequest, planner: Planner | None, user_id: str = "anonymous", on_progress: ProgressFn = no_progress
) -> TripPlanResponse:
    """普通接口和流式接口共用的核心逻辑（第 11 课从 plan_trip 里抽出来）"""
    if planner is None:
        # 依赖注入返回 None，说明大模型没配置好
        # 退回 mock 数据，方便没有 Key 时也能开发前端
        plan = build_mock_plan(request)
        plan.budget = recompute_budget(plan, request)  # 第 9 课：mock 数据也按统一口径计算预算（酒店要乘房间数）
        on_progress("完成", 100)
        return TripPlanResponse(success=True, message="未配置大模型，返回 mock 数据", data=plan)

    # P2：整个规划过程包在一个根 span 里。tags 用来在 Langfuse 里按规划模式筛选、对比
    mode = get_settings().planner_mode
    with trace_span("trip_plan", user_id=user_id, tags=[mode], input=request.model_dump(mode="json")) as span:
        result: PlanResult = planner.plan(request, on_progress=on_progress)
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


# def 而不是 async def：Agent 内部的高德调用和大模型调用都是同步阻塞的（见第 5 课的表格）
@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
def plan_trip(
    request: TripRequest,
    # 依赖注入：没配大模型时拿到的是 None
    planner: Planner | None = Depends(get_trip_planner),
    user_id: UserId = "anonymous",
):
    return run_planner(request, planner, user_id)


# ---------- 第 11 课：SSE 流式接口，推送真实进度 ----------
# SSE（Server-Sent Events）：服务端通过一个一直不关闭的 HTTP 响应持续推送消息，格式是
#   data: {...JSON...}\n\n
# 比 WebSocket 简单：单向推送（服务端 → 浏览器）、基于普通 HTTP、经过代理也没问题
@router.post("/plan/stream", summary="生成旅行计划（SSE 推送进度）")
def plan_trip_stream(
    request: TripRequest,
    planner: Planner | None = Depends(get_trip_planner),
    user_id: UserId = "anonymous",
):
    # 线程安全的队列：后台线程往里放事件，响应生成器从里面取出来发给浏览器
    # 相当于一个"管道"，两头各在一个线程里
    events: queue.Queue[dict | None] = queue.Queue()

    def report(stage: str, percent: int) -> None:
        events.put({"type": "progress", "stage": stage, "percent": percent})

    def worker() -> None:
        try:
            response = run_planner(request, planner, user_id, on_progress=report)
            # mode="json"：保证所有字段都能被 json.dumps 序列化
            events.put({"type": "result", "data": response.model_dump(mode="json")})
        except Exception as exc:  # 高德 Key 错误等异常，也要通知前端，否则前端会一直等下去
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)  # None 表示"结束了"

    # P2：新线程不会继承当前线程的 ContextVar（request_id 会变成默认值 "-"）
    # copy_context() 拍下当前上下文的"快照"，ctx.run(worker) 在这个快照里执行 worker
    # 这样后台线程里的日志、trace_id 都和这次请求对得上
    ctx = contextvars.copy_context()
    # daemon=True：服务关闭时不用等这个线程结束
    threading.Thread(target=ctx.run, args=(worker,), daemon=True).start()

    def stream() -> Iterator[str]:
        # 海象运算符 :=（Python 3.8+）：赋值的同时判断，相当于
        # while (true) { const event = events.get(); if (event === null) break; ... }
        while (event := events.get()) is not None:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

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