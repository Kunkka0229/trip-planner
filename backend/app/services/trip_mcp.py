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
    