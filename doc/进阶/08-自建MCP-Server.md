# P8：工具工程与自建 MCP Server

> **需求卡片**
> 公司另一个团队在做"商旅报销 Agent"，需要查景点门票和酒店的参考价，来判断员工报销是否合理。他们问能不能用你们的数据。
> 同时，你们自己的修改助手也有个短板：用户说"晚上想吃烤鸭"，但第一次规划时的候选餐厅里没有烤鸭店，助手只能说"抱歉"。
> 本迭代：**① 把"查高德 + 算价格"封装成一个独立的 MCP 服务，其他团队的 Agent、Claude Code 都能直接用；② 修改助手通过这个服务搜索新地点，搜到的结果加入候选，再用来修改行程。**

**目标**：从"用别人的 MCP"变成"给别人提供 MCP"。学完你会掌握：FastMCP 写 MCP Server（工具、资源、提示模板）、工具设计的原则、结构化输出和错误语义、streamable-http 传输、鉴权、在 LangChain 里以两种方式使用 MCP 工具、工具重试和调用次数限制、外部服务不可用时的降级。

> 本课代码在临时目录里测试过：新增 6 个测试，共 68 个全部通过。测试会**真的启动一个 MCP Server**（随机端口），通过 HTTP 连接它，验证：结构化返回、错误标记 `isError`、资源和提示模板、没有令牌返回 401、修改助手"直接替换失败 → 去 MCP 搜索 → 再替换成功"的完整过程、新候选在下一轮对话里依然可用、MCP 服务挂掉时助手照常工作。调试中发现一个坑：**资源 URI 里的中文会被 URL 编码**（Step 2 要点 5）。**没有接入 Claude Code 实际测试**，Step 6 的命令来自官方文档。

## 回顾：MCP 是什么，为什么要自建

第 8 课你用过 MCP：`amap-mcp-server` 是别人写好的服务，我们的 Agent 作为客户端调用它的工具。当时也踩了它的坑：**工具出错时返回的是普通文本，Agent 根本不知道失败了**。

| | Function Calling | MCP |
|---|---|---|
| 是什么 | 模型的能力：输出"我要调用哪个工具、参数是什么" | 一个协议：规定工具怎么**声明**、怎么**被调用**、怎么**传输** |
| 工具定义在哪 | 你的代码里（`@tool`） | 一个独立的服务里 |
| 谁能用 | 只有你的这个 Agent | 任何支持 MCP 的客户端：别的团队的 Agent、Claude Code、Cursor…… |
| 类比 | 组件内部的方法 | 一个发布到 npm 的包，或者一个 REST API |

两者不是二选一：MCP 工具被加载进 LangChain 之后，最终还是通过 Function Calling 交给模型。**MCP 解决的是"工具怎么在不同的 Agent、不同的团队之间复用"**。公司里的典型做法是：每个数据域（地图、订单、用户、知识库）各有一个 MCP 服务，各个业务 Agent 按需接入。

## 本课的架构

```
                ┌──────────────────────────────────┐
                │ trip-data MCP Server（:7002）     │
                │  tools:     search_places         │──→ 高德 + 价格规则（复用 app/ 里的代码）
                │             get_weather           │
                │  resources: prices://{city}       │
                │  prompts:   plan_trip             │
                └──────────────────────────────────┘
                     ↑ HTTP + Bearer 令牌
       ┌─────────────┼───────────────────────┐
       │             │                       │
  我们的修改助手   Claude Code / Cursor    别的团队的 Agent
  ① get_weather：原样交给模型
  ② search_more_places：我们的工具里调用 MCP，结果写进 state
```

## 本课新增 / 修改的文件

```
mcp_server/                    ← 新增：独立进程，和 app/ 平级
├── __init__.py
└── server.py
app/
├── services/trip_mcp.py       ← 新增：MCP 客户端
├── graph/edit_agent.py        ← 修改：search_more_places 工具，重试和限次中间件
├── graph/trip_graph.py        ← 修改：assistant 加载 MCP 工具，带回 context
├── planner/pricing.py         ← 修改：拆出不依赖 TripRequest 的价格函数
└── config.py                  ← 修改：MCP 相关配置
tests/test_mcp.py              ← 新增
```

`.env` 追加：

```bash
# ---------- 自建 MCP Server（P8）----------
MCP_PORT=7002
MCP_TOKEN=换成一串随机字符     # 生成方法：python -c "import secrets; print(secrets.token_urlsafe(32))"
TRIP_MCP_URL=http://127.0.0.1:7002/mcp
```

`mcp` 这个包在第 8 课装 `langchain-mcp-adapters` 时已经带上了（本课用的是 1.30），不需要额外安装。

## Step 1：价格函数去掉对 TripRequest 的依赖 `app/planner/pricing.py`

MCP 工具的调用方是别的团队，他们没有 `TripRequest`，只知道"城市、月份、酒店档次"。把需要 `TripRequest` 的函数拆成两层：外层保持原样（已有代码不用改），内层只要最基本的参数：

```python
def ticket_season(request: TripRequest) -> str:
    """按出发月份粗分票价季节：11-3 月淡季，7-8 月旺季，其余平季（不考虑节假日）"""
    return season_of(request.start_date.month)


def season_of(month: int) -> str:
    """P8：从 ticket_season 里拆出来，MCP 工具只知道月份，没有 TripRequest"""
    if month in (11, 12, 1, 2, 3):
        return "off_season"
    if month in (7, 8):
        return "peak_season"
    return "normal_season"


def ticket_price_hint(poi: POIInfo, request: TripRequest) -> tuple[int, str]:
    """返回 (成人票价, 价格来源)：先查价格表，查不到再走关键词规则"""
    return ticket_price_for(poi, request.city, request.start_date.month)


def ticket_price_for(poi: POIInfo, city: str, month: int) -> tuple[int, str]:
    """P8：ticket_price_hint 的"不依赖 TripRequest"版本，MCP 工具直接用"""
    # removesuffix：去掉末尾的"市"，"北京市" → "北京"，和表里的城市名对齐
    profile = load_price_table().get((city.removesuffix("市"), poi.name))
    if profile:
        # 例如故宫：{"off_season": 40, "normal_season": 60, "peak_season": 60}
        return int(profile[season_of(month)]), "price_table"
    ...（后面的规则判断不变）


def hotel_cost_hint(poi: POIInfo, request: TripRequest) -> tuple[int, str]:
    """返回 (每间每晚价格, 价格来源)"""
    return hotel_cost_for(poi, request.accommodation)


def hotel_cost_for(poi: POIInfo, accommodation: str) -> tuple[int, str]:
    """P8：hotel_cost_hint 的"不依赖 TripRequest"版本"""
    if poi.cost and poi.cost > 0:
        return round(poi.cost), "amap_cost"
    # dict.get(key, 默认值)：找不到住宿类型时按 400 算
    return HOTEL_PRICE_BY_LEVEL.get(accommodation, 400), "level_default"
```

**这样做的好处**：MCP 服务和 PlannerContext 用的是**同一套**价格逻辑。别的团队查到的故宫门票，和我们行程里的故宫门票，永远是同一个数。这就是"单一事实来源"。

`app/config.py` 加上 MCP 配置：

```python
    # ---------- 自建 MCP Server（P8）----------
    mcp_port: int = 7002  # MCP Server 监听的端口
    mcp_token: str = ""  # 调用 MCP Server 需要的令牌；留空 = 不鉴权（只能在本机开发时这样做）
    trip_mcp_url: str = ""  # 主服务连接 MCP Server 的地址，例如 http://127.0.0.1:7002/mcp；留空 = 不使用
```

## Step 2：MCP Server `mcp_server/server.py`

```bash
mkdir -p mcp_server && touch mcp_server/__init__.py
```

```python
"""trip-data：旅行数据 MCP Server（P8）

把"查高德 + 算价格"这套能力以 MCP 的形式提供出去，公司里其他团队的 Agent（商旅报销、客服……）、
Claude Desktop、Claude Code 都能直接用，不需要复制我们的代码

运行：uv run python -m mcp_server.server
地址：http://127.0.0.1:7002/mcp（streamable-http 传输）

对比第 8 课用的 amap-mcp-server：
  - 工具的错误以 isError 明确返回（第 8 课的坑：错误被当成普通文本，Agent 不知道失败了）
  - 返回结构化数据（structuredContent），并且附带价格参考，和我们 PlannerContext 的口径一致
  - 只读：只开放查询类工具。下单这种写操作需要用户确认（P6），不适合直接开放给外部 Agent
"""

import json
from typing import Annotated, Any, Literal
from urllib.parse import unquote

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.models.schemas import WeatherInfo
from app.planner.pricing import hotel_cost_for, load_price_table, meal_cost_hint, ticket_price_for
from app.services.amap_client import AmapClient, AmapError, get_amap_client

# 测试时替换成假高德（和 FastAPI 的 dependency_overrides 一个思路，只是这里没有依赖注入框架，就用一个模块变量）
get_amap = get_amap_client

mcp = FastMCP(
    "trip-data",
    # instructions：连接时发给客户端，告诉对方的 Agent"这个服务是干什么的"
    instructions="旅行数据服务：查询中国城市的景点、酒店、餐厅、天气和门票价格参考。地点数据来自高德地图。",
    # stateless_http：每个请求独立处理，服务端不保存会话。可以随意部署多个实例做负载均衡
    stateless_http=True,
    # json_response：直接返回 JSON，而不是 SSE 流。我们的工具都是一次性返回结果，不需要流式
    json_response=True,
)


class Location(BaseModel):
    longitude: float
    latitude: float


class Place(BaseModel):
    """工具的返回结构。FastMCP 会根据它生成 outputSchema，客户端能拿到结构化数据"""

    name: str
    district: str
    address: str
    location: Location
    rating: float | None
    price: int = Field(description="价格参考（元）。景点=成人门票/人，酒店=每间每晚，餐厅=人均")
    price_source: str = Field(description="价格来源：price_table=票价表，amap_cost=高德人均，其他=按规则估算")


# ---------- 工具 ----------
# 工具设计的原则（这些描述是写给"对方的模型"看的）：
#   1. 名字是动词短语，一眼看出做什么：search_places，而不是 places / amap_v3_place_text
#   2. 参数用 Literal 和取值范围限制，让模型少犯错；每个参数都写 description
#   3. 说明里写清楚"返回什么"和"什么时候用"
#   4. 返回精简的、对决策有用的字段，而不是把高德的原始 JSON 全部丢回去（省 token，也少干扰）
#   5. annotations 标明只读、会访问外部世界，客户端（比如 Claude Desktop）可以据此决定要不要先问用户

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@mcp.tool(title="搜索地点", annotations=READ_ONLY)
async def search_places(
    city: Annotated[str, Field(description="城市名，例如 北京、杭州")],
    keyword: Annotated[str, Field(description="搜索关键词，例如 博物馆、烤鸭、亲子酒店")],
    kind: Annotated[Literal["attraction", "hotel", "food"], Field(description="地点类型，决定价格的含义")] = "attraction",
    month: Annotated[int, Field(ge=1, le=12, description="出行月份，影响景点的淡旺季票价")] = 10,
    hotel_level: Annotated[str, Field(description="酒店档次，高德没有价格时按档次估算：经济型酒店 / 舒适型酒店 / 高端酒店 / 民宿")] = "经济型酒店",
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> list[Place]:
    """在指定城市里按关键词搜索景点、酒店或餐厅，返回名称、区县、地址、坐标、评分和价格参考。
    没有结果时返回空列表，可以换一个更宽泛的关键词再试。"""
    try:
        pois = await get_amap().asearch_poi(keyword, city, limit=limit)
    except AmapError as exc:
        # ToolError：返回给客户端的是 isError=true + 这段文字。写清楚原因和下一步怎么办，对方的模型才能自己处理
        raise ToolError(f"高德查询失败（{exc}），请稍后重试或换一个关键词") from exc

    places = []
    for poi in pois:
        if kind == "attraction":
            price, source = ticket_price_for(poi, city, month)
        elif kind == "hotel":
            price, source = hotel_cost_for(poi, hotel_level)
        else:
            price, source = meal_cost_hint(poi)
        places.append(Place(
            name=poi.name, district=poi.district, address=poi.address[:40],
            location=Location(**poi.location.model_dump()), rating=poi.rating, price=price, price_source=source,
        ))
    return places


@mcp.tool(title="天气预报", annotations=READ_ONLY)
async def get_weather(city: Annotated[str, Field(description="城市名")]) -> list[WeatherInfo]:
    """查询城市从今天起 4 天的天气预报（白天天气、白天和夜间温度）。更远的日期查不到。"""
    try:
        return await get_amap().aget_weather(city)
    except AmapError as exc:
        raise ToolError(f"天气查询失败（{exc}），请稍后重试") from exc


# ---------- 资源：只读的数据，客户端可以直接读取，不需要模型"调用" ----------

@mcp.resource("prices://{city}", title="景点票价表", mime_type="application/json")
def city_prices(city: str) -> str:
    """某个城市的热门景点票价表（淡季 / 平季 / 旺季）。数据为估算值，仅供预算参考。"""
    # URI 里的中文会被 URL 编码：客户端读 prices://北京，这里收到的是 %E5%8C%97%E4%BA%AC，要先解码
    city = unquote(city)
    rows: dict[str, Any] = {}
    for (table_city, name), profile in load_price_table().items():
        if table_city == city.removesuffix("市"):
            rows[name] = profile
    return json.dumps(rows, ensure_ascii=False)


# ---------- 提示模板：客户端可以直接选用的"预设指令" ----------

@mcp.prompt(title="规划一次旅行")
def plan_trip(city: str, days: str, preferences: str = "") -> str:
    """生成一段让 Agent 用本服务的工具规划旅行的指令"""
    return (
        f"请帮我规划一次 {city} {days} 天的旅行，偏好：{preferences or '无'}。\n"
        "要求：先用 search_places 搜索景点、酒店和餐厅，用 get_weather 查天气；"
        "只推荐搜索结果里出现的地点，价格以工具返回的 price 为准；同一天的景点尽量在同一个区。"
    )


# ---------- 鉴权 ----------

class BearerAuth:
    """最简单的令牌鉴权：请求头必须带 Authorization: Bearer <MCP_TOKEN>

    这是一个"纯 ASGI 中间件"：ASGI 是 Python Web 服务的底层协议，FastAPI、Starlette 都建立在它上面
    （前端类比：Koa 的中间件 async (ctx, next) => {...}，只是参数换成了 scope / receive / send）
    生产环境应该用 OAuth（FastMCP 的 token_verifier / auth 参数），这里先把"没有令牌就拒绝"做到
    """

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.token:
            headers = dict(scope["headers"])  # ASGI 里请求头是 [(b"name", b"value"), ...]
            if headers.get(b"authorization") != f"Bearer {self.token}".encode():
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)  # 放行，交给 MCP 处理（相当于 await next()）


def build_app(token: str) -> ASGIApp:
    return BearerAuth(mcp.streamable_http_app(), token)


if __name__ == "__main__":
    settings = get_settings()
    if not settings.mcp_token:
        print("⚠️  没有设置 MCP_TOKEN，任何人都能调用这个服务，只能在本机开发时这样做")
    uvicorn.run(build_app(settings.mcp_token), host="127.0.0.1", port=settings.mcp_port)
```

**要点**

1. **MCP 的三种能力**：
   - **tools**（工具）：由**模型**决定什么时候调用，可以有副作用。
   - **resources**（资源）：只读数据，由**客户端应用**决定什么时候读，比如 Claude Code 里用 `@` 引用。
   - **prompts**（提示模板）：预设的指令，由**用户**选择使用，比如 Claude Code 里的斜杠命令。
2. **工具设计原则**（代码注释里的 5 条）是本课最重要的内容。**工具的名字、描述、参数是写给模型看的**，模型完全靠它们决定"什么时候用、怎么用"。描述写得模糊，模型就会用错。对照一下第 8 课 amap-mcp-server 的 `maps_text_search`：参数叫 `keywords`、`city`、`types`，没有说明返回什么，返回的是高德原始 JSON 的一大段文字。
3. **结构化输出**：返回类型写成 `list[Place]`，FastMCP 会生成 `outputSchema`，调用结果里同时带上 `structuredContent`（结构化 JSON）和 `content`（文本）。调用方是代码时用前者，是模型时用后者。
4. **错误语义**：`raise ToolError(...)` 会让结果带上 `isError: true`。调用方（不管是代码还是模型）一眼就知道失败了。错误信息写清楚"为什么失败、下一步怎么办"。注意 `ToolError` 的信息里用的是 `AmapError`，第 5 课已经保证它不带 Key（测试里也验证了）。
5. **坑：资源 URI 里的中文**。客户端读取 `prices://北京` 时，URI 会被 URL 编码，服务端收到的 `city` 是 `%E5%8C%97%E4%BA%AC`。我第一版没解码，查出来是空的，**而且不报错**。所以要 `unquote`。凡是 URI、URL 里可能出现中文的地方，都要想一想编码问题。
6. **鉴权**：MCP 服务一旦部署，就是一个**对外的 API**，必须鉴权，否则谁都能用你的高德额度。这里用最简单的固定令牌；生产环境应该用 OAuth，FastMCP 内置了支持（`token_verifier` 参数），作业 3 让你了解一下。
7. **只读**：没有开放 `book_hotel`。写操作需要用户确认（P6），外部 Agent 不一定有确认流程。**开放出去的能力，权限要尽量小**。

启动：

```bash
uv run python -m mcp_server.server
# INFO:     Uvicorn running on http://127.0.0.1:7002
```

## Step 3：MCP 客户端 `app/services/trip_mcp.py`

```python
"""主服务作为 MCP 客户端，调用我们自己的 trip-data MCP Server（P8）

两种用法：
  1. load_trip_mcp_tools()：把 MCP 工具原样交给 Agent（模型直接调用，结果原样返回给模型）
  2. call_trip_tool()：在我们自己的工具里调用 MCP 工具，拿到结构化数据后再加工（比如写进 state）
"""

import logging
from functools import lru_cache
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings

logger = logging.getLogger(__name__)


class TripMCPError(Exception):
    """MCP 工具返回了错误，或者服务连不上"""


@lru_cache
def get_trip_mcp_client() -> MultiServerMCPClient | None:
    settings = get_settings()
    if not settings.trip_mcp_url:
        return None
    return MultiServerMCPClient({
        "trip": {
            "transport": "streamable_http",  # 和第 8 课的 stdio 不同：通过 HTTP 连接一个独立运行的服务
            "url": settings.trip_mcp_url,
            "headers": {"Authorization": f"Bearer {settings.mcp_token}"},
            "timeout": 10,
        }
    })


_tools_cache: list[BaseTool] | None = None


async def load_trip_mcp_tools(names: set[str]) -> list[BaseTool]:
    """读取 MCP Server 提供的工具，只保留需要的那几个

    服务连不上时返回空列表（降级）：Agent 少了几个工具，但基本功能照常可用
    成功的结果缓存起来，不用每次对话都去问一遍"你有哪些工具"
    """
    global _tools_cache
    client = get_trip_mcp_client()
    if client is None:
        return []
    if _tools_cache is None:
        try:
            _tools_cache = await client.get_tools()
        except Exception as exc:
            logger.warning("trip-data MCP 不可用，本次不使用 MCP 工具: %s", type(exc).__name__)
            return []
    return [tool for tool in _tools_cache if tool.name in names]


async def call_trip_tool(name: str, arguments: dict[str, Any]) -> Any:
    """直接调用一个 MCP 工具，返回结构化结果（structuredContent）"""
    client = get_trip_mcp_client()
    if client is None:
        raise TripMCPError("未配置 TRIP_MCP_URL")
    try:
        # session：和 MCP Server 建立一次会话（握手、协商协议版本），用完自动关闭
        async with client.session("trip") as session:
            result = await session.call_tool(name, arguments)
    except Exception as exc:
        raise TripMCPError(f"MCP 服务连接失败: {type(exc).__name__}") from exc
    if result.isError:  # 工具执行失败：错误信息在 content 的文字里
        raise TripMCPError("".join(getattr(block, "text", "") for block in result.content))
    data = result.structuredContent or {}
    # 返回值是列表时，MCP 规定 structuredContent 必须是对象，FastMCP 会把它包成 {"result": [...]}
    return data.get("result", data)
```

**两种用法的区别**：

| | `load_trip_mcp_tools()`：工具交给模型 | `call_trip_tool()`：在自己的工具里调用 |
|---|---|---|
| 谁调用 | 模型 | 我们的代码 |
| 结果去哪 | 原样作为工具结果给模型看 | 我们加工后写进 state |
| 适合 | 查询类、结果只用来回答问题（天气） | 结果需要进入系统状态（新候选） |
| 本课 | `get_weather` | `search_more_places` 里调用 `search_places` |

## Step 4：修改助手接入 MCP `app/graph/edit_agent.py`

导入：

```python
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware, ModelRequest, SummarizationMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware,
    dynamic_prompt,
)
...
from langchain_core.tools import BaseTool
...
from app.services.trip_mcp import TripMCPError, call_trip_tool
```

在 `describe_booking` 上面加新工具：

```python
PRICE_KEYS = {"attraction": "ticket_price_hint", "hotel": "estimated_cost_hint", "food": "meal_cost_hint"}


@tool
async def search_more_places(kind: Literal["attraction", "hotel", "food"], keyword: str, runtime: Runtime) -> Command | str:
    """候选里没有用户想要的地点时，用关键词搜索更多候选（例如 烤鸭、亲子乐园、温泉酒店）。
    搜到的地点会加入候选列表，之后就可以用 replace_attraction / add_attraction / replace_meal 使用它们。"""
    request: TripRequest = runtime.state["request"]
    try:
        # P8：通过 MCP 调用我们自己的 trip-data 服务（它和 PlannerContext 用的是同一套价格口径）
        places = await call_trip_tool("search_places", {
            "city": request.city, "keyword": keyword, "kind": kind,
            "month": request.start_date.month, "hotel_level": request.accommodation, "limit": 8,
        })
    except TripMCPError as exc:
        return f"搜索失败：{exc}"

    context = json.loads(json.dumps(runtime.state["context"]))  # 深拷贝（context 是纯 JSON 数据，这样最简单）
    rows = context["tool_snapshot"][CANDIDATE_KEYS[kind]]
    known = {row["name"] for row in rows}
    added = []
    for place in places:
        if place["name"] in known:
            continue
        # 转换成和 PlannerContext 里一样的格式，之后的工具、grounding 都能直接用
        rows.append({
            "name": place["name"], "district": place["district"], "address": place["address"],
            "location": place["location"], "rating": place["rating"],
            PRICE_KEYS[kind]: place["price"], "price_source": place["price_source"],
        })
        added.append({"name": place["name"], "district": place["district"], "price": place["price"]})
    if not added:
        return f"没有搜到新的「{keyword}」，可以换个关键词"
    return Command(update={
        "context": context,
        "messages": [ToolMessage(f"已加入候选：{json.dumps(added, ensure_ascii=False)}", tool_call_id=runtime.tool_call_id)],
    })
```

system prompt 规则加第 8 条：

```
8. 候选里没有用户想要的地点时，先用 search_more_places 搜索（如果有这个工具），再从搜到的结果里选。
```

`build_edit_agent` 改成：

```python
# P8：依赖 trip-data MCP 服务的工具。服务不可用时整组去掉，Agent 照常工作，只是不能搜索新地点
MCP_TOOL_NAMES = {"get_weather"}  # 直接交给模型使用的 MCP 工具


def build_edit_agent(llm: BaseChatModel, mcp_tools: list[BaseTool] | None = None):
    """每次调用时用运行时传入的 llm 创建（create_agent 只是把节点连成图，耗时可以忽略）

    mcp_tools（P8）：从 trip-data MCP 服务加载的工具；传了说明服务可用，同时启用 search_more_places
    """
    extra = [search_more_places, *mcp_tools] if mcp_tools else []
    return create_agent(
        llm,
        tools=[*EDIT_TOOLS, *extra],
        ...
        middleware=[
            edit_prompt,
            HumanInTheLoopMiddleware(...),
            # P8：外部服务的工具偶尔会因为网络抖动失败（抛异常），自动重试 2 次，间隔 0.5 秒、1 秒
            # 注意：工具"正常地返回了错误信息"（比如"没有搜到"）不算异常，不会重试，交给模型自己处理
            ToolRetryMiddleware(tools=["search_more_places", "get_weather"], max_retries=2, initial_delay=0.5),
            # P8：一轮对话里最多搜索 3 次，防止模型反复换关键词搜个没完（每次都要花钱、花时间）
            ToolCallLimitMiddleware(tool_name="search_more_places", run_limit=3),
            SummarizationMiddleware(...),
        ],
        ...
    )
```

**要点**

1. **搜索结果进入"候选"，而不是直接进入行程**。`search_more_places` 只扩充候选，真正修改行程还是走 `replace_meal` 这些有校验的工具。所有修改都经过同一道门，grounding 不会被绕过。
2. **两种失败，两种处理**：网络抖动、服务暂时不可用是"异常"，用 `ToolRetryMiddleware` 自动重试，模型感知不到；"没搜到""参数不对"是"正常的业务结果"，作为文字返回给模型，让它换个关键词。**分清"该由系统处理的错误"和"该由模型处理的错误"**，是工具工程里很重要的一点。
3. **`ToolCallLimitMiddleware` 是成本护栏**。模型有时会陷入"换个关键词再搜一次"的循环。`run_limit=3` 表示一次调用（一轮对话）里最多 3 次，超过之后中间件会告诉模型"已达到次数上限"（`exit_behavior="continue"`，默认值），模型只能用已有的结果回答。P11 会系统地讲成本护栏。

## Step 5：assistant 节点 `app/graph/trip_graph.py`

导入：

```python
from app.graph.edit_agent import MCP_TOOL_NAMES, build_edit_agent
...
from app.services.trip_mcp import load_trip_mcp_tools
```

`assistant` 节点创建 Agent 时传入 MCP 工具，返回时带上 `context`：

```python
    # P8：加载 MCP 工具（服务不可用时是空列表，Agent 照常工作）
    agent = build_edit_agent(runtime.context.llm, await load_trip_mcp_tools(MCP_TOOL_NAMES))
    ...
    # P8：context 也要带回来，search_more_places 可能往里面加了新的候选，下一轮对话还要用
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result["messages"]],
        "plan": result["plan"],
        "context": result["context"],
    }
```

## Step 6：给别人用：Claude Code 和 MCP Inspector

**MCP Inspector**（官方调试工具，类似 Postman）：

```bash
npx @modelcontextprotocol/inspector
```

浏览器打开后，Transport 选 Streamable HTTP，URL 填 `http://127.0.0.1:7002/mcp`，在 Authentication 里加上 `Authorization: Bearer <你的 MCP_TOKEN>`，Connect。可以看到工具列表、每个工具的 JSON Schema，直接填参数调用，看返回的 `structuredContent`。

**Claude Code**：

```bash
claude mcp add --transport http trip-data http://127.0.0.1:7002/mcp \
  --header "Authorization: Bearer <你的 MCP_TOKEN>"
```

然后在 Claude Code 里问"帮我查一下杭州有哪些亲子景点，门票多少钱"，它会调用你的 `search_places`。输入 `/` 能看到 `plan_trip` 提示模板，输入 `@` 能引用 `prices://杭州` 资源。**这就是"别的团队的 Agent 复用你的能力"的样子**：你没有为 Claude Code 写一行代码。

## Step 7：测试 `tests/test_mcp.py`

```python
"""P8：自建 MCP Server，以及主服务通过 MCP 使用它"""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from langchain_mcp_adapters.client import MultiServerMCPClient

import mcp_server.server as trip_server
from app.agents.deps import get_planner_deps
from app.api.main import app
from app.graph.state import PlannerDeps
from app.services import trip_mcp
from app.services.amap_client import AmapClient
from tests.fakes import POI_RESPONSE, WEATHER_RESPONSE, FakeLLM, good_plan_json, make_fake_amap, tool_call

TOKEN = "test-token"
ROAST_DUCK = {"status": "1", "pois": [
    {"id": "D1", "name": "四季民福烤鸭店(故宫店)", "type": "餐饮服务", "address": "南池子大街", "adname": "东城区",
     "location": "116.401,39.912", "biz_ext": {"rating": "4.8", "cost": "180.00"}},
]}


def amap_with_roast_duck() -> AmapClient:
    """假高德：搜"烤鸭"时返回一家新餐厅，其他关键词返回第 11 课的数据"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/weather/weatherInfo"):
            return httpx.Response(200, json=WEATHER_RESPONSE)
        if request.url.params.get("keywords") == "烤鸭":
            return httpx.Response(200, json=ROAST_DUCK)
        if request.url.params.get("keywords") == "坏掉的":
            return httpx.Response(500)
        return httpx.Response(200, json=POI_RESPONSE)

    return AmapClient("fake-key", transport=httpx.MockTransport(handler), qps_limit=0)


@pytest.fixture(scope="module")  # scope="module"：整个文件只启动一次服务，所有测试共用
def mcp_url():
    trip_server.get_amap = amap_with_roast_duck
    with socket.socket() as s:  # 让操作系统分配一个空闲端口，避免和别的服务冲突
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(trip_server.build_app(TOKEN), port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True


def client_for(url: str, token: str = TOKEN) -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {"trip": {"transport": "streamable_http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}
    )


# ---------- MCP Server 本身 ----------

def test_tools_return_structured_data(mcp_url):
    async def scenario():
        async with client_for(mcp_url).session("trip") as session:
            tools = (await session.list_tools()).tools
            result = await session.call_tool("search_places", {"city": "北京", "keyword": "博物馆"})
            return tools, result

    tools, result = asyncio.run(scenario())
    assert {t.name for t in tools} == {"search_places", "get_weather"}
    search = next(t for t in tools if t.name == "search_places")
    assert search.annotations.readOnlyHint is True
    assert search.outputSchema is not None  # 声明了返回结构，客户端可以据此解析
    places = result.structuredContent["result"]
    assert places[0] == {
        "name": "故宫博物院", "district": "东城区", "address": "景山前街4号",
        "location": {"longitude": 116.397029, "latitude": 39.917839},
        "rating": 4.9, "price": 60, "price_source": "price_table",  # 和 PlannerContext 同一套价格口径
    }


def test_tool_error_is_reported(mcp_url):
    async def scenario():
        async with client_for(mcp_url).session("trip") as session:
            return await session.call_tool("search_places", {"city": "北京", "keyword": "坏掉的"})

    result = asyncio.run(scenario())
    assert result.isError is True  # 第 8 课的 amap-mcp-server 在这里不会标记错误
    assert "高德查询失败" in result.content[0].text
    assert "fake-key" not in result.content[0].text  # 第 9 课的教训：错误信息里不能有 Key


def test_resource_and_prompt(mcp_url):
    async def scenario():
        async with client_for(mcp_url).session("trip") as session:
            resource = await session.read_resource("prices://北京")
            prompt = await session.get_prompt("plan_trip", {"city": "北京", "days": "3"})
            return resource, prompt

    resource, prompt = asyncio.run(scenario())
    assert "故宫博物院" in json.loads(resource.contents[0].text)
    assert "search_places" in prompt.messages[0].content.text


def test_requires_token(mcp_url):
    assert httpx.post(mcp_url, json={}).status_code == 401
    assert httpx.post(mcp_url, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401


# ---------- 主服务通过 MCP 使用它 ----------

@pytest.fixture
def use_mcp(mcp_url, monkeypatch):
    monkeypatch.setattr(trip_mcp, "get_trip_mcp_client", lambda: client_for(mcp_url))
    monkeypatch.setattr(trip_mcp, "_tools_cache", None)


def chat(client, thread_id: str, message: str) -> list[dict]:
    with client.stream("POST", f"/api/trip/{thread_id}/chat", json={"message": message}, headers={"X-User-Id": "u"}) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_search_then_replace(client, trip_request, use_mcp):
    llm = FakeLLM(replies=[
        good_plan_json(),
        tool_call("replace_meal", day=1, meal_type="dinner", restaurant="四季民福烤鸭店"),  # 候选里没有，失败
        tool_call("search_more_places", kind="food", keyword="烤鸭"),  # 去 MCP 搜
        tool_call("replace_meal", day=1, meal_type="dinner", restaurant="四季民福烤鸭店"),  # 再试：成功
        "第 1 天晚餐换成了四季民福烤鸭店，人均 180 元。",
    ])
    app.dependency_overrides[get_planner_deps] = lambda: PlannerDeps(amap=make_fake_amap(), llm=llm)
    thread_id = client.post("/api/trip/plan", json=trip_request, headers={"X-User-Id": "u"}).json()["thread_id"]

    events = chat(client, thread_id, "第一天晚上想吃烤鸭")
    results = [e["content"] for e in events if e["type"] == "tool_result"]
    assert results[0].startswith("修改失败")
    assert "已加入候选" in results[1]
    dinner = next(m for m in events[-1]["plan"]["days"][0]["meals"] if m["type"] == "dinner")
    assert dinner == {"type": "dinner", "name": "四季民福烤鸭店(故宫店)", "estimated_cost": 180}  # 价格来自高德人均

    # 新候选被保存进了会话：下一轮对话还能直接用
    llm.replies += [tool_call("replace_meal", day=2, meal_type="lunch", restaurant="四季民福烤鸭店"), "好的"]
    events = chat(client, thread_id, "第二天中午也吃这家")
    lunch = next(m for m in events[-1]["plan"]["days"][1]["meals"] if m["type"] == "lunch")
    assert lunch["name"] == "四季民福烤鸭店(故宫店)"


def test_mcp_down_degrades_gracefully(client, trip_request, monkeypatch):
    # 指向一个没有服务的端口：加载工具失败，Agent 照常工作，只是没有搜索和天气工具
    monkeypatch.setattr(trip_mcp, "get_trip_mcp_client", lambda: client_for("http://127.0.0.1:9/mcp"))
    monkeypatch.setattr(trip_mcp, "_tools_cache", None)
    llm = FakeLLM(replies=[good_plan_json(), "第 2 天去故宫博物院。"])
    app.dependency_overrides[get_planner_deps] = lambda: PlannerDeps(amap=make_fake_amap(), llm=llm)
    thread_id = client.post("/api/trip/plan", json=trip_request, headers={"X-User-Id": "u"}).json()["thread_id"]
    assert chat(client, thread_id, "第二天去哪")[-1]["reply"] == "第 2 天去故宫博物院。"
```

```bash
uv run pytest -q
# 68 passed
```

**`scope="module"` 的 fixture**：启动服务比较慢，整个测试文件只启动一次，所有测试共用。第 11 课用的 fixture 都是默认的 `scope="function"`（每个测试重新创建一次）。

**`test_search_then_replace` 模拟的正是需求卡片里的场景**：模型先尝试直接替换 → 工具说"不在候选里" → 模型去搜索 → 搜到后再替换 → 成功。注意替换时模型写的是"四季民福烤鸭店"，存进行程的是"四季民福烤鸭店(故宫店)"：第 9 课的 `find_candidate` 去掉括号后做了匹配，用的是候选的完整名字和高德的人均价格。

## 本地开发时同时跑两个服务

```bash
# 终端 1
uv run python -m mcp_server.server
# 终端 2
uv run python run.py
```

不启动 MCP Server 也没关系，助手会降级（日志里有一行 `trip-data MCP 不可用`），只是不能搜索新地点、不能查天气。P13 会用 Docker Compose 把它们一起管理起来。

## 前端改动（做到这一课时让我直接生成）

不需要新接口。对话面板里，`tool_result` 事件的 `name` 是 `search_more_places` 时，显示成"🔍 搜到了 3 家烤鸭店"（解析 `content` 里的列表），并且可以展开看名字和价格。

## 📝 课后作业

- [ ] 1. **用 Claude Code 当你的用户**：按 Step 6 接入，让它"用 plan_trip 规划一次杭州 2 天亲子游"。观察它调用了几次工具、有没有用到价格。然后反过来想：如果你是别的团队，看着这些工具描述，有哪里不清楚？改进你的工具描述。
- [ ] 2. **再加一个工具**：`estimate_trip_cost(city, attraction_names, nights, hotel_level, people)`，返回门票、住宿的估算总价（复用 pricing.py）。这正是需求卡片里"商旅报销 Agent"最需要的。想一想：参数里的景点名，对方的模型可能写得不准（"故宫" vs "故宫博物院"），你的工具怎么处理？
- [ ] 3. **了解 OAuth**：读一下 MCP 官方文档里的 Authorization 部分，用自己的话说说：固定令牌和 OAuth 相比，缺点是什么？（提示：令牌泄露了怎么办？怎么区分是哪个团队在调用？怎么只给某个团队开放部分工具？）
- [ ] 4. **思考题**：`load_trip_mcp_tools` 把工具列表永久缓存了。如果 MCP Server 升级、新增了一个工具，主服务要重启才能用上。你会怎么改？缓存失效的时机怎么选？

## 我的笔记

<!-- 在这里记录你的答案和心得 -->
