"""旅行规划相关接口"""

import json
import queue
import threading
from typing import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.agents.deps import Planner, get_trip_planner
from app.agents.output import recompute_budget
from app.agents.progress import ProgressFn, no_progress
from app.config import get_settings
from app.models.schemas import TripPlanResponse, TripRequest
from app.services.mock_planner import build_mock_plan
from app.agents.generation import PlanResult

router = APIRouter(prefix="/trip", tags=["旅行规划"])

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
    }
    

def run_planner(request: TripRequest, planner: Planner | None, on_progress: ProgressFn = no_progress) -> TripPlanResponse:
    """普通接口和流式接口共用的核心逻辑（第 11 课从 plan_trip 里抽出来）"""
    if planner is None:
        # 依赖注入返回 None，说明大模型没配置好
        # 退回 mock 数据，方便没有 Key 时也能开发前端
        plan = build_mock_plan(request)
        plan.budget = recompute_budget(plan, request)  # 第 9 课：mock 数据也按统一口径计算预算（酒店要乘房间数）
        on_progress("完成", 100)
        return TripPlanResponse(success=True, message="未配置大模型，返回 mock 数据", data=plan)
    
    result: PlanResult = planner.plan(request, on_progress=on_progress)
    if result.source == "fallback":
        reason = result.errors[-1] if result.errors else "未知原因"
        message = f"智能规划失败，返回基础行程。原因：{reason}"
    else:
        message = f"旅行计划生成成功（第 {result.attempts} 次生成）"
    if result.warnings:
        message += "；注意：" + "；".join(result.warnings[:3])
    return TripPlanResponse(success=True, message=message, data=result.plan)
    

# def 而不是 async def：Agent 内部的高德调用和大模型调用都是同步阻塞的（见第 5 课的表格）
@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
def plan_trip(
    request: TripRequest,
    # 依赖注入：没配大模型时拿到的是 None
    planner: Planner | None = Depends(get_trip_planner),
):
    return run_planner(request, planner)

# ---------- 第 11 课：SSE 流式接口，推送真实进度 ----------
# SSE（Server-Sent Events）：服务端通过一个一直不关闭的 HTTP 响应持续推送消息，格式是
#   data: {...JSON...}\n\n
# 比 WebSocket 简单：单向推送（服务端 → 浏览器）、基于普通 HTTP、经过代理也没问题
@router.post("/plan/stream", summary="生成旅行计划（SSE 推送进度）")
def plan_trip_stream(request: TripRequest, planner: Planner | None = Depends(get_trip_planner)):
    # 线程安全的队列：后台线程往里放事件，响应生成器从里面取出来发给浏览器
    # 相当于一个"管道"，两头各在一个线程里
    events: queue.Queue[dict | None] = queue.Queue()

    def report(stage: str, percent: int) -> None:
        events.put({"type": "progress", "stage": stage, "percent": percent})
    
    def worker() -> None:
        try:
            response = run_planner(request, planner, on_progress=report)
            # mode="json"：保证所有字段都能被 json.dumps 序列化
            events.put({"type": "result", "data": response.model_dump(mode="json")})
        except Exception as exc:  # 高德 Key 错误等异常，也要通知前端，否则前端会一直等下去
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)  # None 表示"结束了"

    # daemon=True：服务关闭时不用等这个线程结束
    threading.Thread(target=worker, daemon=True).start()

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
    
    
# POST /api/trip/plan
# response_model：声明响应结构，有三个作用
#   1. /docs 里会展示完整的响应结构
#   2. 返回前会按模型校验并序列化（返回的数据结构不对，后端直接报错，不会把错误数据发给前端）
#   3. 过滤掉模型里没有声明的多余字段，避免不小心把内部数据泄露给前端
# @router.post("/plan", response_model=TripPlanResponse, summary="生成旅行规划")
# def plan_trip(request: TripRequest):
#     # 参数类型是 Pydantic 模型 → FastAPI 自动从请求体（JSON body）读取并校验
#     # 相当于 Express 的 req.body，但已经校验并转好类型了
#     # 能执行到这里，说明 request 一定是合法的（校验失败时 FastAPI 直接返回 422，不会进入函数）
#     plan = build_mock_plan(request)
#     return TripPlanResponse(success=True, message="旅行计划生成成功（mock）", data=plan)