# 第 8 课：书中的方案：多 Agent + MCP（LangGraph）

**目标**：按书 13.3、13.4 实现**多 Agent 协作 + MCP 工具调用**，并和第 6、7 课的"直连版"放在一起，通过配置切换。顺便学会 **LangGraph** 的核心用法：用一张"状态图"编排多个 Agent。这一课的重点不在"写出来"，而在于**亲手体会这种架构的问题**，为第 9 课的重构做铺垫。

```
                      ┌─ 景点 Agent ──tool_calls──┐
TripRequest ──────────┼─ 天气 Agent ──tool_calls──┼── MCP ── amap-mcp-server（子进程）── 高德 API
   （LangGraph 并行）  └─ 酒店 Agent ──tool_calls──┘
                              │ 三段自然语言汇报
                              ▼
                         规划 Agent ──→ TripPlan JSON（复用第 7 课的校验和重试）
```

对应：书 13.3（多智能体协作）、13.4（MCP 工具集成），参考仓库 `app/services/amap_service.py`、`app/agents/prompts.py` 里保留的三个工具 Agent 提示词。

> 本课已实测完整链路：真实的 `ChatOpenAI` 连接一个假的 OpenAI 兼容接口（按脚本返回 tool_calls，每次回复前等 1 秒）→ `create_agent` 执行工具调用 → 启动真实的 `amap-mcp-server` 子进程 → 向高德发出真实请求（测试用的是假 Key，所以返回 `INVALID_USER_KEY`）→ 结果回传给 Agent → 规划 Agent 生成行程。三个工具 Agent 确实是并行的：串行执行至少要 7 秒，实测一次请求约 4.5 秒。**测试中还踩到一个异步事件循环的坑**，Step 4 会讲。

## MCP 是什么？为什么书里要用它？

书 13.4.1 的问题是："为什么不直接调 API？"

| | 直接调 API（第 5 课） | MCP |
|---|---|---|
| 谁决定调用什么 | 代码 | **大模型** |
| 接入新能力 | 写新的客户端代码 | 换一个 MCP Server 就行，Agent 代码不用改 |
| 生态 | 每家 API 写法都不一样 | 统一协议：高德、GitHub、文件系统、数据库……都有现成的 Server |
| 适合场景 | 流程固定、追求稳定 | 流程开放、需要模型自主探索 |

前端类比：MCP 就像 **LSP（语言服务协议）**。VSCode 不用为每种语言单独写补全逻辑，只要语言实现了 LSP Server 就能接入。同理，Agent 不用为每个服务单独写调用代码，只要服务实现了 MCP Server 就能接入。

## Step 1：重构，把重试循环抽出来

多 Agent 版和直连版的最后一步完全相同："把 prompt 交给规划 Agent → 校验 → 重试 → 兜底"。所以先把第 7 课 `TripPlanner.plan` 里的循环抽成一个独立函数。

新建 `app/agents/generation.py`：

```python
"""从第 7 课的 TripPlanner.plan 里抽出来的"生成 + 校验 + 重试 + 兜底"循环

为什么要抽出来？第 8 课的多 Agent 版本和第 7 课的直连版本，最后一步都是
"把一个 prompt 交给规划 Agent，拿到合格的 TripPlan"。同样的逻辑不应该写两遍
"""

from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.output import parse_and_check, recompute_budget
from app.agents.prompts import PLANNER_SYSTEM_PROMPT
from app.models.schemas import TripPlan, TripRequest
from app.services.mock_planner import build_mock_plan

MAX_ATTEMPTS = 3
BASE_TEMPERATURE = 0.3


@dataclass
class PlanResult:
    plan: TripPlan
    source: str  # "llm" | "fallback"
    attempts: int
    errors: list[str] = field(default_factory=list)


def generate_plan_with_retries(llm: BaseChatModel, base_prompt: str, request: TripRequest) -> PlanResult:
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = base_prompt
        if errors:
            prompt += f"\n\n注意：上一次的输出不合格，原因是：{errors[-1]}。请修正后重新输出完整 JSON。"
        temperature = BASE_TEMPERATURE + 0.1 * (attempt - 1)
        messages = [SystemMessage(PLANNER_SYSTEM_PROMPT), HumanMessage(prompt)]
        try:
            response = llm.invoke(messages, temperature=temperature).text
            plan = parse_and_check(response, request)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            errors.append(reason)
            print(f"⚠️  第 {attempt}/{MAX_ATTEMPTS} 次失败: {reason}")
            continue
        print(f"✅ 第 {attempt} 次生成成功")
        return PlanResult(plan=plan, source="llm", attempts=attempt, errors=errors)

    print("❌ 大模型多次生成失败，返回兜底方案")
    fallback = build_mock_plan(request)
    fallback.overall_suggestions = "智能规划暂时不可用，以下是基础行程模板，请稍后重试。"
    fallback.budget = recompute_budget(fallback, request)
    return PlanResult(plan=fallback, source="fallback", attempts=MAX_ATTEMPTS, errors=errors)
```

`app/agents/trip_planner.py` 随之变短（整个文件替换）：

```python
"""直连版规划器（第 6、7 课）：代码调高德 → 一个规划 Agent"""

import json

from langchain_core.language_models import BaseChatModel

from app.agents.generation import PlanResult, generate_plan_with_retries
from app.agents.output import trip_dates
from app.agents.prompts import build_planner_prompt
from app.models.schemas import POIInfo, TripRequest
from app.services.amap_client import AmapClient


def compact_poi(poi: POIInfo) -> dict:
    """只保留大模型规划时需要的字段"""
    return {
        "name": poi.name,
        "address": poi.address,
        "district": poi.district,
        "location": poi.location.model_dump(),
        "rating": poi.rating,
        "cost": poi.cost,
    }


class TripPlanner:
    def __init__(self, amap: AmapClient, llm: BaseChatModel):
        self.amap = amap
        self.llm = llm

    def collect_candidates(self, request: TripRequest) -> dict[str, list[dict]]:
        keywords = request.preferences or ["景点"]
        attractions: list[POIInfo] = []
        for keyword in keywords[:3]:
            attractions += self.amap.search_poi(keyword, request.city, limit=8)
        hotels = self.amap.search_poi(request.accommodation, request.city, limit=8)
        foods = self.amap.search_poi("美食", request.city, limit=15)
        unique_attractions = list({poi.id: poi for poi in attractions}.values())
        return {
            "attractions": [compact_poi(p) for p in unique_attractions],
            "hotels": [compact_poi(p) for p in hotels],
            "foods": [compact_poi(p) for p in foods],
        }

    def plan(self, request: TripRequest) -> PlanResult:
        candidates = self.collect_candidates(request)
        weather = [w.model_dump() for w in self.amap.get_weather(request.city)]
        prompt = build_planner_prompt(
            request_json=request.model_dump_json(),
            candidates_json=json.dumps(candidates, ensure_ascii=False),
            weather_json=json.dumps(weather, ensure_ascii=False),
            dates=trip_dates(request),
        )
        # 重试循环挪到了 generation.py，这里只剩一行
        return generate_plan_with_retries(self.llm, prompt, request)
```

> 重构后，第 7 课的功能应该完全不变。先启动服务从前端提交一次，确认没问题再继续。**每次重构完都先验证一遍，再往下加新功能**，这是个好习惯。

## Step 2：安装依赖，接入 MCP `app/services/amap_mcp.py`

```bash
cd /Users/star/Desktop/star/agent/my-helloagents-trip-planner/backend
# langgraph：状态图编排；langchain：create_agent；langchain-mcp-adapters：把 MCP 工具转换成 LangChain 工具
uv add langgraph langchain langchain-mcp-adapters
# 讲义实测版本：langgraph 1.2.14 / langchain 1.4.3 / langchain-mcp-adapters 0.3.2
```

`amap-mcp-server` 通过 `uvx` 启动（`uvx` 是 uv 自带的命令，你已经装好了）。第一次运行时会自动下载，可以先手动验证一下：

```bash
# 能看到它启动后等待输入就说明没问题，按 Ctrl+C 退出
AMAP_MAPS_API_KEY=你的key uvx --with "pydantic<2.14" amap-mcp-server==0.1.11
```

```python
"""高德 MCP 工具：把高德地图的能力以"工具"的形式提供给 Agent 调用

MCP（Model Context Protocol）：一套让大模型调用外部工具的标准协议
  - MCP Server：工具提供方。这里是 amap-mcp-server，一个独立的子进程，内部负责调用高德 API
  - MCP Client：工具使用方。这里是 langchain-mcp-adapters 的 MultiServerMCPClient
  - 两者通过 stdin/stdout 交换 JSON-RPC 消息（stdio 传输）

前端类比：MCP Server 就像一个"插件"，MCP Client 是"插件宿主"，协议规定了插件怎么声明能力、怎么被调用
"""

from functools import lru_cache

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings


@lru_cache  # 共享一个客户端实例（书 13.4.3 "共享 MCP 实例"）。注意：创建客户端本身不会启动子进程
def get_amap_mcp_client() -> MultiServerMCPClient:
    settings = get_settings()
    if not settings.amap_api_key:
        raise RuntimeError("AMAP_API_KEY 未配置")

    # 字典的 key "amap" 是给这个 Server 起的名字；可以同时配置多个 Server（所以叫 MultiServer）
    return MultiServerMCPClient(
        {
            "amap": {
                "transport": "stdio",  # 通过子进程的 stdin/stdout 通信
                # 启动 MCP Server 的命令：uvx 会自动下载并运行 amap-mcp-server 这个 Python 包
                # 类比前端：npx some-cli
                "command": "uvx",
                # --with "pydantic<2.14"：amap-mcp-server 锁死了旧版 mcp==1.8.1，它用到的 pydantic 内部函数
                # 在 pydantic 2.14 里被删掉了，不加这个限制子进程会启动失败（见下方踩坑记录）
                # ==0.1.11：锁定版本，避免它哪天升级又带来新的不兼容
                "args": ["--with", "pydantic<2.14", "amap-mcp-server==0.1.11"],
                # 传给子进程的环境变量：amap-mcp-server 从 AMAP_MAPS_API_KEY 读取 Key
                "env": {"AMAP_MAPS_API_KEY": settings.amap_api_key},
            }
        }
    )
```

> **踩坑记录（2026-10-08）**：不加 `--with "pydantic<2.14"` 时，请求会报 `McpError: Connection closed`，意思是子进程刚启动就退出了。往上翻堆栈，能看到子进程自己的报错：`ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'`。原因是 `amap-mcp-server` 把依赖锁死在 `mcp==1.8.1`，这个旧版 mcp 用了 pydantic 的**内部私有函数**，而 pydantic 2.14.0 删掉了它。`uvx` 每次准备新环境都会装最新版 pydantic，于是"昨天还能用，今天突然坏了"。教训：**第三方 MCP Server 也是依赖，要锁版本**；看到 `Connection closed`，先手动运行一次上面那条 `uvx` 命令，子进程的真实报错会直接打印出来。

拿到工具的方式是 `tools = await client.get_tools()`：客户端会启动子进程，问 Server"你有哪些工具"，再把每个工具包装成一个 LangChain 的 `BaseTool`。实测 `amap-mcp-server` 提供 16 个工具，名字形如 `maps_text_search`、`maps_weather`、`maps_direction_walking_by_address`……

注意 `await`：**MCP 工具只支持异步调用**，用同步的 `tool.invoke(...)` 会直接报错 `StructuredTool does not support sync invocation`。这会影响 Step 4 的写法。

## Step 3：工具 Agent 的提示词

在 `app/agents/prompts.py` 末尾追加：

```python
# ============================================================
# 第 8 课：书中多 Agent 方案的"工具 Agent"提示词
# ============================================================
# 不需要再教模型"工具调用格式"：create_agent 会把工具的名字、说明、参数 JSON Schema
# 通过接口的 tools 字段发给模型，模型用原生 Function Calling 返回结构化的 tool_calls

ATTRACTION_AGENT_PROMPT = """你是景点搜索专家，负责根据城市和用户偏好搜索景点。

你必须调用 maps_text_search 工具搜索，不要自己编造景点信息。
拿到工具结果后，整理出 5-10 个景点，每个景点包含：名称、地址、经纬度（经度,纬度）。"""

WEATHER_AGENT_PROMPT = """你是天气查询专家，负责查询城市天气。

你必须调用 maps_weather 工具查询，不要自己编造天气。
拿到工具结果后，按日期列出：日期、白天天气、白天温度、夜间温度。"""

HOTEL_AGENT_PROMPT = """你是酒店推荐专家，负责搜索合适的酒店。

你必须调用 maps_text_search 工具搜索，不要自己编造酒店信息。
拿到工具结果后，整理出 3-5 家酒店，每家包含：名称、地址、经纬度（经度,纬度）。"""
```

### Agent 是怎么调用工具的？

参考仓库用的 hello-agents `SimpleAgent` 是**基于文本约定**的：在 prompt 里教模型输出 `[TOOL_CALL:工具名:参数=值]`，再用正则表达式去匹配。模型只要少写一个括号就匹配不上。

现在主流大模型都支持**原生 Function Calling**，`create_agent` 用的就是这种方式：

```
① 请求里带上 tools 字段，用 JSON Schema 描述每个工具（工具名、说明、参数），由 LangChain 根据 MCP 工具的信息自动生成
   "tools": [{"type": "function", "function": {"name": "maps_text_search", "parameters": {...}}}]

② 模型决定调用工具时，返回的不是文字，而是结构化的 tool_calls：
   {"role": "assistant", "content": null,
    "tool_calls": [{"id": "call_1", "function": {"name": "maps_text_search",
                                              "arguments": "{\"keywords\": \"历史文化景点\", \"city\": \"北京\"}"}}]}

③ Agent 执行工具，把结果作为一条 role=tool 的消息追加进去，再调用一次模型
   [system, user, assistant(tool_calls), tool(工具结果)] → 模型给出最终回答
```

`create_agent` 返回的其实也是一张 LangGraph 图，只有两个节点，循环执行：

```
  START ──→ model 节点 ──有 tool_calls──→ tools 节点（执行工具）
              ▲    │                         │
              │    └──没有 tool_calls──→ END（最后一条 AIMessage 就是最终回答）
              │                              │
              └──── 工具结果（ToolMessage）───┘
```

前端类比：tool_calls 就像后端返回了一个"请帮我调用某个接口"的结构化指令，而不是一段需要你用正则去解析的 HTML。

## Step 4：多 Agent 规划器 `app/agents/multi_agent_planner.py`

这一步用 LangGraph 把 4 个 Agent 连成一张图。先认识三个概念：

| 概念 | 是什么 | 前端类比 |
|---|---|---|
| **State** | 整张图共享的数据，用 `TypedDict` 声明有哪些字段 | Pinia 的 store |
| **Node（节点）** | 一个函数：接收 state，返回要更新的字段 | 一个 action，返回值会被合并进 store |
| **Edge（边）** | 节点之间的执行顺序。一个节点连出多条边，下游节点就**并行执行** | 路由表 / 流程图的箭头 |

```python
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

    def _build_graph(self, tools: list[BaseTool]):
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
            return {"result": generate_plan_with_retries(self.llm, prompt, request)}

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

    async def _aplan(self, request: TripRequest) -> PlanResult:
        # 向 MCP Server 查询它提供了哪些工具（会启动一次 amap-mcp-server 子进程）
        tools = await get_amap_mcp_client().get_tools()
        graph = self._build_graph(tools)
        final_state = await graph.ainvoke({"request": request})  # 传入初始 state，拿到最终 state
        return final_state["result"]

    def plan(self, request: TripRequest) -> PlanResult:
        # MCP 工具只支持异步调用，而我们的路由是同步的 def（FastAPI 会放到线程池里执行）
        # run_coroutine_threadsafe：把协程提交给后台事件循环执行，返回一个 Future
        # future.result()：阻塞当前线程，直到结果出来。相当于在同步代码里"await 一个 Promise"
        future = asyncio.run_coroutine_threadsafe(self._aplan(request), get_background_loop())
        return future.result()
```

### 踩坑记录：为什么不直接用 `asyncio.run`？

最直觉的写法是在 `plan` 里写 `return asyncio.run(self._aplan(request))`。实测**第一个请求成功，第二个请求开始全部失败**：

```
RuntimeError: <asyncio.locks.Event object at 0x...> is bound to a different event loop
```

原因：`asyncio.run` 每次都会新建一个事件循环，用完就关掉。而 `ChatOpenAI` 内部的异步 HTTP 连接池是**全局缓存**的，并且绑定在第一次使用它的那个事件循环上。第二个请求换了一个新的事件循环再去用它，就报错了。

所以代码里改成了"**一个常驻的后台事件循环**"（`get_background_loop`）：所有请求都把协程提交到同一个事件循环里执行。用这种写法实测连续 2 个请求、并发 2 个请求都能成功。

> 更彻底的做法是把整条链路都改成异步：路由用 `async def`，规划器提供 `async def aplan`。但直连版和 PlannerContext 版的高德调用都是同步的，全部改造的成本比较高，这门课就不展开了。

## Step 5：配置切换

`app/config.py`：

```python
from typing import Literal   # 文件顶部的导入区追加

    # Settings 类里追加：
    # 规划模式：direct = 代码直连高德（第 6、7 课）；multi_agent = 书中的多 Agent + MCP（第 8 课）
    planner_mode: Literal["direct", "multi_agent"] = "direct"
```

`app/agents/deps.py`（整个文件替换）：

```python
"""给路由用的依赖：根据配置选择规划器"""

from typing import Protocol

from app.agents.generation import PlanResult
from app.config import get_settings
from app.models.schemas import TripRequest
from app.services.amap_client import get_amap_client
from app.services.llm_service import LLMNotConfiguredError, get_llm


# Protocol：结构化类型，和 TS 的 interface 一样是"鸭子类型"
# 只要一个类有 plan(request) -> PlanResult 方法，就算是 Planner，不需要显式继承
class Planner(Protocol):
    def plan(self, request: TripRequest) -> PlanResult: ...


def get_trip_planner() -> Planner | None:
    try:
        llm = get_llm()
    except LLMNotConfiguredError:
        return None

    if get_settings().planner_mode == "multi_agent":
        # 在函数内部导入：只有选了这个模式才会加载 MCP 相关代码
        from app.agents.multi_agent_planner import MultiAgentTripPlanner

        return MultiAgentTripPlanner(llm=llm)

    from app.agents.trip_planner import TripPlanner

    return TripPlanner(amap=get_amap_client(), llm=llm)
```

`app/api/routes/trip.py` 的导入和参数类型改成 `Planner`：

```python
from app.agents.deps import Planner, get_trip_planner   # 替换原来的两行导入

def plan_trip(
    request: TripRequest,
    planner: Planner | None = Depends(get_trip_planner),  # TripPlanner → Planner
):
```

## Step 6：对比两种模式

```bash
# .env 里切换
PLANNER_MODE=multi_agent
```

用**同一个请求**，在两种模式下各生成 2~3 次，把结果填进下面的表格（写在笔记里）：

| 对比项 | direct | multi_agent |
|---|---|---|
| 总耗时（秒） | | |
| 大模型调用次数（看日志） | | |
| 景点是不是真实存在（拿高德地图搜一下） | | |
| 坐标准确吗（第 10 课在地图上看得更直观） | | |
| 某个工具失败时，用户能察觉到吗 | | |

### 剧透：你会发现的问题

测试时我用了一个**无效的 Key**，发给模型的工具结果消息是这样的：

```
{"role": "tool", "tool_call_id": "call_1",
 "content": [{"type": "text", "text": "{\n  \"error\": \"Text Search failed: INVALID_USER_KEY\"\n}"}]}
```

`amap-mcp-server` 没有把失败标记成"工具出错"，只是返回了一段写着 error 的普通文字，所以 LangChain 也只能把它当成正常结果交给模型。**三个工具全部调用失败，可整个流程照样"成功"返回了一份行程。** 错误被工具 Agent 用自然语言"总结"之后传给了规划 Agent，规划 Agent 拿不到数据，就**凭记忆编**了一份。代码完全察觉不到异常，前端也没有任何提示。

这就是"让大模型调工具，再用自然语言传递结果"的根本问题：

1. **错误被吞掉**：工具失败了也没有异常，只有一段文字。
2. **结构丢失**：高德返回的是精确的坐标和价格，经过"大模型总结"这一步之后，就变成了可能出错的文字。
3. **慢且贵**：每个工具 Agent 至少调用大模型 2 次，总共 7 次以上，而直连版只有 1 次。
4. **不可控**：模型可能用错工具、传错参数、调用次数忽多忽少。

第 9 课会把这些问题逐一解决，最后得到的就是参考仓库现在的架构。

## 📝 课后作业

- [ ] 1. **完成上面的对比表格**，写进笔记。
- [ ] 2. **串行 vs 并行**：书 13.3.3 里三个专家 Agent 是依次执行的。只改 `_build_graph` 里的边，把图改成 `START → 景点 → 天气 → 酒店 → 规划 → END`，对比两种连法的总耗时。（提示：一个 `add_edge` 只连两个节点；改完后列表形式的那条边就不需要了）
- [ ] 3. **画出你的图**：在 `_aplan` 里加一行 `print(graph.get_graph().draw_mermaid())`，把输出贴到 [Mermaid Live Editor](https://mermaid.live) 里看看。然后**（选做）**加一个"美食 Agent"节点，用 `maps_text_search` 搜索当地美食，让规划 Agent 不再"凭空推荐餐厅"。别忘了在 `TripState` 里加字段。
- [ ] 4. **思考题**：书 13.4.3 讲"共享 MCP 实例"，我们用 `@lru_cache` 共享了 `MultiServerMCPClient`。可是观察终端会发现，每调用一次工具，`amap-mcp-server` 的启动日志就会再出现一次（`Processing request of type ...`）。这说明什么？会带来什么性能问题？（提示：查一下 langchain-mcp-adapters 文档里 `client.session(...)` 的用法）

**提交方式**：作业 1 的表格、作业 2 的耗时对比、作业 3 的 Mermaid 图、作业 4 的答案发给老师。下一课：重构为参考仓库的 PlannerContext 架构。

## 我的笔记

<!-- 在这里记录你的答案和心得 -->
