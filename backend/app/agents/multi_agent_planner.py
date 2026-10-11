"""多 Agent 版规划器（书 13.3、13.4 的方案），用 LangGraph 编排

4 个 Agent 分工协作，画成一张图：

           ┌─→ 景点 Agent ─┐
  START ───┼─→ 天气 Agent ─┼──→ 规划 Agent ──→ END
           └─→ 酒店 Agent ─┘
  （三个工具 Agent 各自通过 MCP 调用高德，并行执行，再用自然语言汇报结果）

和直连版（trip_planner.py）的根本区别：
  直连版：由代码决定调用哪些接口，拿到结构化数据
  多 Agent 版：由大模型决定调用什么工具、传什么参数，拿到的是大模型整理后的自然语言
"""
import asyncio
import threading
from functools import lru_cache
from typing import TypedDict

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from app.agents.generation import PlanResult, generate_plan_with_retries
from app.agents.output import trip_dates
from app.agents.progress import ProgressFn, no_progress
from app.agents.prompts import (
    ATTRACTION_AGENT_PROMPT,
    HOTEL_AGENT_PROMPT,
    WEATHER_AGENT_PROMPT,
    build_planner_prompt,
)
from app.models.schemas import TripRequest
from app.services.amap_mcp import get_amap_mcp_client

# State：整张图共享的"状态"，每个节点读取它，并返回自己要更新的那几个字段
# 前端类比：Redux/Pinia 的 store。节点返回的 dict 会被合并进 state，类似 setState({...})
# total=False：字段可以缺省，因为一开始只有 request，其他字段由各个节点陆续填进来
class TripState(TypedDict, total=False):
    request: TripRequest
    attractions: str  # 景点 Agent 的汇报
    weather: str  # 天气 Agent 的汇报
    hotels: str  # 酒店 Agent 的汇报
    result: PlanResult  # 规划 Agent 的最终结果
    
@lru_cache  # 整个进程只创建一个
def get_background_loop() -> asyncio.AbstractEventLoop:
    """一个常驻的后台事件循环，专门执行多 Agent 版的异步代码

    为什么不直接用 asyncio.run？asyncio.run 每次都会新建一个事件循环、用完就关掉，
    而 ChatOpenAI 内部的异步 HTTP 连接池是全局共享的，并且绑定在第一次使用它的那个事件循环上。
    第二个请求换了一个新的事件循环再去用它，就会报错：
        RuntimeError: ... is bound to a different event loop
    解决办法：只开一个事件循环，让它一直运行，所有请求都提交到这里执行
    前端类比：一个常驻的 Web Worker，主线程把任务 postMessage 过去，等它返回结果
    """
    # 创建一个事件循环
    loop = asyncio.new_event_loop()
    
    # daemon=True：服务关闭时不用等这个线程结束
    threading.Thread(target=loop.run_forever, name="multi-agent-loop", daemon=True).start()
    return loop
    
class MultiAgentTripPlanner:
    def __init__(self, llm: BaseChatModel):
        self.llm = llm
        
    async def _ask(self, tools: list[BaseTool], system_prompt: str, question: str) -> str:
        """创建一个带工具的 Agent，问一个问题，返回它的最终回答"""
        # create_agent 内部就是一个小 LangGraph：模型节点 ⇄ 工具节点，循环到模型不再调用工具为止
        agent = create_agent(self.llm, tools=tools, system_prompt=system_prompt)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]},
            # recursion_limit：图最多走几步，防止死循环。"调模型 → 执行工具"一轮算 2 步，
            # 6 步 ≈ 最多 2 轮工具调用 + 1 次最终回答，超过就抛出 GraphRecursionError
            # （作用相当于参考仓库 SimpleAgent 的 max_tool_iterations）
            config={"recursion_limit": 6},
        )
        # result["messages"] 是完整的对话记录：user → ai(tool_calls) → tool → ai(最终回答)
        return result["messages"][-1].text
    
    # on_progress：第 11 课的进度回调。planner_node 是这里面定义的闭包，可以直接使用它
    def _build_graph(self, tools: list[BaseTool], on_progress: ProgressFn):
        by_name = {tool.name: tool for tool in tools}
        # 每个 Agent 只给它需要的工具：工具说明会占用 prompt，给 16 个工具既费 token，又容易选错
        search_tools = [by_name["maps_text_search"]]
        weather_tools = [by_name["maps_weather"]]
        
        # ---------- 节点：普通函数，接收 state，返回要更新的字段 ----------
        async def attraction_node(state: TripState) -> TripState:
            request = state["request"]
            preferences = "、".join(request.preferences) or "热门"
            print("🔍 景点 Agent 工作中...")
            
            answer = await self._ask(search_tools, ATTRACTION_AGENT_PROMPT, f"搜索{request.city}的{preferences}景点")
            return {"attractions": answer}
        
        async def weather_node(state: TripState) -> TripState:
            print("🌤️  天气 Agent 工作中...")
            answer = await self._ask(weather_tools, WEATHER_AGENT_PROMPT, f"查询{state['request'].city}的天气")
            return {"weather": answer}
        
        async def hotel_node(state: TripState) -> TripState:
            request = state["request"]
            print("🏨 酒店 Agent 工作中...")
            answer = await self._ask(search_tools, HOTEL_AGENT_PROMPT, f"搜索{request.city}的{request.accommodation}")
            return {"hotels": answer}
        
        # 规划节点是同步函数：LangGraph 会自动把它放到线程池里执行，不会卡住事件循环
        def planner_node(state: TripState) -> TripState:
            request = state["request"]
            # 三个 Agent 的输出都是自然语言，直接拼起来交给规划 Agent
            prompt = build_planner_prompt(
                request_json=request.model_dump_json(),
                candidates_json=(
                    f"【景点】\n{state['attractions']}\n\n【酒店】\n{state['hotels']}\n\n"
                    "【餐饮】无候选数据，请推荐当地有代表性的餐厅"
                ),
                weather_json=state["weather"],
                dates=trip_dates(request),
            )
            print("📋 规划 Agent 工作中...")
            return {"result": generate_plan_with_retries(self.llm, prompt, request, on_progress=on_progress)}
        
        # ---------- 把节点连成图 ----------
        graph = StateGraph(TripState)
        graph.add_node("attraction_agent", attraction_node)
        graph.add_node("weather_agent", weather_node)
        graph.add_node("hotel_agent", hotel_node)
        graph.add_node("planner_agent", planner_node)
        
        # 从 START 连出三条边：三个工具 Agent 并行执行（书 13.3.3 是依次执行的）
        graph.add_edge(START, "attraction_agent")
        graph.add_edge(START, "weather_agent")
        graph.add_edge(START, "hotel_agent")
        
        # 列表形式的边 = "等这几个节点全部完成，再执行下一个"，相当于 Promise.all 之后再继续
        graph.add_edge(["attraction_agent", "weather_agent", "hotel_agent"], "planner_agent")
        graph.add_edge("planner_agent", END)

        # compile：检查图的结构（有没有孤立节点等），生成可以执行的对象
        return graph.compile()
        
    async def _aplan(self, request: TripRequest, on_progress: ProgressFn) -> PlanResult:
        # 向 MCP Server 查询它提供了哪些工具（会启动一次 amap-mcp-server 子进程）
        tools = await get_amap_mcp_client().get_tools()
        on_progress("多个 Agent 正在查询高德", 10)
        graph = self._build_graph(tools, on_progress)
        # 传入初始 state，拿到最终 state
        final_state = await graph.ainvoke({"request": request})
        on_progress("完成", 100)
        return final_state["result"]
    
    def plan(self, request: TripRequest, on_progress: ProgressFn = no_progress) -> PlanResult:
        # MCP 工具只支持异步调用，而我们的路由是同步的 def（FastAPI 会放到线程池里执行）
        # run_coroutine_threadsafe：把协程提交给后台事件循环执行，返回一个 Future
        # future.result()：阻塞当前线程，直到结果出来。相当于在同步代码里"await 一个 Promise"
        future = asyncio.run_coroutine_threadsafe(self._aplan(request, on_progress), get_background_loop())
        return future.result()