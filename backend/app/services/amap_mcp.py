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
                "args": ["--with", "pydantic<2.14", "amap-mcp-server==0.1.11"],
                # 传给子进程的环境变量：amap-mcp-server 从 AMAP_MAPS_API_KEY 读取 Key
                "env": {"AMAP_MAPS_API_KEY": settings.amap_api_key},
            }
        }
    )