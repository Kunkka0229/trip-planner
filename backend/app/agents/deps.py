
"""给路由用的依赖：根据配置选择规划器"""

from typing import Protocol

from app.agents.generation import PlanResult
from app.agents.progress import ProgressFn
from app.config import get_settings
from app.models.schemas import TripRequest
from app.services.amap_client import get_amap_client
from app.services.llm_service import LLMNotConfiguredError, get_llm

# Protocol：结构化类型，和 TS 的 interface 一样是"鸭子类型"
# 只要一个类有 plan(request) -> PlanResult 方法，就算是 Planner，不需要显式继承
class Planner(Protocol):
    # 第 11 课：增加进度回调参数。改了这里，所有规划器的 plan 都要跟着改（和改了 TS interface 一样）
    def plan(self, request: TripRequest, on_progress: ProgressFn = ...) -> PlanResult: ...

def get_trip_planner() -> Planner | None:
    """大模型没配置时返回 None，路由会退回 mock 数据，方便没有 Key 时也能开发前端"""
    try:
        llm = get_llm()
    except LLMNotConfiguredError:
        return None

    mode = get_settings().planner_mode
    if mode == "context":
        from app.agents.context_planner import ContextTripPlanner
        return ContextTripPlanner(amap=get_amap_client(), llm=llm)
        
    if mode == "multi_agent":
        # 在函数内部导入：只有选了这个模式才会加载 MCP 相关代码
        from app.agents.multi_agent_planner import MultiAgentTripPlanner
        return MultiAgentTripPlanner(llm=llm)
    
    from app.agents.trip_planner import TripPlanner
    return TripPlanner(amap=get_amap_client(), llm=llm)