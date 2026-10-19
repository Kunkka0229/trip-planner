"""图的"状态"和"依赖"：整张图里所有节点共享的数据结构"""

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.models.schemas import TripPlan, TripRequest
from app.services.amap_client import AmapClient
from app.services.booking import BookingService, get_booking_service

class TripState(TypedDict, total=False):
    request: TripRequest  # 输入：用户的旅行需求
    context: dict[str, Any]  # collect 子图产出的 PlannerContext
    draft: str  # 大模型最近一次的原始输出（还没校验）
    attempts: int  # 已经调用了几次大模型
    # Annotated[类型, reducer]：这个字段的更新方式不是"覆盖"，而是用 operator.add 把新旧值相加
    # 节点返回 {"errors": ["第 2 次的错误"]}，state 里的 errors 会变成 [旧错误..., "第 2 次的错误"]
    # 前端类比：Redux reducer 里的 [...state.errors, ...action.payload]
    errors: Annotated[list[str], operator.add]
    warnings: list[str]
    plan: TripPlan  # 校验通过（或兜底）的最终行程
    source: Literal["llm", "fallback"]
    # ---------- P5：多轮对话 ----------
    # add_messages：LangGraph 自带的消息 reducer。新消息追加到末尾；id 相同的消息会被替换（而不是重复追加）
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str  # 这个会话属于谁，防止别人拿着 thread_id 偷看或修改
    budget_confirmed: bool  # P6：用户已经就"超预算"做过选择了，不要再问第二次
    
@dataclass
class PlannerDeps:
    """运行时依赖：每次调用图时传进来，节点通过 runtime.context 读取

    为什么不放进 state？state 是"数据"，会被 checkpointer 序列化保存（P5），
    而高德客户端、大模型客户端是"工具"，既不能也不应该被保存
    前端类比：React 的 Context / Vue 的 provide-inject，组件（节点）不用层层传参就能拿到
    """

    amap: AmapClient
    llm: BaseChatModel
    # P6：预订服务（模拟）。field(default_factory=...)：不传时调用这个函数创建默认值（dataclass 的写法）
    booking: BookingService = field(default_factory=get_booking_service)
    # P6：有没有人在线回答问题。评测、批处理时设成 False，遇到需要确认的情况按默认方式处理，不中断
    interactive: bool = True

