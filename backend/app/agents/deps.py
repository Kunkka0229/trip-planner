"""给路由用的依赖（P4 重写）

P4 之前：根据 PLANNER_MODE 返回不同的规划器对象
P4 之后：只有 LangGraph 一种规划方式，路由需要的是"图的运行时依赖"（高德客户端 + 大模型）
"""
from fastapi import Request

from app.graph.state import PlannerDeps
from app.services.amap_client import get_amap_client
from app.services.llm_service import LLMNotConfiguredError, get_llm

def get_planner_deps(request: Request) -> PlannerDeps | None:
    """大模型没配置时返回 None，路由会退回 mock 数据，方便没有 Key 时也能开发前端

    request：FastAPI 会自动注入当前请求，用来读取启动时放在 app.state 上的资源（P9：知识库）
    """
    
    try:
        llm = get_llm()
    except LLMNotConfiguredError:
        return None
    return PlannerDeps(amap=get_amap_client(), llm=llm, kb=request.app.state.kb)