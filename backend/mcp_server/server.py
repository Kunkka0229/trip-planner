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

import hmac
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
from app.observability.logs import setup_logging
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
        pois = await get_amap().asearch_poi(keyword, city, limit)
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
            name=poi.name,
            district=poi.district,
            address=poi.address[:40],
            location=Location(**poi.location.model_dump()),
            rating=poi.rating,
            price=price,
            price_source=source
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

            # hmac.compare_digest：恒定时间比较。!= 遇到第一个不同的字符就返回，攻击者理论上能靠响应时间
            # 一位一位猜出令牌（时序攻击）；compare_digest 不管哪一位不同，耗时都一样。比较密钥、令牌都应该用它
            # 默认值 b""：没带这个请求头时 get 返回 None，而 compare_digest 不接受 None
            expected = f"Bearer {self.token}".encode()
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            
        await self.app(scope, receive, send)  # 放行，交给 MCP 处理（相当于 await next()）
        
def build_app(token: str) -> ASGIApp:
    return BearerAuth(mcp.streamable_http_app(), token)

if __name__ == "__main__":
    settings = get_settings()
    # 和主服务用同一套日志配置：httpx 的请求日志会带上完整 URL，高德的 key 就在 URL 里，不能打到终端
    setup_logging(settings.log_level)
    if not settings.mcp_token:
        print("⚠️  没有设置 MCP_TOKEN，任何人都能调用这个服务，只能在本机开发时这样做")
    uvicorn.run(build_app(settings.mcp_token), host="127.0.0.1", port=settings.mcp_port)