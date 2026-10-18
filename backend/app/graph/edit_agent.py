"""行程修改 Agent（P5）：用户说"第二天太累了"，它调用工具只改需要改的地方

为什么用"工具局部修改"，而不是"把整份行程重新生成一遍"？
  1. 快：一次工具调用只输出几十个 token，重新生成要输出几千个
  2. 稳：用户满意的部分（其他几天）逐字不变，不会被模型"顺手"改掉
  3. 可控：每个工具都在代码里校验，新景点必须来自高德候选，价格由代码填，不可能编造

这是 LangChain 1.x 的标准写法：create_agent + 工具 + 中间件
  create_agent 内部就是第 8 课见过的小图：model 节点 ⇄ tools 节点，循环到模型不再调用工具为止
"""

import json
from typing import Any, Callable, Literal, NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelRequest, SummarizationMiddleware, dynamic_prompt
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.graph.state import PlannerDeps
from app.agents.output import PlanValidationError, recompute_budget, validate_plan
from app.models.schemas import Attraction, Location, Meal, TripPlan, TripRequest
from app.planner.grounding import find_candidate

class TripChatState(AgentState):
    """在 create_agent 自带的 state（messages）上，加上行程相关的字段，工具通过 runtime.state 读取"""
    
    request: NotRequired[TripRequest]
    context: NotRequired[dict[str, Any]]
    plan: NotRequired[TripPlan]
    
# ToolRuntime[上下文类型, state 类型]：写全泛型参数。只写 ToolRuntime 时，LangChain 会按"上下文是 None"去序列化，
# 而我们传进来的是 PlannerDeps，Pydantic 会打印一大串 PydanticSerializationUnexpectedValue 警告
Runtime = ToolRuntime[PlannerDeps, TripChatState]

class EditError(Exception):
    """修改不合法。错误信息会作为工具结果返回给模型，让它自己纠正（和第 7 课"把错误反馈给模型"一个思路）"""

CANDIDATE_KEYS = {"attraction": "attraction_pois", "hotel": "hotel_pois", "food": "food_pois"}
# 餐别的显示顺序：前端按列表顺序渲染，换餐后要按它重新排序，否则新午餐会排到晚餐后面
MEAL_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3}

def get_day(plan: TripPlan, day: int):
    """工具参数里的 day 从 1 开始（"第 1 天"），对模型和用户都更自然；内部列表下标从 0 开始"""
    if not 1 <= day <= len(plan.days):
        raise EditError(f"day 必须在 1~{len(plan.days)} 之间")
    return plan.days[day - 1]

def find_row(state: dict, kind: str, name: str) -> dict[str, Any]:
    row = find_candidate(name, state["context"]["tool_snapshot"][CANDIDATE_KEYS[kind]])
    if row is None:
        raise EditError(f"「{name}」不在高德候选里，请先调用 list_candidates 查看可选项，只能从中选择")
    return row

def apply_edit(runtime: Runtime, summary: str, mutate: Callable[[TripPlan], None]) -> Command | str:
    """所有修改类工具的公共流程：复制 → 修改 → 业务校验 → 重算预算 → 写回 state"""
    
    request: TripRequest = runtime.state["request"]
    # 深拷贝一份再改：改到一半失败时，state 里的原行程不受影响（相当于前端的 structuredClone）
    plan = runtime.state["plan"].model_copy(deep=True)
    
    try:
        mutate(plan)
        validate_plan(plan, request)  # 第 7 课的业务校验：每天 1~3 个景点、三餐齐全……
    except (EditError, PlanValidationError) as exc:
        return f"修改失败：{exc}"  # 返回字符串 = 普通的工具结果，模型看到后会调整做法
    
    plan.budget = recompute_budget(plan, request)
    # 返回 Command：工具不只是"返回一段文字"，还可以直接更新 state
    # 注意 messages 里必须有一条对应这次调用的 ToolMessage，否则模型会以为工具没有返回
    return Command(
        update={
            "plan": plan,
            "messages": [ToolMessage(f"{summary}。新的总预算 {plan.budget.total} 元", tool_call_id=runtime.tool_call_id)],
        }
    )

# ---------- 工具 ----------
# @tool：把函数变成工具。函数名 = 工具名，docstring = 工具说明，参数类型 = 参数的 JSON Schema
# 这三样都会发给模型，模型据此决定"调用哪个工具、传什么参数"，所以 docstring 要写给模型看
# runtime: ToolRuntime 参数不会出现在 JSON Schema 里，由 LangChain 自动注入（和 LangGraph 节点的 runtime 一样）
@tool
def list_candidates(kind: Literal["attraction", "hotel", "food"], runtime: Runtime) -> str:
    """列出可以选择的候选地点（来自高德地图）。kind：attraction=景点，hotel=酒店，food=餐厅。
    增加或替换任何地点之前，必须先调用它，只能从返回的候选里选择。"""
    
    rows = runtime.state["context"]["tool_snapshot"][CANDIDATE_KEYS[kind]]
    price_key = {"attraction": "ticket_price_hint", "hotel": "estimated_cost_hint", "food": "meal_cost_hint"}[kind]
    brief = [{"name": r["name"], "district": r["district"], "price": r.get(price_key), "rating": r.get("rating")} for r in rows]
    return json.dumps(brief, ensure_ascii=False)

@tool
def replace_attraction(day: int, old_name: str, new_name: str, runtime: Runtime) -> Command | str:
    """把第 day 天（从 1 开始）的景点 old_name 换成候选里的 new_name。"""
    
    def mutate(plan: TripPlan) -> None:
        target = get_day(plan, day)
        index = next((i for i, a in enumerate(target.attractions) if a.name == old_name), None)
        if index is None:
            raise EditError(f"第 {day} 天没有「{old_name}」，当天的景点是：{[a.name for a in target.attractions]}")
        row = find_row(runtime.state, "attraction", new_name)
        target.attractions[index] = Attraction(
            name=row["name"], 
            address=row["address"], 
            location=Location(**row["location"]),
            ticket_price=row["ticket_price_hint"],
        )
    
    return apply_edit(runtime, f"已把第 {day} 天的「{old_name}」换成「{new_name}」", mutate)

@tool
def remove_attraction(day: int, name: str, runtime: Runtime) -> Command | str:      
    """删除第 day 天（从 1 开始）的景点 name。每天至少要保留 1 个景点。"""
    
    def mutate(plan: TripPlan) -> None:
        target = get_day(plan, day)
        before = len(target.attractions)
        target.attractions = [a for a in target.attractions if a.name != name]
        if len(target.attractions) == before:
            raise EditError(f"第 {day} 天没有「{name}」，当天的景点是：{[a.name for a in target.attractions]}")
        
    return apply_edit(runtime, f"已删除第 {day} 天的「{name}」", mutate)

@tool
def add_attraction(day: int, name: str, runtime: Runtime) -> Command | str:
    """在第 day 天（从 1 开始）的末尾增加候选里的景点 name。每天最多 3 个景点。"""

    def mutate(plan: TripPlan) -> None:
        row = find_row(runtime.state, "attraction", name)
        get_day(plan, day).attractions.append(
            Attraction(
                name=row["name"], 
                address=row["address"], 
                location=Location(**row["location"]),
                ticket_price=row["ticket_price_hint"]
            )
        )
    
    return apply_edit(runtime, f"已在第 {day} 天增加「{name}」", mutate)

@tool
def replace_meal(day: int, meal_type: Literal["lunch", "dinner"], restaurant: str, runtime: Runtime) -> Command | str:
    """把第 day 天（从 1 开始）的午餐（lunch）或晚餐（dinner）换成候选里的餐厅 restaurant。"""
    
    def mutate(plan: TripPlan) -> None:
        row = find_row(runtime.state, "food", restaurant)
        target = get_day(plan, day)
        target.meals = [m for m in target.meals if m.type != meal_type]
        target.meals.append(Meal(type=meal_type, name=row["name"], estimated_cost=row["meal_cost_hint"]))
        # 先删后加，新的一餐在列表末尾；按早 → 午 → 晚排回来（sort 是稳定排序，同餐别的相对顺序不变）
        target.meals.sort(key=lambda m: MEAL_ORDER[m.type])

    return apply_edit(runtime, f"已把第 {day} 天的{'午餐' if meal_type == 'lunch' else '晚餐'}换成「{restaurant}」", mutate)

@tool
def update_day_description(day: int, description: str, runtime: Runtime) -> Command | str:
    """改写第 day 天（从 1 开始）的行程概述。修改了当天的景点或餐厅之后，要调用它让概述和内容保持一致。"""
    
    def mutate(plan: TripPlan) -> None:
        get_day(plan, day).description = description
        
    return apply_edit(runtime, f"已更新第 {day} 天的概述", mutate)

EDIT_TOOLS = [list_candidates, replace_attraction, remove_attraction, add_attraction, replace_meal, update_day_description]

# ---------- 动态系统提示词：每次调用模型前，把"当前的行程"写进去 ----------
EDIT_SYSTEM_PROMPT = """你是旅行行程的修改助手。用户已经有一份行程，你根据用户的要求，用工具做**最小的修改**。

规则：
1. 只改用户要求改的部分，其他内容保持不变。
2. 增加或替换地点前，先用 list_candidates 查看候选，只能从候选里选。
3. 修改完某一天的景点或餐厅后，调用 update_day_description 更新当天的概述。
4. 工具返回"修改失败"时，根据原因调整做法再试，不要放弃也不要编造。
5. 全部改完后，用一两句话告诉用户改了什么、预算有什么变化。
6. 用户只是提问（比如"第二天去哪"）时，直接根据当前行程回答，不要调用修改工具。

当前行程（day 从 1 开始）：
{plan}

用户的原始需求：
{request}"""

def plan_outline(plan: TripPlan) -> str:
    """精简的行程，给模型看：不需要坐标、地址这些细节"""
    
    days = [
        {
            "day": i + 1,
            "date": d.date,
            "description": d.description,
            "attractions": [a.name for a in d.attractions],
            "meals": {m.type: m.name for m in d.meals},
            "hotel": d.hotel.name if d.hotel else None,
        }
        for i, d in enumerate(plan.days)
    ]
    total = plan.budget.total if plan.budget else None
    return json.dumps({"days": days, "budget_total": total}, ensure_ascii=False)

# @dynamic_prompt：一种中间件，在每次调用模型前执行，返回值作为本次调用的 system prompt
# 为什么不在创建 Agent 时写死？因为每调用一次工具，行程就变了，模型下一步要看到最新的行程
@dynamic_prompt
def edit_prompt(request: ModelRequest) -> str:
    state = request.state
    return EDIT_SYSTEM_PROMPT.format(
        plan=plan_outline(state["plan"]),
        request=state["request"].model_dump_json(include={"city", "preferences", "free_text_input", "party", "budget_constraint"}),
    )

def build_edit_agent(llm: BaseChatModel):
    """每次调用时用运行时传入的 llm 创建（create_agent 只是把节点连成图，耗时可以忽略）"""
    
    return create_agent(
        llm,
        tools=EDIT_TOOLS,
        state_schema=TripChatState,
        # 外层图的运行时依赖（PlannerDeps）会自动传进来，这里声明一下类型，否则 Pydantic 会报序列化警告
        context_schema=PlannerDeps,
        middleware=[
            edit_prompt,
            # 对话太长时，把较早的消息压缩成一段摘要，只保留最近 20 条原文
            # 不压缩的话，聊得越久，每次调用的 token 越多，越来越慢、越来越贵，最终超出模型的上下文长度
            SummarizationMiddleware(model=llm, trigger=("tokens", 6000), keep=("messages", 20)),
        ],
        name="trip_editor",
    )