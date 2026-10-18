"""Checkpointer：把图的 state 持久化，按 thread_id 区分不同的会话

CHECKPOINT_URL 的三种写法：
  memory                         内存，进程重启就没了（测试用）
  sqlite:///data/checkpoints.db  SQLite 文件，路径相对于 backend/（本地开发用）
  postgresql://user:pass@host/db Postgres（生产用，需要 uv add langgraph-checkpoint-postgres "psycopg[binary,pool]"）
"""

from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import AsyncGenerator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.config import BACKEND_DIR

# 序列化器：state 存进数据库前要转成字节，读出来再还原成对象
# 还原时，LangGraph 只信任它认识的类型（消息、基础类型……）。我们自己的 Pydantic 模型要显式加入白名单，
# 否则会警告 "Deserializing unregistered type ... will be blocked in a future version"
# 为什么要白名单？反序列化时会按记录里的"模块名 + 类名"去创建对象，如果数据库被人篡改，
# 不设白名单就可能被诱导去实例化任意类，这是一类经典的安全漏洞（反序列化攻击）
SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[("app.models.schemas", "TripRequest"), ("app.models.schemas", "TripPlan")]
)

def with_serde(saver: BaseCheckpointSaver) -> BaseCheckpointSaver:
    """把上面带白名单的 SERDE 装到 saver 上，返回同一个 saver（方便写成 yield with_serde(saver) 一行）

    为什么不在创建时传 serde=SERDE？
      InMemorySaver(serde=SERDE) 可以直接传；但 SQLite / Postgres 的 saver 是用
      from_conn_string(url) 创建的，这个工厂方法只接收连接字符串，没有 serde 参数，
      只能等它创建好之后再替换掉默认的序列化器
    serde 是 saver 的普通属性，存档（put）和读档（get）时才会用到，
    所以只要在第一次读写之前替换掉就能生效
    """
    saver.serde = SERDE
    return saver

@asynccontextmanager
async def open_checkpointer(url: str) -> AsyncGenerator[BaseCheckpointSaver, None]:
    """打开 checkpointer，用完自动关闭连接。在 main.py 的 lifespan 里使用"""
    if url == "memory":
        yield InMemorySaver(serde=SERDE)
    elif url.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        path = BACKEND_DIR / url.removeprefix("sqlite:///")
        path.parent.mkdir(parents=True, exist_ok=True)  # 自动创建 data/ 目录
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            yield with_serde(saver)
    elif url.startswith(("postgresql://", "postgres://")):
        # 在函数里导入：不用 Postgres 的人不需要安装这个包
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()  # 第一次运行时建表，之后再调用不会重复建
            yield with_serde(saver)
    else:
        raise ValueError(f"不支持的 CHECKPOINT_URL: {url}")