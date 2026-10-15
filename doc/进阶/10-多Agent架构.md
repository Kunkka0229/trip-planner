# P10：多 Agent 架构模式

> **需求卡片**
> 产品要给助手加"城际交通查询"。技术评审会上有人提议："助手的工具已经 10 多个了，模型经常选错，不如拆成多个 Agent：一个管修改、一个管问答、一个管交通，再加一个主管调度。"
> 另一位同事反对："第 8 课我们就做过多 Agent，结果又慢又贵，第 9 课改回单 Agent 才好起来。"
> 本迭代：**① 实现多 Agent 版本（主管 + 三个专家）；② 用评测数据对比单 Agent 和多 Agent；③ 写一份架构决策记录（ADR），给出有数据支撑的结论。**

**目标**：学会做架构决策，而不只是学会写多 Agent。学完你会掌握：三种主流的多 Agent 模式、"子 Agent 作为工具"的实现（上下文隔离、状态回传）、用 `lc_agent_name` 区分不同 Agent 的输出、对话场景的评测方法、ADR 的写法。

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
                       │                          工具：search_transport
                       └─ book_hotel（主管自己执行，审批中间件挂在主管上）
```

**设计要点**

1. **上下文隔离**：专家收到的只有主管写的一句任务（"把第 1 天晚餐换成国家博物馆"），看不到用户和主管之间的全部对话。专家的上下文短，不容易被无关信息干扰。代价是：如果主管写的任务漏了信息（比如没写"用户不吃辣"），专家就不知道。
2. **结论回传**：主管只拿到专家的一句总结，看不到专家内部调了几次工具、试错了几次。主管的上下文因此保持干净。
3. **状态回传**：编辑专家改过的 `plan` 和 `context` 通过 `Command(update=...)` 写回主管的 state，再写回主图。
4. **审批放在哪一层**：`book_hotel` 由主管直接执行，审批中间件挂在主管上。如果放进专家里，中断要穿过"主图 → 主管 → 工具 → 专家"四层，能工作，但排查问题时要在四层之间来回找。**有副作用的操作，尽量放在离用户最近的那一层**。

## 本课新增 / 修改的文件

```
app/
├── services/transport.py     ← 新增：城际交通（模拟数据）
├── graph/
│   ├── edit_agent.py         ← 修改：search_transport 工具；工具分组
│   ├── supervisor.py         ← 新增：主管 + 三个专家
│   └── trip_graph.py         ← 修改：按配置选择单 Agent / 多 Agent
├── agents/graph_planner.py   ← 修改：只把"和用户对话的 Agent"的文字推给前端
└── config.py                 ← 修改：assistant_mode
evals/chat_dataset.jsonl, evals/chat_eval.py   ← 新增：对话评测
```

## Step 1：交通查询（模拟数据）`app/services/transport.py`

```python
"""城际交通查询（P10，模拟数据）

真实项目里会对接 12306、航旅平台的接口。这里用"城市名 + 日期"生成固定的假数据：
同样的查询永远得到同样的结果（方便测试和评测），并且在结果里明确标注是模拟数据
"""

import hashlib
from dataclasses import dataclass


@dataclass
class TransportOption:
    kind: str  # 高铁 / 航班
    number: str
    depart: str
    arrive: str
    duration_minutes: int
    price: int


def _seed(*parts: str) -> int:
    return int(hashlib.md5("|".join(parts).encode()).hexdigest(), 16)


def search_trains(origin: str, destination: str, date: str) -> list[TransportOption]:
    seed = _seed("train", origin, destination, date)
    duration = 120 + seed % 300  # 2~7 小时
    options = []
    for i, hour in enumerate((7, 9, 13, 16)):
        minute = (seed >> (i * 4)) % 6 * 10
        arrive_total = hour * 60 + minute + duration
        options.append(TransportOption(
            kind="高铁", number=f"G{100 + (seed >> (i * 8)) % 900}",
            depart=f"{hour:02d}:{minute:02d}", arrive=f"{arrive_total // 60 % 24:02d}:{arrive_total % 60:02d}",
            duration_minutes=duration, price=150 + duration // 2,
        ))
    return options


def search_flights(origin: str, destination: str, date: str) -> list[TransportOption]:
    seed = _seed("flight", origin, destination, date)
    duration = 100 + seed % 120
    options = []
    for i, hour in enumerate((8, 12, 19)):
        minute = (seed >> (i * 4)) % 4 * 15
        arrive_total = hour * 60 + minute + duration
        options.append(TransportOption(
            kind="航班", number=f"MU{5000 + (seed >> (i * 8)) % 999}",
            depart=f"{hour:02d}:{minute:02d}", arrive=f"{arrive_total // 60 % 24:02d}:{arrive_total % 60:02d}",
            duration_minutes=duration, price=400 + (seed >> (i * 6)) % 800,
        ))
    return options
```

## Step 2：工具分组 `app/graph/edit_agent.py`

在 `EDIT_TOOLS` 的位置，加上交通工具，并把工具分成组（导入 `from app.services import transport`）：

```python
@tool
def search_transport(origin: str, destination: str, date: str, kind: Literal["高铁", "航班"] = "高铁") -> str:
    """查询城际交通（P10）：origin 出发城市，destination 到达城市，date 日期 YYYY-MM-DD，kind 高铁或航班。
    用户没说从哪里出发时，先问用户，不要猜。"""
    search = transport.search_trains if kind == "高铁" else transport.search_flights
    options = search(origin, destination, date)
    lines = [f"{o.number} {o.depart}→{o.arrive}（{o.duration_minutes // 60}小时{o.duration_minutes % 60}分）{o.price} 元" for o in options]
    return f"{date} {origin}→{destination} 的{kind}（模拟数据，仅供参考）：\n" + "\n".join(lines)


# ---------- 工具分组（P10）：单 Agent 用全部工具；多 Agent 时，每个专家只拿自己那一组 ----------
MODIFY_TOOLS = [list_candidates, replace_attraction, remove_attraction, add_attraction, replace_meal, update_day_description]
EDIT_TOOLS = [*MODIFY_TOOLS, book_hotel, search_transport]
```

单 Agent 版也加上了交通工具，这样两个版本的**能力完全相同**，评测对比才公平。

## Step 3：主管和专家 `app/graph/supervisor.py`

```python
"""多 Agent 版的修改助手（P10）：主管 + 三个专家

                     ┌─ delegate_edit ──→ 编辑专家（6 个修改工具 + 搜索新地点）
用户 ⇄ 主管（trip_assistant）─┼─ ask_guide ──────→ 攻略专家（知识库 + 天气）
                     ├─ ask_transport ──→ 交通专家（高铁 / 航班）
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
    search_more_places, search_transport, search_travel_guide,
)
from app.graph.state import PlannerDeps
from app.memory.memories import format_memories
from app.services.trip_mcp import get_trip_mcp_client, load_trip_mcp_tools

# ---------- 专家 ----------

GUIDE_PROMPT = """你是旅行攻略专家，只回答问题，不修改行程。
先用 search_travel_guide 查知识库（如果有天气工具，天气问题用它查），根据查到的内容回答，用 [1] [2] 标注出处。
查不到就如实说不确定。回答控制在 100 字以内。城市：{city}"""

TRANSPORT_PROMPT = """你是城际交通专家。用 search_transport 查询高铁或航班，挑出 2~3 个最合适的班次，说明理由（时间、价格）。
任务里没有出发城市或日期时，直接回答"缺少出发城市"或"缺少日期"，不要猜。"""


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
    """向交通专家查询城际高铁、航班。task 里写清楚出发城市、到达城市、日期，例如"上海到北京，2026-11-06，高铁"。"""
    expert = create_agent(
        runtime.context.llm, tools=[search_transport], system_prompt=TRANSPORT_PROMPT, name="transport_expert",
    )
    result = await expert.ainvoke({"messages": [HumanMessage(task)]})
    return last_text(result)


# ---------- 主管 ----------

SUPERVISOR_PROMPT = """你是旅行助手的主管，负责和用户对话，把具体工作交给专家：
- 修改行程（换、删、加景点或餐厅）→ delegate_edit，一次委派一件事，task 写清楚
- 开放时间、预约、门票、注意事项、天气等问题 → ask_guide
- 高铁、航班 → ask_transport（用户没说出发城市时先问用户）
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
3. **专家的 prompt 都很短**，只讲自己那一件事。对比单 Agent 版本的 EDIT_SYSTEM_PROMPT，已经有 10 条规则了。

## Step 4：按配置切换 `app/config.py`、`app/graph/trip_graph.py`

```python
    # ---------- 多 Agent（P10）----------
    # single = 一个修改助手拿着全部工具（P5~P9）；supervisor = 主管 + 三个专家
    assistant_mode: Literal["single", "supervisor"] = "single"
```

`assistant` 节点（导入 `from app.config import get_settings` 和 `from app.graph.supervisor import build_supervisor`）：

```python
    if get_settings().assistant_mode == "supervisor":  # P10：多 Agent 版
        agent = build_supervisor(runtime.context.llm)
    else:
        mcp_tools = await load_trip_mcp_tools(MCP_TOOL_NAMES)
        agent = build_edit_agent(runtime.context.llm, mcp_tools, use_kb=runtime.context.kb is not None)
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
{"id": "train", "message": "我从上海出发，帮我查一下第一天去北京的高铁", "expect_any": ["search_transport"]}
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

## Step 7：写 ADR（架构决策记录）

ADR（Architecture Decision Record）是公司里记录"为什么这样设计"的标准文档。半年后有人问"为什么不用多 Agent"，翻 ADR 就有答案，不用靠人记忆。在 `doc/进阶/` 下新建 `ADR-001-修改助手的Agent架构.md`，模板如下：

```markdown
# ADR-001：修改助手采用单 Agent 还是多 Agent

- 状态：已采纳 / 已否决
- 日期：2026-xx-xx

## 背景
修改助手的工具增加到了 11 个，有同事提议拆成主管 + 专家。

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
| 子任务需要完全不同的上下文 | 比如代码审查专家需要读大量代码，不应该塞进主对话 |
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

## 我的笔记

<!-- 在这里记录你的评测结果和 ADR 结论 -->
