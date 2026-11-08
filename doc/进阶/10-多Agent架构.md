# P10：多 Agent 架构模式

> **需求卡片**
> 产品要给助手加"城际火车查询"，数据直接用社区现成的 12306 MCP 服务。技术评审会上有人提议："助手的工具已经 10 多个了，模型经常选错，不如拆成多个 Agent：一个管修改、一个管问答、一个管交通，再加一个主管调度。"
> 另一位同事反对："第 8 课我们就做过多 Agent，结果又慢又贵，第 9 课改回单 Agent 才好起来。"
> 本迭代：**① 实现多 Agent 版本（主管 + 三个专家）；② 用评测数据对比单 Agent 和多 Agent；③ 写一份架构决策记录（ADR），给出有数据支撑的结论。**

**目标**：学会做架构决策，而不只是学会写多 Agent。学完你会掌握：接入第三方 MCP 服务（锁版本、挑工具、降级）、三种主流的多 Agent 模式、"子 Agent 作为工具"的实现（上下文隔离、状态回传）、用 `lc_agent_name` 区分不同 Agent 的输出、对话场景的评测方法、ADR 的写法。

> **没有用真实模型跑对话评测**，所以本课**不给结论**：结论要由你跑完评测之后自己写进 ADR。

## 先回顾：第 8 课到第 9 课发生了什么

| | 第 8 课：多 Agent + MCP | 第 9 课：PlannerContext |
|---|---|---|
| 结构 | 景点 / 天气 / 酒店三个 Agent 各自调工具 → 规划 Agent | 代码并行收集数据 → 一个规划 Agent |
| 问题 | 大模型调用 7 次以上；工具错误被吞掉；数据被总结成自然语言，丢了结构 | 1 次大模型调用；错误有记录；数据保持结构化 |

第 8 课的问题不是"多 Agent 不好"，而是**用错了地方**：收集数据这件事，步骤是确定的（就是查这 4 样东西），根本不需要模型来"决定"调用什么工具。**能用代码写死的流程，就不要交给 Agent**。

那么修改助手呢？用户的请求千变万化（改行程、问问题、查交通、订酒店、闲聊），这里确实需要模型来决定做什么。问题是：**一个 Agent 拿着所有工具，和多个 Agent 各管一摊，哪个更好？**

## 三种主流模式

```
① 工作流（Workflow）              ② 主管 + 子 Agent（本课实现）          ③ 交接（Handoff / Swarm）
   代码决定流程                     主管决定委派给谁                       Agent 之间互相移交控制权

   START → A → B → C → END         用户 ⇄ 主管 ─┬─→ 专家 1（返回结论）     用户 ⇄ 当前 Agent
                                               ├─→ 专家 2                      │ "这个问题交给交通专家"
   （P1 的规划图）                              └─→ 专家 3                      ↓
                                                                        用户 ⇄ 交通专家（直接和用户对话）
```

| | 工作流 | 主管 + 子 Agent | 交接 |
|---|---|---|---|
| 谁决定下一步 | 代码 | 主管模型 | 当前 Agent |
| 谁和用户对话 | — | 始终是主管 | 当前持有控制权的 Agent |
| 上下文 | 共享 state | **子 Agent 只看到任务**，主管只看到结论 | 共享对话历史 |
| 优点 | 可控、可测、便宜 | 每个专家专注、主管上下文干净 | 少一层转述，延迟低 |
| 缺点 | 不灵活 | 每次委派多 2 次以上模型调用；信息在转述中丢失 | 控制流难追踪，容易"踢皮球" |
| 适合 | 步骤确定的任务 | 子任务边界清晰、工具很多 | 客服分流（售前转售后） |

LangChain 1.x 推荐的是 ②：把子 Agent 包装成一个工具。③ 在 LangGraph 里用 `Command(goto="另一个 Agent", graph=Command.PARENT)` 实现，本课不展开，作业 4 让你了解一下。

## 本课的多 Agent 结构

```
                       ┌─ delegate_edit(task) ──→ 编辑专家 editor_expert
                       │                          工具：6 个修改工具 + search_more_places
用户 ⇄ 主管 trip_assistant ─┼─ ask_guide(question) ──→ 攻略专家 guide_expert
                       │                          工具：search_travel_guide + get_weather
                       ├─ ask_transport(task) ──→ 交通专家 transport_expert
                       │                          工具：12306 MCP（查日期、查车站、查余票）
                       └─ book_hotel（主管自己执行，审批中间件挂在主管上）
```

**设计要点**

1. **上下文隔离**：专家收到的只有主管写的一句任务（"把第 1 天晚餐换成国家博物馆"），看不到用户和主管之间的全部对话。专家的上下文短，不容易被无关信息干扰。代价是：如果主管写的任务漏了信息（比如没写"用户不吃辣"），专家就不知道。
2. **结论回传**：主管只拿到专家的一句总结，看不到专家内部调了几次工具、试错了几次。主管的上下文因此保持干净。12306 返回的余票列表动辄几千 token，在多 Agent 版里，这些内容只出现在交通专家的上下文里。
3. **状态回传**：编辑专家改过的 `plan` 和 `context` 通过 `Command(update=...)` 写回主管的 state，再写回主图。
4. **审批放在哪一层**：`book_hotel` 由主管直接执行，审批中间件挂在主管上。如果放进专家里，中断要穿过"主图 → 主管 → 工具 → 专家"四层，能工作，但排查问题时要在四层之间来回找。**有副作用的操作，尽量放在离用户最近的那一层**。

## 本课新增 / 修改的文件

```
app/
├── services/rail_mcp.py      ← 新增：连接第三方 12306 MCP，只取需要的 3 个工具
├── graph/
│   ├── edit_agent.py         ← 修改：工具分组；单 Agent 版也接上 12306 工具
│   ├── supervisor.py         ← 新增：主管 + 三个专家
│   └── trip_graph.py         ← 修改：按配置选择单 Agent / 多 Agent
├── agents/graph_planner.py   ← 修改：只把"和用户对话的 Agent"的文字推给前端
└── config.py                 ← 修改：rail_mcp_url、assistant_mode
evals/chat_dataset.jsonl, evals/chat_eval.py   ← 新增：对话评测
```

## Step 1：接入 12306 MCP `app/services/rail_mcp.py`

P8 我们自己写了一个 MCP 服务给别人用；这次反过来，直接用社区现成的 [12306-mcp](https://modelscope.cn/mcp/servers/@Joooook/12306-mcp)（作者 Joooook，Node.js 实现，[源码](https://github.com/Joooook/12306-mcp)）。它查询的是 12306 官网的**实时**车次和余票，一共提供 8 个工具：

| 工具 | 作用 | 本课用不用 |
|---|---|---|
| `get-current-date` | 今天的日期（上海时区，yyyy-MM-dd） | ✅ 把"明天""下周五"换算成具体日期 |
| `get-station-code-of-citys` | 城市名 → 车站代码，多个城市用 `\|` 隔开 | ✅ |
| `get-tickets` | 查余票：日期、出发站、到达站，可以按车型、出发时间过滤和排序 | ✅ |
| `get-stations-code-in-city` | 一个城市的所有车站 | — |
| `get-station-code-by-names` | 车站名 → 车站代码 | — |
| `get-station-by-telecode` | 车站代码 → 车站详情 | — |
| `get-interline-tickets` | 中转换乘方案 | —（作业 5） |
| `get-train-route-stations` | 某个车次经停的所有车站 | — |

两种接法任选一种，主服务只需要一个地址：

**① 魔搭托管（推荐，不用装 Node）**：登录魔搭，打开上面的页面，按页面上的说明生成你自己的服务地址。魔搭会给出 SSE 和 Streamable HTTP 两种地址，优先选 Streamable HTTP。**这个地址本身就是凭证**，只放在 `.env` 里，不要提交到 git。

**② 本机运行**（需要 Node.js 18 以上）：

```bash
npx -y 12306-mcp@0.3.10 --port 7003   # 带 --port 时用 HTTP 传输；不带就是 stdio
```

> 为什么不像第 8 课那样用 stdio？`langchain-mcp-adapters` 用 stdio 连接时，**每次调用工具都会启动一个新的子进程**，而 12306-mcp 每次启动都要先从 12306 官网下载全国车站表，一次查询要多等好几秒。HTTP 方式是常驻进程，启动一次就够了。

`.env` 加上：

```bash
# ---------- 12306 MCP（P10）----------
RAIL_MCP_URL=魔搭生成的地址      # 本机运行时填 http://127.0.0.1:7003/mcp
```

`app/config.py`：

```python
    # ---------- 12306 MCP（P10）----------
    rail_mcp_url: str = ""  # 12306 MCP 服务的地址（魔搭托管或本机运行）；留空 = 不提供火车查询
```

`app/services/rail_mcp.py`：

```python
"""城际火车查询：接入第三方的 12306 MCP 服务（P10）

12306-mcp（https://github.com/Joooook/12306-mcp）是社区写好的 MCP Server，查询 12306 官网的实时余票。
可以用魔搭托管的地址，也可以本机运行：npx -y 12306-mcp@0.3.10 --port 7003

和 P8 自建的 trip-data 不同，这是别人的服务，接入时注意三件事：
  1. 锁版本：第三方 MCP 也是依赖（第 8 课 amap-mcp-server 的教训）
  2. 只挑需要的工具：它有 8 个工具，全部交给模型，模型更容易选错
  3. 连不上就降级：没有火车查询，其他功能照常可用
"""

import logging
from functools import lru_cache

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings

logger = logging.getLogger(__name__)

# 只给模型用的 3 个工具：查今天日期、城市 → 车站代码、查余票
RAIL_TOOL_NAMES = {"get-current-date", "get-station-code-of-citys", "get-tickets"}


@lru_cache
def get_rail_mcp_client() -> MultiServerMCPClient | None:
    url = get_settings().rail_mcp_url
    if not url:
        return None
    return MultiServerMCPClient({
        "rail": {
            # 魔搭给的地址以 /sse 结尾时是 SSE 传输（旧协议），其他按 streamable-http 连接
            "transport": "sse" if url.rstrip("/").endswith("/sse") else "streamable_http",
            "url": url,
            "timeout": 20,  # 查余票要访问 12306 官网，比查我们自己的服务慢
        }
    })


_tools_cache: list[BaseTool] | None = None


async def load_rail_tools() -> list[BaseTool]:
    """读取 12306 MCP 的工具，只保留 RAIL_TOOL_NAMES 里的；服务连不上时返回空列表（降级）"""
    global _tools_cache
    client = get_rail_mcp_client()
    if client is None:
        return []
    if _tools_cache is None:
        try:
            tools = await client.get_tools()
        except Exception as exc:
            logger.warning("12306 MCP 不可用，本次不提供火车查询: %s", type(exc).__name__)
            return []
        _tools_cache = [t for t in tools if t.name in RAIL_TOOL_NAMES]
    return _tools_cache
```

**要点**

1. **用别人的 MCP，和自己写的不一样**：P8 的 trip-data 返回结构化数据、出错时抛 `ToolError`；12306-mcp 返回的是**一大段文字**（车次、时间、各座位的价格和余票），查不到城市时返回的也是一段以 `Error:` 开头的普通文字。所以这里只能把工具**原样交给模型**，让模型自己读结果，没法像 `search_more_places` 那样拿结构化数据写进 state。
2. **工具名带连字符**（`get-tickets`）：OpenAI 兼容接口允许函数名里有 `-`，通义千问、DeepSeek 都能正常调用，不需要改名。
3. **结果很长**：北京到上海一天有几十趟高铁，每趟列出好几种座位，不加过滤，一次 `get-tickets` 就是几千 token。`get-tickets` 有 `trainFilterFlags`（`G` 高铁、`D` 动车……）、`earliestStartTime` / `latestStartTime`（出发时间段）、`limitedNum`（最多返回几条）这几个参数，Step 3 的 prompt 里会让模型用上。
4. **实时数据有两个限制**：日期不能早于今天，并且 12306 只卖大约 15 天内的票。所以"第一天去北京的高铁"能不能查到，取决于行程日期离今天有多远。
5. **第三方工具的返回内容不可信**：它返回的文字会直接进入模型的上下文，P11 会讲怎么过滤。

## Step 2：工具分组 `app/graph/edit_agent.py`

把 `EDIT_TOOLS` 分成组：

```python
# ---------- 工具分组（P10）：单 Agent 用全部工具；多 Agent 时，每个专家只拿自己那一组 ----------
MODIFY_TOOLS = [list_candidates, replace_attraction, remove_attraction, add_attraction, replace_meal, update_day_description]
EDIT_TOOLS = [*MODIFY_TOOLS, book_hotel]
```

`EDIT_SYSTEM_PROMPT` 的规则里加一条：

```
11. 用户问高铁、火车时，用 12306 的工具查（如果有这些工具）：相对日期先用 get-current-date 换算；
    先用 get-station-code-of-citys 查车站代码，再用 get-tickets 查询，高铁用 trainFilterFlags="G"，limitedNum 最多 10。
    用户没说从哪里出发时，先问用户，不要猜。12306 只能查大约 15 天内的票。
```

`build_edit_agent` 多一个参数 `rail_tools`，把 12306 的工具加进去：

```python
def build_edit_agent(llm: BaseChatModel, mcp_tools: list[BaseTool] | None = None, use_kb: bool = False,
                     rail_tools: list[BaseTool] | None = None):
    """...
    rail_tools（P10）：从 12306 MCP 加载的工具；服务不可用时为空，Agent 就不能查火车
    """
    extra = [search_more_places, *mcp_tools] if mcp_tools else []
    if use_kb:
        extra.append(search_travel_guide)
    extra += rail_tools or []
```

`ToolRetryMiddleware` 的 `tools` 里加上 `"get-tickets"`（访问 12306 官网偶尔会超时）。

单 Agent 版也加上了 12306 工具，这样两个版本的**能力完全相同**，评测对比才公平。

## Step 3：主管和专家 `app/graph/supervisor.py`

```python
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
        llm, tools=tools, state_schema=TripChatState, context_schema=PlannerDeps, name="editor_expert",
        middleware=[
            edit_prompt,  # 和单 Agent 版一样的动态 prompt（带当前行程）
            ToolRetryMiddleware(tools=["search_more_places"], max_retries=2, initial_delay=0.5),
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
        "plan": state["plan"], "context": state["context"], "request": state["request"],
        "memories": state.get("memories", []),
    })
    # 专家改过的行程和候选，写回主管的 state；主管只拿到专家的一句总结
    return Command(update={
        "plan": result["plan"],
        "context": result["context"],
        "messages": [ToolMessage(last_text(result), tool_call_id=runtime.tool_call_id)],
    })


@tool
async def ask_guide(question: str, runtime: Runtime) -> str:
    """向攻略专家提问：开放时间、闭馆、预约、门票、路线、避坑、带娃带老人的建议、天气等。"""
    deps = runtime.context
    tools: list[BaseTool] = [search_travel_guide] if deps.kb is not None else []
    tools += await load_trip_mcp_tools({"get_weather"})
    expert = create_agent(
        deps.llm, tools=tools, state_schema=TripChatState, context_schema=PlannerDeps, name="guide_expert",
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
        runtime.context.llm, tools=tools, system_prompt=TRANSPORT_PROMPT, name="transport_expert",
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
```

**要点**

1. **专家每次调用时创建**，用完就丢，没有自己的长期状态。它就是一个"会思考的函数"。
2. **委派工具的描述是给主管看的**，要写清楚"什么时候用"和"task 怎么写"。主管写的 task 质量，直接决定专家干得好不好。这就是多 Agent 系统里最容易出问题的环节：**信息在 Agent 之间传递时丢失**。
3. **专家的 prompt 都很短**，只讲自己那一件事。对比单 Agent 版本的 EDIT_SYSTEM_PROMPT，已经有 11 条规则了。交通专家的 prompt 是最长的，因为 12306 的工具要按"日期 → 车站代码 → 余票"的顺序调用，参数也多。这些规则放在单 Agent 版里，每次对话都要带着；放在专家里，只有查火车时才会用到。
4. **降级放在委派工具里**：12306 不可用时，`ask_transport` 直接返回一句话，主管照常工作，不用为"有没有交通专家"写两套 prompt。

## Step 4：按配置切换 `app/config.py`、`app/graph/trip_graph.py`

```python
    # ---------- 多 Agent（P10）----------
    # single = 一个修改助手拿着全部工具（P5~P9）；supervisor = 主管 + 三个专家
    assistant_mode: Literal["single", "supervisor"] = "single"
```

`assistant` 节点（导入 `from app.config import get_settings`、`from app.graph.supervisor import build_supervisor` 和 `from app.services.rail_mcp import load_rail_tools`）：

```python
    if get_settings().assistant_mode == "supervisor":  # P10：多 Agent 版（交通专家自己加载 12306 工具）
        agent = build_supervisor(runtime.context.llm)
    else:
        mcp_tools = await load_trip_mcp_tools(MCP_TOOL_NAMES)
        agent = build_edit_agent(runtime.context.llm, mcp_tools, use_kb=runtime.context.kb is not None,
                                 rail_tools=await load_rail_tools())
```

**默认值还是 `single`**。在评测数据出来之前，没有理由换掉一个工作正常的方案。

## Step 5：只把主管的话推给用户 `app/agents/graph_planner.py`

问题：`stream_mode="messages"` 会把图里**所有**模型调用的输出都推出来，包括专家给主管的总结。用户会先看到专家的"已把第 1 天晚餐换成国家博物馆，餐饮预算有变化"，紧接着又看到主管的"好的，第 1 天的晚餐已经换好了"，像是说了两遍。

解决：`create_agent(name=...)` 设置的名字会出现在元数据的 `lc_agent_name` 里。只推送"直接和用户对话的 Agent"的文字：

```python
# P10：直接和用户对话的 Agent（create_agent 的 name）。专家们的输出是给主管看的，不推给用户
USER_FACING_AGENTS = {"trip_editor", "trip_assistant"}
```

`drive()` 里处理 `messages` 的部分改成：

```python
            message, metadata = chunk
            agent = metadata.get("lc_agent_name")  # 这条消息来自哪个 Agent（create_agent 的 name）
            if isinstance(message, ToolMessage):
                # P10：带上 agent，前端可以把专家内部的步骤折叠显示
                emit({"type": "tool_result", "name": message.name, "content": message.text, "agent": agent})
            # 只推"直接和用户对话的 Agent"在 model 节点生成的文字：
            #   generate 节点输出的是 JSON（P4 用逐天草稿推送）；专家的输出是给主管看的；摘要中间件的输出是内部的
            elif (isinstance(message, AIMessageChunk) and message.text
                  and metadata.get("langgraph_node") == "model" and agent in USER_FACING_AGENTS):
                emit({"type": "token", "text": message.text})
```

注意条件里同时检查了 `langgraph_node == "model"`：`SummarizationMiddleware` 压缩对话时也会调用模型，它所在的节点不叫 `model`，这样它生成的摘要也不会被推给用户。

## Step 6：对话评测

`evals/chat_dataset.jsonl`：

```json
{"id": "fewer-attractions", "message": "第二天太累了，少安排一个景点", "expect_any": ["remove_attraction"]}
{"id": "swap-dinner", "message": "第一天的晚饭换一家评分高一点的", "expect_any": ["replace_meal"]}
{"id": "add-museum", "message": "第三天再加一个博物馆", "expect_any": ["add_attraction"]}
{"id": "roast-duck", "message": "最后一天晚上想吃烤鸭", "expect_any": ["replace_meal", "search_more_places"]}
{"id": "guide-booking", "message": "故宫需要提前预约吗", "expect_any": ["search_travel_guide"]}
{"id": "guide-monday", "message": "国家博物馆周一开门吗", "expect_any": ["search_travel_guide"]}
{"id": "guide-kids", "message": "带一岁的宝宝，一天去几个景点合适", "expect_any": ["search_travel_guide"]}
{"id": "train-ask-origin", "message": "帮我看看高铁", "expect_none": true}
{"id": "train", "message": "我从上海出发，帮我查一下后天上午去北京的高铁", "expect_any": ["get-tickets"]}
{"id": "thanks", "message": "好的，谢谢", "expect_none": true}
```

`evals/chat_eval.py`：

```python
"""对话评测：比较单 Agent 和多 Agent（主管 + 专家）（P10）

每个用例从同一份"冻结的行程"出发，发一条消息，检查：
  tool_ok：调用了期望的工具（或者期望不调用任何工具时，确实没有调用）
  llm_calls：这一轮一共调用了几次模型（多 Agent 的主要成本）
  tokens、latency

用法：
  uv run python -m evals.chat_eval --mode single
  uv run python -m evals.chat_eval --mode supervisor
第一次运行会先用 P3 冻结的 bj-classic-3d 的 context 生成一份行程，存到 evals/chat_base.json，之后都用它
"""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler, get_usage_metadata_callback
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.config import get_settings
from app.graph.checkpoint import SERDE
from app.graph.state import PlannerDeps
from app.graph.trip_graph import build_trip_graph, trip_graph
from app.knowledge.service import get_knowledge_base
from app.models.schemas import TripPlan, TripRequest
from app.services.llm_service import get_llm
from evals.dataset import load_cases, load_context

EVALS_DIR = Path(__file__).resolve().parent
BASE_PATH = EVALS_DIR / "chat_base.json"
DATASET = EVALS_DIR / "chat_dataset.jsonl"


class Counter(AsyncCallbackHandler):
    """回调：统计模型调用次数和调用过的工具名（包括嵌套在专家里的）"""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.tools: list[str] = []

    async def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.llm_calls += 1

    async def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        self.tools.append(serialized.get("name", ""))


async def make_base(deps: PlannerDeps) -> dict[str, Any]:
    """生成（或读取）所有用例共用的初始行程"""
    if BASE_PATH.exists():
        data = json.loads(BASE_PATH.read_text(encoding="utf-8"))
        return {"request": TripRequest.model_validate(data["request"]), "context": data["context"],
                "plan": TripPlan.model_validate(data["plan"])}
    case = next(c for c in load_cases() if c["id"] == "bj-classic-3d")
    final = await trip_graph.ainvoke({"request": case["request"], "context": load_context(case["id"])}, context=deps)
    data = {"request": case["request"].model_dump(mode="json"), "context": final["context"],
            "plan": final["plan"].model_dump(mode="json")}
    BASE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return await make_base(deps)


async def run_case(case: dict[str, Any], base: dict[str, Any], deps: PlannerDeps) -> dict[str, Any]:
    graph = build_trip_graph(checkpointer=InMemorySaver(serde=SERDE))
    config = {"configurable": {"thread_id": case["id"]}}
    # 把冻结的行程"放进"一个新会话：假装 assistant 节点刚执行完，下一次调用会从 START 进入 assistant
    await graph.aupdate_state(config, {**base, "user_id": "eval", "source": "llm"}, as_node="assistant")

    counter = Counter()
    started = time.perf_counter()
    with get_usage_metadata_callback() as usage:
        await graph.ainvoke({"messages": [HumanMessage(case["message"])]},
                            {**config, "callbacks": [counter, usage], "recursion_limit": 40}, context=deps)
    latency = time.perf_counter() - started

    if case.get("expect_none"):
        tool_ok = not counter.tools
    else:
        tool_ok = any(name in counter.tools for name in case["expect_any"])
    return {
        "id": case["id"], "tool_ok": tool_ok, "tools": counter.tools, "llm_calls": counter.llm_calls,
        "tokens": sum(u["total_tokens"] for u in usage.usage_metadata.values()), "latency_s": round(latency, 2),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cases": len(rows),
        "tool_accuracy": round(sum(r["tool_ok"] for r in rows) / len(rows), 3),
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in rows), 2),
        "avg_tokens": round(statistics.mean(r["tokens"] for r in rows)),
        "latency_p50_s": round(statistics.median(r["latency_s"] for r in rows), 1),
    }


async def run_all(mode: str, deps: PlannerDeps) -> list[dict[str, Any]]:
    get_settings().assistant_mode = mode
    base = await make_base(deps)
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for case in cases:  # 依次执行：对话评测的用例少，并发会让延迟数据失真
        row = await run_case(case, base, deps)
        print(f"{'✅' if row['tool_ok'] else '❌'} {row['id']:<18} 模型 {row['llm_calls']} 次  工具 {row['tools']}", flush=True)
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["single", "supervisor"], required=True)
    args = parser.parse_args()
    deps = PlannerDeps(amap=None, llm=get_llm(), interactive=False, kb=get_knowledge_base())
    rows = asyncio.run(run_all(args.mode, deps))
    print("\n| 指标 | 值 |\n|---|---|")
    for key, value in summarize(rows).items():
        print(f"| {key} | {value} |")


if __name__ == "__main__":
    main()
```

```bash
uv run python -m evals.chat_eval --mode single
uv run python -m evals.chat_eval --mode supervisor
```

要点：

1. **所有用例从同一份冻结的行程出发**（`chat_base.json`），用 `aupdate_state(..., as_node="assistant")` 直接"放进"一个新会话。这和 P3 冻结 context 是同一个思路：控制变量。
2. **用回调统计**：`on_tool_start` 会在**任何一层**的工具开始执行时触发，所以专家内部调用的 `replace_meal` 也能统计到。
3. **`train-ask-origin`（"帮我看看高铁"）期望不调用任何工具**：用户没说从哪出发，正确的做法是先问。这类"不该做什么"的用例和"该做什么"一样重要。
4. **`train` 用例说的是"后天"，不是"第一天"**：冻结行程的日期是固定的，过一段时间就变成过去的日期，12306 查不到。用相对日期，模型要先调 `get-current-date`，顺便也测了"相对日期换算"这一步。跑评测前要先配好 `RAIL_MCP_URL`。
5. **12306 是实时数据，评测结果不能完全复现**：今天查到的车次和余票，明天就变了。所以评测只检查"有没有调用 `get-tickets`"，不检查查到了哪些车次。`tokens` 这一项也会随着当天的车次数量浮动，对比两种模式时，要在同一时间段里跑。

## Step 7：写 ADR（架构决策记录）

ADR（Architecture Decision Record）是公司里记录"为什么这样设计"的标准文档。半年后有人问"为什么不用多 Agent"，翻 ADR 就有答案，不用靠人记忆。在 `doc/进阶/` 下新建 `ADR-001-修改助手的Agent架构.md`，模板如下：

```markdown
# ADR-001：修改助手采用单 Agent 还是多 Agent

- 状态：已采纳 / 已否决
- 日期：2026-xx-xx

## 背景
修改助手的工具增加到了 13 个（接入 12306 MCP 之后），有同事提议拆成主管 + 专家。

## 选项
1. 单 Agent（现状）
2. 主管 + 三个专家（P10 实现）

## 评测数据（evals/chat_eval.py，模型：xxx，10 个用例）
| 指标 | 单 Agent | 多 Agent |
|---|---|---|
| tool_accuracy | | |
| avg_llm_calls | | |
| avg_tokens | | |
| latency_p50_s | | |

## 决定
（选哪个，为什么。用上面的数字说话）

## 后果
（这个决定带来的代价是什么；什么情况下应该重新评估，比如"工具超过 20 个时"）
```

## 什么时候该用多 Agent

综合业界的经验，**先从单 Agent 开始**，出现下面的信号再考虑拆：

| 信号 | 说明 |
|---|---|
| 工具太多，评测显示选错率上升 | 一般在 15~20 个工具以上开始明显 |
| 子任务需要完全不同的上下文 | 比如代码审查专家需要读大量代码，不应该塞进主对话；12306 几千 token 的余票列表也是这样 |
| 子任务可以并行 | 比如同时调研 5 个竞品，各派一个子 Agent |
| 需要不同的模型 | 简单的子任务用便宜的小模型（P12） |
| 需要不同的权限 | 只有某个专家能访问支付接口 |

反过来，**步骤确定的流程用工作流（第 9 课、P1），不要用 Agent**；子任务之间需要频繁共享细节时，拆开反而会因为信息丢失而变差。

## 前端改动（做到这一课时让我直接生成）

`tool_result` 事件多了 `agent` 字段。对话面板里，`agent` 不是 `trip_assistant` / `trip_editor` 的工具结果，折叠在对应的委派步骤下面（"🧑‍💼 编辑专家处理中 → 🔧 已把第 1 天晚餐……"），默认收起，可以展开。

## 📝 课后作业

- [ ] 1. **跑评测，写 ADR**：用真实模型分别跑两种模式，把结果填进 ADR，写出你的决定和理由。这是本课最重要的作业。
- [ ] 2. **加难度**：在 `chat_dataset.jsonl` 里加 5 个复杂用例，比如"第二天轻松一点，晚饭换成烤鸭，再帮我查一下回上海的高铁"（一句话包含三件事）。哪种模式处理得更好？
- [ ] 3. **专家用小模型**：给 `PlannerDeps` 加一个 `expert_llm`（比如 qwen-turbo 一类的小模型），专家用它，主管用大模型。重新跑评测，看成本和准确率怎么变化。P12 会正式讲模型路由。
- [ ] 4. **了解 Handoff**：读一下 LangGraph 文档里关于多 Agent 的 handoffs 部分，用 `Command(goto=..., graph=Command.PARENT)` 写一个最小的例子：售前 Agent 遇到退款问题时，把对话交给售后 Agent。它和本课的"子 Agent 作为工具"相比，用户体验上有什么不同？
- [ ] 5. **中转换乘**：把 12306-mcp 的 `get-interline-tickets` 加进 `RAIL_TOOL_NAMES`，给交通专家的 prompt 加一条"没有直达车时查中转方案"，用一条没有直达高铁的线路试一下。再想一想：这个工具应该只给交通专家，还是单 Agent 版也要加？加了之后单 Agent 版有几个工具？
- [ ] 6. **看清 token 花在哪**：在 Langfuse（P2）里分别打开两种模式下 `train` 用例的 trace，找到 `get-tickets` 返回的内容。单 Agent 版里，这段内容后面跟着几次模型调用？多 Agent 版里，主管的上下文里有没有它？这个差别在 ADR 里应该怎么写？

## 我的笔记

<!-- 在这里记录你的评测结果和 ADR 结论 -->
