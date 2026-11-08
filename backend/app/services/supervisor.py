"""多 Agent 版的修改助手（P10）：主管 + 三个专家

                     ┌─ delegate_edit ──→ 编辑专家（6 个修改工具 + 搜索新地点）
用户 ⇄ 主管（trip_assistant）─┼─ ask_guide ──────→ 攻略专家（知识库 + 天气）
                     ├─ ask_transport ──→ 交通专家（12306 MCP：火车车次和余票）
                     └─ book_hotel（主管自己执行，需要用户审批）

这种结构叫"子 Agent 作为工具"（subagents as tools），是目前最常用的多 Agent 模式：
  - 主管只看到专家的结论，看不到专家内部的十几次工具调用 → 主管的上下文短、干净
  - 每个专家的 prompt 和工具都很少 → 更不容易选错工具
  - 代价：每次委派至少多 2 次模型调用（专家思考 + 专家总结），更慢、更贵；信息在传递中可能丢失
是否值得，用 evals/chat_eval.py 的数据说话
"""

import json

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware, ModelRequest, SummarizationMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware,
    dynamic_prompt,
)
from langchain.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from app.graph.edit_agent import (
    MODIFY_TOOLS, Runtime, TripChatState, book_hotel, describe_booking, edit_prompt, plan_outline,
    search_more_places, search_travel_guide,
)
from app.graph.state import PlannerDeps
from app.memory.memories import format_memories
from app.services.rail_mcp import load_rail_tools
from app.services.trip_mcp import get_trip_mcp_client, load_trip_mcp_tools

# ---------- 专家 ----------

GUIDE_PROMPT = """你是旅行攻略专家，只回答问题，不修改行程。
先用 search_travel_guide 查知识库（如果有天气工具，天气问题用它查），根据查到的内容回答，用 [1] [2] 标注出处。
查不到就如实说不确定。回答控制在 100 字以内。城市：{city}"""

TRANSPORT_PROMPT = """你是城际火车专家，数据来自 12306 的实时余票。步骤：
1. 日期是"明天""下周五"这类相对说法时，先用 get-current-date 拿到今天的日期再推算；
2. 用 get-station-code-of-citys 查出发城市和到达城市的车站代码（两个城市用 | 隔开，一次查完）；
3. 用 get-tickets 查询：高铁加 trainFilterFlags="G"，动车加 "D"；limitedNum 最多 10；
   任务里说了时间段（"上午""晚上"）时，用 earliestStartTime / latestStartTime 过滤；
4. 挑出 2~3 个最合适的车次，说明理由（出发时间、历时、二等座价格和余票）。
任务里没有出发城市或日期时，直接回答"缺少出发城市"或"缺少日期"，不要猜。
12306 只能查今天起大约 15 天内的票，日期太远时如实说明。不支持航班。"""

def build_editor_expert(llm: BaseChatModel, use_search: bool):
    """编辑专家：P5 的修改 Agent 去掉了预订、交通、攻略问答，只负责改行程"""
    
    tools: list[BaseTool] = [*MODIFY_TOOLS, *([search_more_places] if use_search else [])]
    return create_agent(
        llm, tools=tools, 
        state_schema=TripChatState, 
        context_schema=PlannerDeps, 
        name="editor_expert",
        middleware=[
            edit_prompt,  # 和单 Agent 版一样的动态 prompt（带当前行程）
            ToolRetryMiddleware(
                tools=["search_more_places"], 
                max_retries=2, 
                initial_delay=0.5
            ),
            ToolCallLimitMiddleware(tool_name="search_more_places", run_limit=3),
        ],
    )
    
def last_text(result: dict) -> str:
    return result["messages"][-1].text

@tool
async def delegate_edit(task: str, runtime: Runtime) -> Command:
    """把"修改行程"的任务交给编辑专家（换景点、删景点、加景点、换餐厅、改概述）。
    task 要写清楚改哪一天、改成什么样，例如"第 2 天减少到 2 个景点，保留评分高的"。"""

    deps = runtime.context
    expert = build_editor_expert(deps.llm, use_search=get_trip_mcp_client() is not None)
    state = runtime.state
    # 上下文隔离：专家只看到这一个任务，看不到主管和用户之间的完整对话
    result = await expert.ainvoke({
        "messages": [HumanMessage(task)],
        "plan": state["plan"], 
        "context": state["context"], 
        "request": state["request"],
        "memories": state.get("memories", []),
    })
    # 专家改过的行程和候选，写回主管的 state；主管只拿到专家的一句总结
    return Command(
        update={
            "plan": result["plan"],
            "context": result["context"],
            "messages": [ToolMessage(last_text(result), tool_call_id=runtime.tool_call_id)],
        }
    )
    
@tool
async def ask_guide(question: str, runtime: Runtime) -> str:
    """向攻略专家提问：开放时间、闭馆、预约、门票、路线、避坑、带娃带老人的建议、天气等。"""
    deps = runtime.context
    tools: list[BaseTool] = [search_travel_guide] if deps.kb is not None else []
    tools += await load_trip_mcp_tools({"get_weather"})
    expert = create_agent(
        deps.llm, 
        tools=tools, 
        state_schema=TripChatState, 
        context_schema=PlannerDeps, 
        name="guide_expert",
        system_prompt=GUIDE_PROMPT.format(city=runtime.state["request"].city),
    )
    result = await expert.ainvoke({"messages": [HumanMessage(question)], "request": runtime.state["request"]})
    return last_text(result)

@tool
async def ask_transport(task: str, runtime: Runtime) -> str:
    """向交通专家查询城际火车（高铁、动车、普速）的实时车次和余票，数据来自 12306。
    task 里写清楚出发城市、到达城市、日期，例如"上海到北京，2026-11-06，高铁，上午出发"。"""
    tools = await load_rail_tools()
    if not tools:  # 12306 MCP 没配置或连不上：降级，让主管如实告诉用户
        return "火车查询服务暂时不可用，请用户到 12306 App 查询"
    expert = create_agent(
        runtime.context.llm, 
        tools=tools, 
        system_prompt=TRANSPORT_PROMPT, 
        name="transport_expert",
        middleware=[
            ToolRetryMiddleware(tools=["get-tickets"], max_retries=2, initial_delay=0.5),
            ToolCallLimitMiddleware(tool_name="get-tickets", run_limit=3),  # 防止反复换条件查个没完
        ],
    )
    result = await expert.ainvoke({"messages": [HumanMessage(task)]})
    return last_text(result)

# ---------- 主管 ----------

SUPERVISOR_PROMPT = """你是旅行助手的主管，负责和用户对话，把具体工作交给专家：
- 修改行程（换、删、加景点或餐厅）→ delegate_edit，一次委派一件事，task 写清楚
- 开放时间、预约、门票、注意事项、天气等问题 → ask_guide
- 高铁、火车 → ask_transport（用户没说出发城市时先问用户）；航班暂不支持，如实告诉用户
- 预订酒店 → 你自己调用 book_hotel（会请用户确认）
- 闲聊、感谢、关于当前行程的简单问题 → 直接回答，不要委派

专家返回后，用一两句话把结果告诉用户；专家给出的 [1] [2] 引用标记要保留。

当前行程（day 从 1 开始）：
{plan}{memories}"""

@dynamic_prompt
def supervisor_prompt(request: ModelRequest) -> str:
    state = request.state
    return SUPERVISOR_PROMPT.format(plan=plan_outline(state["plan"]), memories=format_memories(state.get("memories", [])))

def build_supervisor(llm: BaseChatModel):
    return create_agent(
        llm,
        tools=[delegate_edit, ask_guide, ask_transport, book_hotel],
        state_schema=TripChatState,
        context_schema=PlannerDeps,
        middleware=[
            supervisor_prompt,
            HumanInTheLoopMiddleware(
                interrupt_on={"book_hotel": {"allowed_decisions": ["approve", "reject"], "description": describe_booking}}
            ),
            SummarizationMiddleware(model=llm, trigger=("tokens", 6000), keep=("messages", 20)),
        ],
        name="trip_assistant",
    )

