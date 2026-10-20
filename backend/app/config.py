"""应用配置：从环境变量和 backend/.env 文件读取"""

from functools import lru_cache  # 标准库里的缓存装饰器，这里用来实现"单例"
from pathlib import Path         # 处理文件路径的标准库，类似 Node 的 path 模块
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录的绝对路径
# __file__              = .../backend/app/config.py（当前文件）
# .resolve()            = 转成绝对路径
# .parent               = .../backend/app
# .parent.parent        = .../backend
# 为什么不直接写 ".env"？相对路径是相对于"你在哪个目录执行命令"（cwd），
# 在别的目录启动时就会找不到 .env。用 __file__ 算出来的路径永远准确。
BACKEND_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    """
    继承 BaseSettings 后，每个字段都会自动去找同名的环境变量（不区分大小写）：
        字段 amap_api_key  ←→  环境变量 AMAP_API_KEY
    读取优先级（高 → 低）：
        1. 真实的环境变量（比如 PORT=8001 uv run python run.py）
        2. .env 文件
        3. 这里写的默认值
    """
    
    # model_config：这个配置类本身的行为设置
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",  # 指定 .env 文件路径
        env_file_encoding="utf-8",      # 指定 .env 文件编码
        extra="ignore",                 # .env 里有未声明的变量时忽略，不报错
    )
    
    # ---------- 应用信息 ----------
    # 应用名称
    app_name: str = "HelloAgents智能旅行助手"
    app_version: str = "1.0.0"
    
    # ---------- 服务器 ----------
    host: str = "0.0.0.0"  # 0.0.0.0 = 监听所有网卡，局域网其他设备也能访问
    port: int = 7001       # 声明成 int：.env 里写的是字符串 "7000"，会被自动转成整数

    # 如果写成 PORT=abc，启动时直接报错，而不是运行到一半才出问题

    # 允许跨域的前端地址，多个用英文逗号分隔
    # 为什么用 str 而不是 list[str]？pydantic-settings 对 list 类型要求环境变量写成
    # JSON 格式（["http://a","http://b"]），在 .env 里写起来很别扭，所以存字符串再自己拆
    
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    
    # ---------- 第三方服务密钥 ----------
    amap_api_key: str = ""   # 高德地图 Web 服务 Key
    llm_api_key: str = ""    # 大模型 API Key
    llm_base_url: str = ""   # 大模型接口地址（OpenAI 兼容格式）
    llm_model_id: str = ""   # 模型名称
    llm_timeout: int = 180   # 单次大模型调用的超时（秒），生成多日行程比较慢

    log_level: str = "INFO"
    
    # 规划模式（.env 里用 PLANNER_MODE 切换）：
    #   context     = PlannerContext：代码并行收集上下文 + 单 Agent 规划（第 9 课，默认）
    #   direct      = 代码直连高德 + 单 Agent 规划（第 6、7 课）
    #   multi_agent = 书中的多 Agent + MCP，LangGraph 编排（第 8 课）
    # Literal：只允许这三个值，.env 里写错（比如 contxt）启动时就会直接报错
    planner_mode: Literal["graph", "context", "direct", "multi_agent"] = "graph"

    amap_qps_limit: float = 3  # 高德每秒最多请求几次，个人开发者 Key 的并发上限比较低；0 = 不限速

    app_env: str = "dev"  # 运行环境：dev / prod（P2）

    # ---------- 可观测性（P2）----------
    langfuse_public_key: str = ""  # 留空 = 不启用 Langfuse
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "http://localhost:3000"
    
    # ---------- 会话持久化（P5）----------
    checkpoint_url: str = "sqlite:///data/checkpoints.db"  # 写法见 app/graph/checkpoint.py

    # ---------- 长期记忆（P7）----------
    store_url: str = "sqlite:///data/store.db"  # 写法和 checkpoint_url 一样
    embedding_model: str = ""  # 留空 = 不建向量索引，召回时不按相关性排序；百炼可填 text-embedding-v4
    embedding_dims: int = 1024  # 向量维度，要和 embedding 模型的输出一致
    
    # @property：把方法变成"只读属性"，调用时不用加括号：settings.cors_origins_list
    # 类似 JS class 里的 get corsOriginsList() { ... }
    @property
    def cors_origins_list(self) -> list[str]:
        """把逗号分隔的字符串拆成列表，并去掉空白和空项"""
        # 列表推导式，相当于 JS 的：
        # this.cors_origins.split(",").map(o => o.trim()).filter(o => o)
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# @lru_cache：缓存函数返回值。第一次调用时创建 Settings 并读取 .env，
# 之后每次调用都直接返回同一个对象，相当于单例，不会重复读文件
@lru_cache
def get_settings() -> Settings:
    return Settings()