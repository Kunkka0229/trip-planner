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
